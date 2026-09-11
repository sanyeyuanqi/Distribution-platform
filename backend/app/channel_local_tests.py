"""One local test plan over confirmed uploads, deduplicated by original Key/model."""
import json
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from .channel_service import UploadInput, new_task
from .distribution_monitoring import observation_json, test_item_observation
from .models import Category, CredentialFormat, Site
from .models_channels import Distribution, TaskItem
from .security import fingerprint

CONFLICT_MESSAGE = '同一 Key 在不同已上传分发中的调用地址或模型映射不一致，未发送此项测试；请核对分发配置'


def confirmed_models(dist):
    """Desired/local template models are not evidence of a successful upload."""
    value = (dist.remote_snapshot or {}).get('models')
    if isinstance(value, str):
        value = value.split(',') if value.strip() else []
    if not isinstance(value, list) or any(type(model) is not str for model in value):
        return []
    try:
        result = UploadInput.model_list(value)
        if any(any(ord(char) < 32 or ord(char) == 127 for char in model)
               or len(model.encode('utf-8')) > 255 for model in result):
            return []
        return result
    except (ValueError, UnicodeEncodeError):
        return []


def channel_test_projection(db, channels, distributions, eligibility):
    """Public metadata and latest channel progress in one page-wide query."""
    channel_ids = [channel.id for channel in channels]
    latest = select(TaskItem.id, func.row_number().over(partition_by=TaskItem.channel_id,
        order_by=(TaskItem.created_at.desc(), TaskItem.id.desc())).label('position')).where(
            TaskItem.channel_id.in_(channel_ids), TaskItem.operation == 'test',
            TaskItem.snapshot['test_scope'].as_string() == 'channel').subquery()
    items = {item.channel_id: item for item in db.scalars(select(TaskItem).join(latest, latest.c.id == TaskItem.id)
                                                       .where(latest.c.position == 1))} if channel_ids else {}
    models = {channel_id: [] for channel_id in channel_ids}
    for dist in distributions:
        if eligibility.get(dist.id, {}).get('local_test_available'):
            models[dist.channel_id].extend(confirmed_models(dist))
    empty = observation_json(SimpleNamespace(remote_snapshot={}), 'test')
    return {channel.id: {'local_test_models': sorted(set(models[channel.id])),
                        'local_test_available': bool(models[channel.id]) and not channel.archived,
                        'local_test_reason': None if models[channel.id] and not channel.archived else
                            '渠道已归档，不能测试' if channel.archived else '尚无可确认的已上传模型，请先完成上传或同步核实',
                        'connectivity_test': test_item_observation(items[channel.id]) if channel.id in items else empty}
            for channel in channels}


def invocation_signature(source, model):
    """Compare only fields used by the direct probe, not routing/RPM/account metadata."""
    snapshot = source['snapshot']
    config = snapshot['test_inputs']['channel_config']
    schema = snapshot['format_schema']
    remote_type = schema.get('remote_type')
    mapping = config.get('model_mapping') or {}
    setting = config.get('setting') or {}
    if isinstance(setting, str):
        try:
            setting = json.loads(setting)
        except ValueError:
            setting = {}
    if not isinstance(setting, dict):
        setting = {}
    result = {'target': mapping.get(model, model), 'explicit_mapping': model in mapping,
              'proxy': config.get('proxy') or setting.get('proxy')}
    if remote_type in (1, 3, 14):
        base = (config.get('base_url') or ('https://api.anthropic.com' if remote_type == 14 else 'https://api.openai.com')).rstrip('/')
        if remote_type == 1 and base == 'https://api.openai.com/v1':
            base = 'https://api.openai.com'
        result['base_url'] = base
    if remote_type == 1:
        result['organization'] = config.get('openai_organization') or ''
    if remote_type == 3:
        result['other'] = config.get('other')
    if remote_type == 41 and snapshot.get('test_wire_type') != 'vertex_api_key':
        region = config.get('other') or 'global'
        if isinstance(region, str) and region.startswith('{'):
            try:
                region = json.loads(region)
            except ValueError:
                pass
        result['region'] = region.get(model, region.get('default', 'global')) if isinstance(region, dict) else region
    if remote_type != 33:
        # Only Bedrock uses presence itself to suppress its automatic model alias.
        result.pop('explicit_mapping')
    return result


def plan_fingerprint(snapshot):
    return fingerprint(json.dumps([snapshot.get('test_sources'), snapshot.get('test_plan')],
                                  sort_keys=True, ensure_ascii=False))


