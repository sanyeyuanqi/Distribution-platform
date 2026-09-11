"""Durable local probes of credentials confirmed uploaded to one distribution."""
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select

from .auth import assert_owner
from .catalog_policy import catalog_default_base_url, catalog_model_mapping
from .channel_service import UploadInput, supported_format
from .credential_containers import partition_for
from .db import utcnow
from .distribution_monitoring import record_observation
from .distribution_status import _linked_snapshot
from .distribution_tombstones import locally_deleted
from .models import Category, CredentialFormat
from .models_channels import Channel, Distribution, DistributionVersion, KeyVersion
from .newapi_formats import protocol_schema
from .security import fingerprint


def local_test_reason(channel, dist, versions):
    if channel.archived:
        return '渠道已归档，不能测试'
    if locally_deleted(dist) or dist.status in ('deleted', 'missing') or not _linked_snapshot(dist):
        return '仅支持测试已上传且远端关联已确认的 Key，请先完成上传或同步核实'
    # dist.key_version can already be set when a create/rotation is queued.
    # Only the open, confirmed history row proves which credentials were sent.
    if len(versions) != 1 or versions[0].key_version != channel.key_version:
        return '当前 Key 版本尚未确认上传到此分发，请先完成上传'
    return None


def uploaded_versions(db, distribution_ids):
    by_dist = {}
    if distribution_ids:
        for version in db.execute(select(DistributionVersion.id, DistributionVersion.distribution_id,
                DistributionVersion.key_version).where(DistributionVersion.distribution_id.in_(distribution_ids),
                    DistributionVersion.valid_to.is_(None))):
            by_dist.setdefault(version.distribution_id, []).append(version)
    return by_dist


def local_test_projection(db, channels, distributions):
    """One metadata query for the full page, without reading credentials."""
    versions = uploaded_versions(db, [dist.id for dist in distributions])
    channels = {channel.id: channel for channel in channels}
    result = {}
    for dist in distributions:
        reason = local_test_reason(channels[dist.channel_id], dist, versions.get(dist.id, []))
        result[dist.id] = {'local_test_available': reason is None, 'local_test_reason': reason}
    return result


def confirmed_uploaded_version(db, channel, dist):
    versions = uploaded_versions(db, [dist.id]).get(dist.id, [])
    if reason := local_test_reason(channel, dist, versions):
        raise HTTPException(409, reason)
    return versions[0]


def local_inputs(channel, fmt, dist, adapter, category, *, models=None):
    part = partition_for(channel, fmt, dist.partition_key, site=SimpleNamespace(adapter=adapter))
    config = dict((dist.template_snapshot or {}).get('effective_channel_config') or {})
    if not config.get('base_url'):
        config['base_url'] = catalog_default_base_url(category)
    config['model_mapping'] = catalog_model_mapping(category, dist.models if models is None else models, config.get('model_mapping'))
    if (channel.upload_settings or {}).get('api_base_url'):
        config['base_url'] = channel.upload_settings['api_base_url']
    config.update(part['credential_channel_config'])
    return part, {'channel_config': config,
                  'members': part['member_fingerprints'],
                  'proxy_fingerprints': [fingerprint(entry['proxy']) if entry['proxy'] else None
                                         for entry in part['entries']],
                  'credential_fingerprint': channel.fingerprint}


def local_test_snapshot(channel, fmt, dist, site, category, model, *, uploaded_version, test_all=False, test_content=None,
                        observed_models=None):
    allowed_models = dist.models if observed_models is None else observed_models
    try:
        models = UploadInput.model_list(allowed_models if test_all else [model])
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, '请选择此站点分发已配置的有效模型') from None
    if not models or len(models) > 200 or any(value not in (allowed_models or []) for value in models):
        raise HTTPException(422, '测试模型范围无效，请核对此分发的已配置模型')
    # Only Vertex API Key partitioning differs between remote adapters. Recover
    # the original local partition even if the site's connection was changed.
    for adapter in dict.fromkeys([site.adapter, 'new-api-v1', 'silicon-v1']):
        try:
            part, inputs = local_inputs(channel, fmt, dist, adapter, category, models=observed_models)
            break
        except ValueError:
            continue
    else:
        raise HTTPException(422, '本地密钥分区无效，请核对渠道凭据')
    return {'test_source': 'local', 'test_model': None if test_all else model, 'test_models': models,
            **({'test_model_source': 'remote_snapshot', 'test_wire_type': part['wire_format_schema']['type']}
               if observed_models is not None else {}),
            'test_all': test_all, 'test_content': test_content, 'test_inputs': inputs,
            'test_key_indices': [index + 1 for index in part['indices']],
            'test_progress': progress_result(initial_model_results(models, [index + 1 for index in part['indices']])),
            'uploaded_key': {'remote_id': str(dist.remote_id), 'version_id': uploaded_version.id},
            'format_code': fmt.code, 'format_version': fmt.version,
            'format_schema': dict(fmt.schema_config or {}), 'category_id': channel.category_id,
            'channel_type': (fmt.schema_config or {}).get('remote_type', 1),
            'key_mode': channel.key_mode, 'key_count': part['key_count'],
            'partition_key': dist.partition_key, 'partition_adapter': adapter,
            'models': list(dist.models)}


def selected_test_models(snapshot):
    return snapshot.get('test_models', [snapshot.get('test_model')])


def initial_model_results(models, key_indices=()):
    return [{'model': model, 'status': 'pending', 'message': '', 'provider_message': None, 'latency_ms': None, 'http_status': None,
             'tested_count': 0, 'passed_count': 0, 'failed_count': 0,
             'key_results': [{'key_index': index, 'status': 'pending', 'message': '', 'provider_message': None,
                              'latency_ms': None, 'http_status': None} for index in key_indices]} for model in models]


def provider_message(outcome):
    """Accept only bounded, already-redacted provider text supplied by the probe."""
    value = outcome.get('provider_message')
    if type(value) is not str:
        return None
    value = (value[:1499] + '…' if len(value) > 1500 else value).strip()
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        return None
    if any(ord(char) < 32 and char not in '\n\r\t' or ord(char) == 127 for char in value):
        return None
    return value or None


def merge_provider_message(messages, outcome, *, failed=False):
    """Bound already-redacted probe text; never reinterpret the local message as provider output."""
    value = provider_message(outcome)
    if value and value not in messages['failed']:
        bucket = 'succeeded' if outcome['success'] else 'failed'
        if bucket == 'failed' and value in messages['succeeded']:
            messages['succeeded'].remove(value)
        if value not in messages[bucket] and sum(len(text) + 1 for text in messages[bucket]) < 4000:
            messages[bucket].append(value)
    if (failed or not outcome['success']) and not messages['failed']:
        return None  # A successful Key's text must not hide another Key's local/unknown failure.
    combined = '\n'.join([*messages['failed'], *messages['succeeded']])
    return (combined[:3999] + '…' if len(combined) > 4000 else combined) or None


def progress_result(rows, current_model=None, current_key_index=None):
    # One detached copy protects committed checkpoints while the next Key runs.
    # Model aggregates remain for older clients; individual results are never merged.
    copied = [{**row, 'key_results': [dict(key) for key in row.get('key_results', [])]} for row in rows]
    key_counts = {'succeeded': 0, 'failed': 0}
    for row in rows:
        for key in row.get('key_results', []):
            if key['status'] in key_counts:
                key_counts[key['status']] += 1
    return {'source': 'local', 'model_results': copied,
            'completed_count': sum(row['status'] in ('succeeded', 'failed') for row in rows),
            'total_count': len(rows), 'current_model': current_model,
            'total_key_count': sum(len(row.get('key_results', [])) for row in rows),
            'completed_key_count': sum(key_counts.values()),
            'passed_key_count': key_counts['succeeded'], 'failed_key_count': key_counts['failed'],
            'current_key_index': current_key_index,
            'tested_count': sum(row['tested_count'] for row in rows),
            'passed_count': sum(row['passed_count'] for row in rows),
            'failed_count': sum(row['failed_count'] for row in rows),
            'latency_ms': sum(row['latency_ms'] or 0 for row in rows)}