def enqueue_channel_test(db, actor, channel, distributions, *, model=None, test_all=False, test_content=None):
    from .local_test_tasks import (
        initial_model_results,
        local_test_reason,
        local_test_snapshot,
        progress_result,
        uploaded_versions,
    )
    fmt, category = db.get(CredentialFormat, channel.format_id), db.get(Category, channel.category_id)
    versions = uploaded_versions(db, [dist.id for dist in distributions])
    eligible = [dist for dist in sorted(distributions, key=lambda row: (row.created_at, row.id))
                if not local_test_reason(channel, dist, versions.get(dist.id, [])) and confirmed_models(dist)]
    models = sorted({value for dist in eligible for value in confirmed_models(dist)})
    if not models or (not test_all and model not in models):
        raise HTTPException(422, '请选择当前本地渠道已确认上传的模型；缺少远端模型记录时请先同步核实')
    selected = models if test_all else [model]
    sources, candidates = [], {value: {} for value in selected}
    for dist in eligible:
        observed = confirmed_models(dist)
        relevant = [value for value in selected if value in observed]
        if not relevant:
            continue
        site = db.get(Site, dist.site_id)
        snap = local_test_snapshot(channel, fmt, dist, site, category, None, uploaded_version=versions[dist.id][0],
                                   test_all=True, observed_models=observed)
        snap.pop('test_progress')
        # Freeze only the tested source models; unrelated additions must not expand the plan.
        snap['test_models'] = relevant
        source = {'distribution_id': dist.id, 'site_id': site.id, 'snapshot': snap}
        sources.append(source)
        for value in relevant:
            for index in snap['test_key_indices']:
                candidates[value].setdefault(index, []).append(len(sources) - 1)
    plan, rows = [], []
    for value, indices in candidates.items():
        entries = []
        for index, options in sorted(indices.items()):
            signature = invocation_signature(sources[options[0]], value)
            entries.append({'key_index': index, 'source': options[0],
                            'conflict': any(invocation_signature(sources[other], value) != signature for other in options[1:])})
        plan.append({'model': value, 'keys': entries})
        rows.extend(initial_model_results([value], sorted(indices)))
    snapshot = {'test_source': 'local', 'test_scope': 'channel', 'test_model': None if test_all else model,
                'test_models': selected, 'test_all': test_all, 'test_content': test_content,
                'test_sources': sources, 'test_plan': plan, 'test_progress': progress_result(rows)}
    snapshot['test_plan_fingerprint'] = plan_fingerprint(snapshot)
    task = new_task(db, actor, channel.owner_id, 'test', group_id=channel.group_id)
    db.add(TaskItem(task_id=task.id, channel_id=channel.id, site_id=sources[0]['site_id'], distribution_id=None,
                    operation='test', key_version=channel.key_version, snapshot=snapshot))
    db.flush()
    return task


def assert_channel_test(db, task, item, actor):
    from .local_test_tasks import assert_local_test
    from .worker import WriteStopped
    snapshot = item.snapshot
    if (not snapshot.get('test_sources') or not snapshot.get('test_plan')
            or snapshot.get('test_plan_fingerprint') != plan_fingerprint(snapshot)):
        raise WriteStopped('已上传模型测试来源或冻结计划已改变，请重新发起测试')
    parts, fmt = [], None
    for source in snapshot['test_sources']:
        virtual = SimpleNamespace(channel_id=item.channel_id, distribution_id=source['distribution_id'],
                                  site_id=source['site_id'], key_version=item.key_version, snapshot=source['snapshot'])
        part, fmt = assert_local_test(db, task, virtual, actor)
        parts.append(part)
    return {'channel_plan': snapshot['test_plan'], 'source_parts': parts}, fmt


def lock_channel_sources(db, item):
    """Keep source removal/rotation from crossing an in-flight direct request."""
    from .worker import WriteStopped
    ids = sorted(source['distribution_id'] for source in item.snapshot.get('test_sources', []))
    try:
        with db.no_autoflush:
            found = list(db.scalars(select(Distribution.id).where(Distribution.id.in_(ids))
                                   .order_by(Distribution.id).with_for_update(nowait=True)))
        if found != ids:
            raise WriteStopped('已上传测试来源已删除，停止后续请求')
    except OperationalError:
        db.rollback()
        raise WriteStopped('已上传测试来源正在处理，请稍后重新发起测试') from None