def assert_local_test(db, task, item, actor):
    from .worker import WorkRemoved, WriteStopped
    if item.snapshot.get('test_scope') == 'channel':
        from .channel_local_tests import assert_channel_test
        return assert_channel_test(db, task, item, actor)
    if item.snapshot.get('test_source') != 'local':
        raise WriteStopped('测试方式已更新，请在渠道详情重新发起本地测试')
    if task.cancelled:
        raise WriteStopped('测试已取消，停止后续请求')
    channel = db.get(Channel, item.channel_id, populate_existing=True)
    dist = db.get(Distribution, item.distribution_id, populate_existing=True)
    if not channel or not dist:
        raise WorkRemoved('本地渠道或分发已删除，停止测试')
    try:
        assert_owner(db, actor, channel.owner_id)
    except HTTPException:
        raise WriteStopped('执行人已没有此渠道的本地管理权限') from None
    if channel.archived or channel.key_version != item.key_version:
        raise WriteStopped('渠道已归档或凭据版本已改变')
    if (dist.channel_id != channel.id or dist.site_id != item.site_id
            or dist.partition_key != item.snapshot.get('partition_key', '')):
        raise WriteStopped('本地分发关联或密钥分区已改变，请重新发起测试')
    try:
        uploaded = confirmed_uploaded_version(db, channel, dist)
    except HTTPException as exc:
        raise WriteStopped(exc.detail) from None
    if item.snapshot.get('uploaded_key') != {'remote_id': str(dist.remote_id), 'version_id': uploaded.id}:
        raise WriteStopped('已上传 Key 的远端关联或版本记录已改变，请重新发起测试')
    from .channel_local_tests import confirmed_models
    observed_models = confirmed_models(dist) if item.snapshot.get('test_model_source') == 'remote_snapshot' else None
    allowed_models = dist.models if observed_models is None else observed_models
    models = selected_test_models(item.snapshot)
    if (not isinstance(models, list) or not models or len(models) > 200
            or any(not isinstance(model, str) or model not in (allowed_models or []) for model in models)):
        raise WriteStopped('所选测试模型已不属于此站点分发，请重新选择')
    fmt = db.get(CredentialFormat, channel.format_id, populate_existing=True)
    category = db.get(Category, channel.category_id, populate_existing=True)
    if (not supported_format(category, fmt) or fmt.version != item.snapshot.get('format_version')
            or fmt.code != item.snapshot.get('format_code')
            or channel.category_id != item.snapshot.get('category_id')
            or channel.key_mode != item.snapshot.get('key_mode')):
        raise WriteStopped('渠道分类、格式或凭据模式已改变')
    try:
        if protocol_schema(fmt.schema_config) != protocol_schema(item.snapshot['format_schema']):
            raise ValueError
        part, inputs = local_inputs(channel, fmt, dist, item.snapshot['partition_adapter'], category, models=observed_models)
        if inputs != item.snapshot['test_inputs'] or len(part['entries']) != item.snapshot['key_count']:
            raise ValueError
        if 'test_key_indices' in item.snapshot:
            indices = item.snapshot['test_key_indices']
            if (not isinstance(indices, list) or any(type(index) is not int for index in indices)
                    or indices != [index + 1 for index in part['indices']]):
                raise ValueError
    except (ValueError, KeyError, TypeError):
        raise WriteStopped('本地凭据、模型配置或代理已改变，请重新发起测试') from None
    version = db.scalar(select(KeyVersion).where(KeyVersion.channel_id == channel.id,
        KeyVersion.version == item.key_version).execution_options(populate_existing=True))
    if not version or version.fingerprint != channel.fingerprint or version.key_encrypted != channel.key_encrypted:
        raise WriteStopped('本地凭据版本记录不匹配，停止测试')
    return part, fmt


def execute_local_test(db, task, item, lease_check):
    from . import local_model_probe
    from .adapters.silicon import RemoteError
    from .worker import assert_execution, lock_result_distribution, reconcile_item

    if item.remote_write_attempted:
        reconcile_item(db, None, item, lock_result_distribution(db, item))
        return
    actor, _ = assert_execution(db, task, item)
    part, fmt = assert_local_test(db, task, item, actor)
    channel_scope = item.snapshot.get('test_scope') == 'channel'
    if channel_scope:
        rows = []
        execution_plan = {}
        for model_plan in part['channel_plan']:
            rows.extend(initial_model_results([model_plan['model']], [key['key_index'] for key in model_plan['keys']]))
            execution_plan[model_plan['model']] = []
            for key in model_plan['keys']:
                source_part = part['source_parts'][key['source']]
                entry = source_part['entries'][source_part['indices'].index(key['key_index'] - 1)]
                source = item.snapshot['test_sources'][key['source']]['snapshot']
                execution_plan[model_plan['model']].append((entry, source['test_inputs']['channel_config'], key['conflict']))
    else:
        # Only an unsent, fully revalidated legacy task can gain a new Key plan.
        indices = [index + 1 for index in part['indices']]
        item.snapshot = {**item.snapshot, 'test_key_indices': indices}
        rows = initial_model_results(selected_test_models(item.snapshot), indices)
        execution_plan = {row['model']: [(entry, item.snapshot['test_inputs']['channel_config'], False)
                                        for entry in part['entries']] for row in rows}
    current_model, current_key_index = None, None

    def persist_progress():
        dist = lock_result_distribution(db, item)
        item.snapshot = {**item.snapshot, 'test_progress': progress_result(rows, current_model, current_key_index)}
        record_observation(dist, item, status='running')
        db.commit()

    def before_request():
        lease_check()
        assert_execution(db, task, item, write=True)
        # One explicit task may need OAuth plus inference for each container
        # member. A restarted task never enters this loop after its sent flag.
        item.stage, item.remote_write_attempted = 'test_sent', True
        persist_progress()
        lock_result_distribution(db, item)
        if channel_scope:
            from .channel_local_tests import lock_channel_sources
            lock_channel_sources(db, item)
        lease_check()
        assert_execution(db, task, item, write=True)

    all_errors = []
    for row in rows:
        lease_check()
        assert_execution(db, task, item, write=True)
        current_model, row['status'] = row['model'], 'running'
        persist_progress()
        errors, failure_http_status = [], None
        provider_messages = {'failed': [], 'succeeded': []}
        entries = execution_plan[row['model']]
        for (entry, config, conflict), key_row in zip(entries, row['key_results'], strict=True):
            lease_check()
            assert_execution(db, task, item, write=True)
            current_key_index, key_row['status'] = key_row['key_index'], 'running'
            persist_progress()
            options = {'content': item.snapshot['test_content']} if item.snapshot.get('test_content') else {}
            outcome = ({'success': False, 'error_code': 'source_configuration_conflict'} if conflict else
                       local_model_probe.probe_credential(entry['key'], fmt.schema_config, row['model'], config,
                           proxy=entry['proxy'], before_request=before_request, **options))
            lock_result_distribution(db, item)
            lease_check()
            assert_execution(db, task, item, write=True)
            if not isinstance(outcome, dict) or type(outcome.get('success')) is not bool:
                raise RemoteError('本地测试未返回有效结果，请重新发起测试', unknown=True, category='protocol_error')
            row['tested_count'] += 1
            row['passed_count'] += int(outcome['success'])
            row['failed_count'] += int(not outcome['success'])
            duration = outcome.get('latency_ms')
            if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 <= duration < 86400000:
                row['latency_ms'] = (row['latency_ms'] or 0) + duration
            else:
                duration = None
            http_status = outcome.get('http_status')
            if type(http_status) is not int or not 100 <= http_status <= 599:
                http_status = None
            message = '所选模型连接成功'
            if not outcome['success']:
                failure_http_status = failure_http_status or http_status
                code = outcome.get('error_code')
                message = getattr(local_model_probe, 'ERROR_MESSAGES', {}).get(code, '所选模型连通性测试未通过')
                if conflict:
                    from .channel_local_tests import CONFLICT_MESSAGE
                    message = CONFLICT_MESSAGE
                if message not in errors:
                    errors.append(message)
                if message not in all_errors:
                    all_errors.append(message)
            key_row.update(status='succeeded' if outcome['success'] else 'failed', message=message,
                           provider_message=provider_message(outcome), latency_ms=duration, http_status=http_status)
            current_key_index = None
            row['http_status'] = failure_http_status if row['failed_count'] else http_status
            row['provider_message'] = merge_provider_message(provider_messages, outcome, failed=bool(row['failed_count']))
            row['message'] = '；'.join(errors)
            persist_progress()
        row['status'] = 'failed' if row['failed_count'] else 'succeeded'
        row['message'] = '；'.join(errors) if errors else '所选模型连接成功'
        if len(entries) > 1:
            row['message'] = (f"已测试 {len(entries)} 个密钥，{row['passed_count']} 个通过，{row['failed_count']} 个失败。"
                              + row['message'])
        current_model = None
        persist_progress()
    result = progress_result(rows)
    message = '所选模型连接成功' if not result['failed_count'] else '；'.join(all_errors)
    if len(rows) == 1:
        message = rows[0]['message']
    else:
        passed_models = sum(row['status'] == 'succeeded' for row in rows)
        message = f'已测试 {len(rows)} 个模型，{passed_models} 个通过，{len(rows) - passed_models} 个失败。' + message
    result.update(success=result['failed_count'] == 0, tested_at=utcnow().isoformat() + 'Z', message=message)
    item.snapshot = {**item.snapshot, 'operation_result': result}
    dist = lock_result_distribution(db, item)
    record_observation(dist, item, status='succeeded' if result['success'] else 'failed')
    db.commit()
    if result['failed_count']:
        raise RemoteError(message)
