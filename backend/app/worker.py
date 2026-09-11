"""PostgreSQL queue with Redis per-site leases; safe recovery after process loss.

Run with ``python -m app.worker``. The DB is authoritative. A failed Redis lock
service pauses remote operations, while submissions remain safely persisted.
"""
import logging
import signal
import threading
import time
from datetime import timedelta

from fastapi import HTTPException
from redis import Redis, RedisError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .adapters import get_adapter, supports_adapter
from .adapters.channel_observation import extract_usage, normalize_usage_observation
from .adapters.silicon import RemoteError, public_remote, validate_delete_identity
from .auth import assert_owner, audit, scope_owner_ids
from .catalog_policy import catalog_configuration_issues
from .channel_service import new_task, refresh_task, supported_format, target_issues
from .config import settings
from .credential_containers import (
    multikey_supported,
    partition_for,
    partition_snapshot,
    primary_credential,
    wire_keys,
)
from .db import SessionLocal, utcnow
from .distribution_monitoring import (
    MONITOR_OPERATIONS,
    monitoring_snapshot,
    record_observation,
    record_sync_observation,
)
from .distribution_tombstones import locally_deleted
from .models import Category, CredentialFormat, Site, SiteUploadTemplate, User
from .models_channels import (
    Channel,
    Distribution,
    DistributionVersion,
    KeyVersion,
    Task,
    TaskItem,
    UnclaimedChannel,
)
from .newapi_formats import credential_wire_schema, format_spec, protocol_schema
from .remote_channel_status import remote_channel_state
from .security import decrypt, fingerprint
from .site_remote_identity import same_deployment_site_ids
from .upload_templates import intersect_template_models, template_config

log = logging.getLogger('keyacross.worker')
STOP = threading.Event()
LEASE_SECONDS = 120
DEFAULT_SYNC_INTERVAL_SECONDS = 300
_SCHEDULER_LOCK = 713947215164


class WriteStopped(Exception):
    pass


class WorkRemoved(WriteStopped):
    """The queue entry or its source was hard-deleted; nothing may be persisted."""


class ConfirmedToggleMismatch(RemoteError):
    """A known opposite status was read back; only an explicit retry may write again."""


def assert_execution(db, task, item, *, write=False):
    """Refresh the actor and object, including immediately before every HTTP write."""
    with db.no_autoflush:
        if not db.scalar(select(TaskItem.id).where(TaskItem.id == item.id)):
            raise WorkRemoved('本地任务步骤已彻底删除，停止处理')
        task = db.get(Task, item.task_id, populate_existing=True)
        if not task:
            raise WorkRemoved('本地任务已彻底删除，停止处理')
        if item.distribution_id:
            dist = db.get(Distribution, item.distribution_id, populate_existing=True)
            if not dist:
                raise WorkRemoved('本地分发已彻底删除，停止处理')
            if locally_deleted(dist) and item.operation not in ('force_delete', 'test'):
                raise WriteStopped('此分发已强制移除本地关联，远端状态未确认；旧任务已停止')
    actor = db.get(User, task.execution_actor_id or task.actor_id, populate_existing=True)
    site = db.get(Site, item.site_id, populate_existing=True)
    if not actor or not actor.active or actor.archived or actor.session_version != task.actor_session_version:
        raise WriteStopped('发起账号停用、归档或会话权限已撤销')
    if item.operation == 'test':
        from .local_test_tasks import assert_local_test
        assert_local_test(db, task, item, actor)
        return actor, site
    if not site or (site.archived and item.operation != 'force_delete'):
        raise WriteStopped('站点已归档')
    if item.snapshot.get('site_base_url') != site.base_url or item.snapshot.get('seller_user_id') != str(site.seller_user_id) or item.snapshot.get('site_adapter') != site.adapter:
        raise WriteStopped('站点连接配置已改变，旧任务已停止')
    local_purge_only = item.operation == 'force_delete' and item.snapshot.get('delete_acknowledged') is True
    if 'site_build_id' in item.snapshot and not local_purge_only:
        from .adapters.newapi_builds import build_snapshot
        if item.snapshot['site_build_id'] != build_snapshot(site).get('site_build_id'):
            raise WriteStopped('站点构建与冻结任务不匹配，请重新验证并准备任务')
    if not supports_adapter(site.adapter):
        raise WriteStopped('站点适配器未实现')
    if write and item.operation == 'create':
        template_id = item.snapshot.get('upload_template_id')
        if template_id:
            template = db.get(SiteUploadTemplate, template_id, populate_existing=True)
            if not template or not template.enabled or template_config(template) != item.snapshot.get('template_config'):
                raise WriteStopped('上传模板已停用、删除或修改，旧创建任务已停止；请重新核对模板')
            if template.site_id != site.id or template.version != item.snapshot.get('template_version'):
                raise WriteStopped('上传模板目标或版本不匹配')
        elif item.snapshot.get('routing_group', 'default') != (getattr(site, 'routing_group', None) or 'default'):
            raise WriteStopped('站点路由组已改变，旧创建任务已停止；请重新核对分发配置')
    if item.channel_id:
        channel = db.get(Channel, item.channel_id, populate_existing=True)
        if not channel:
            raise WriteStopped('本地渠道不存在')
        try:
            assert_owner(db, actor, channel.owner_id)
        except HTTPException:
            raise WriteStopped('执行人已没有此渠道的本地管理权限') from None
        if write and item.operation != 'force_delete' and (channel.archived or channel.key_version != item.key_version):
            raise WriteStopped('渠道已归档或凭据版本已改变')
        if item.operation in ('delete_remote', 'force_delete'):
            dist = db.get(Distribution, item.distribution_id, populate_existing=True)
            target = item.snapshot.get('delete_target') or {}
            if (not dist or dist.channel_id != channel.id or dist.site_id != site.id
                    or (item.operation == 'delete_remote' and not target.get('id'))
                    or (str(dist.remote_id) if dist.remote_id else None) != target.get('id')
                    or dist.remote_name != target.get('name')):
                raise WriteStopped('远端渠道关联已改变或缺少删除确认快照，请重新确认删除范围')
        if item.operation in MONITOR_OPERATIONS:
            dist = db.get(Distribution, item.distribution_id, populate_existing=True)
            target = item.snapshot.get('operation_target') or {}
            if (not dist or dist.channel_id != channel.id or dist.site_id != site.id
                    or not target.get('id') or str(dist.remote_id) != target['id']
                    or dist.remote_name != target.get('name') or dist.status == 'deleted'):
                raise WriteStopped('远端渠道关联已改变，请刷新后重新发起操作')
            if item.operation == 'test' and item.snapshot.get('test_model') not in (dist.models or []):
                raise WriteStopped('所选测试模型已不属于此站点分发，请重新选择')
        if write and item.operation != 'force_delete':
            fmt = db.get(CredentialFormat, channel.format_id, populate_existing=True)
            category = db.get(Category, channel.category_id, populate_existing=True)
            dist = db.get(Distribution, item.distribution_id, populate_existing=True)
            if (not dist or dist.channel_id != channel.id or dist.site_id != site.id
                    or dist.partition_key != item.snapshot.get('partition_key', '')):
                raise WriteStopped('远端分发关联或密钥分区不匹配，已停止写入')
            if ('bedrock_api_key_sdk' in dist.template_snapshot
                    and item.snapshot.get('bedrock_api_key_sdk', False) != dist.template_snapshot['bedrock_api_key_sdk']):
                raise WriteStopped('Bedrock 分发接入协议与冻结记录不一致')
            if item.snapshot.get('bedrock_api_key_sdk'):
                from .adapters.bedrock_capabilities import bedrock_api_key_sdk_mode
                if not bedrock_api_key_sdk_mode(site.adapter, (site.capabilities or {}).get('verified_version')):
                    raise WriteStopped('站点版本已不匹配冻结的 Bedrock SDK 接入协议')
            if getattr(channel, 'key_mode', 'single') != item.snapshot.get('key_mode', 'single'):
                raise WriteStopped('密钥容器模式已改变，旧任务已停止')
            if channel.key_mode == 'multiple':
                try:
                    current_part = partition_snapshot(channel, fmt, item.snapshot.get('partition_key', ''), site=site)
                except ValueError:
                    raise WriteStopped('密钥容器分区已改变，旧任务已停止') from None
                if any(item.snapshot.get(key) != value for key, value in current_part.items()):
                    raise WriteStopped('密钥容器成员或分区快照已改变，旧任务已停止')
                if current_part['key_count'] > 1 and not multikey_supported(site):
                    raise WriteStopped('站点尚未验证多密钥容器支持')
            if not supported_format(category, fmt) or fmt.version != item.snapshot.get('format_version'):
                raise WriteStopped('渠道格式已停用或版本不再匹配')
            if item.snapshot.get('format_code', fmt.code) != fmt.code:
                raise WriteStopped('渠道格式标识已改变，旧任务已停止')
            if 'format_schema' in item.snapshot:
                try:
                    unchanged = protocol_schema(fmt.schema_config) == protocol_schema(item.snapshot['format_schema'])
                except ValueError:
                    unchanged = False
                if not unchanged:
                    raise WriteStopped('渠道格式定义已改变，旧任务已停止')
            if item.operation == 'edit' and 'models' in item.snapshot.get('changes', {}):
                from .google_services import service_model_issues
                models = item.snapshot['changes']['models']
                models = models.split(',') if isinstance(models, str) else models
                if issues := service_model_issues(fmt.schema_config, models):
                    raise WriteStopped('；'.join(issues))
            if item.operation in ('create', 'rotate') and (fmt.schema_config or {}).get('type') in ('vertex_gemini', 'vertex_claude'):
                from .google_services import service_model_issues
                expected_wire = credential_wire_schema(primary_credential(channel, fmt, item.snapshot.get('partition_key', ''), site=site), fmt.schema_config)
                if expected_wire != item.snapshot.get('wire_format_schema'):
                    raise WriteStopped('Vertex AI 认证方式快照与密钥不匹配，已停止写入')
                if issues := service_model_issues(fmt.schema_config, item.snapshot.get('models', [])):
                    raise WriteStopped('；'.join(issues))
                if (fmt.schema_config['type'] == 'vertex_claude' and expected_wire['type'] == 'vertex_api_key'
                        and (site.capabilities or {}).get('vertex_claude_api_key') != 'supported'):
                    raise WriteStopped('站点版本尚未验证 Vertex Claude API Key 支持')
            if item.operation in ('create', 'rotate') and (fmt.schema_config or {}).get('type') in ('azure_gpt', 'azure_claude'):
                from .azure_credentials import azure_credential_fields
                fields = azure_credential_fields(primary_credential(channel, fmt, item.snapshot.get('partition_key', ''), site=site), fmt.schema_config['type'])
                if (item.snapshot.get('credential_channel_config') != fields['channel_config']
                        or item.snapshot.get('wire_format_schema') != fields['credential_format']):
                    raise WriteStopped('Azure 资源或协议快照不匹配，已停止写入')
                if item.operation == 'create' and any(item.snapshot.get('channel_config', {}).get(key) != value
                        for key, value in fields['channel_config'].items()):
                    raise WriteStopped('Azure 官方地址或版本快照不匹配，已停止创建')
            if item.operation == 'create' and (issues := catalog_configuration_issues(category, item.snapshot.get('channel_config'))):
                raise WriteStopped('；'.join(issues))
            if item.operation == 'create' and channel.upload_mode == 'template' and not item.snapshot.get('upload_template_id'):
                raise WriteStopped('模板渠道缺少创建模板快照')
            if item.operation == 'create' and item.snapshot.get('upload_template_id'):
                if category.family == 'Google':
                    from .upload_templates import template_variant
                    template_fmt = db.get(CredentialFormat, template.format_id)
                    if (template_variant(category, fmt, channel.models) !=
                            template_variant(category, template_fmt, template.models)):
                        raise WriteStopped('Google 服务与目标模板用途不一致，已停止创建')
                if (channel.upload_settings or {}) != item.snapshot.get('upload_settings_snapshot', {}):
                    raise WriteStopped('渠道上传参数已改变，旧创建任务已停止')
                try:
                    expected_models = intersect_template_models(template.models, (channel.upload_settings or {}).get('models'))
                except (ValueError, AttributeError):
                    raise WriteStopped('模板或 Key 模型范围格式无效，已停止创建') from None
                if not expected_models or item.snapshot.get('models') != expected_models:
                    raise WriteStopped('任务模型快照与模板和 Key 模型交集不一致，已停止创建')
                if (fingerprint(channel.proxy_encrypted) if channel.proxy_encrypted else None) != item.snapshot.get('channel_proxy_fingerprint'):
                    raise WriteStopped('渠道代理已改变，旧创建任务已停止')
                encrypted_proxy = (channel.proxy_encrypted if item.snapshot.get('proxy_source') == 'channel'
                                   else template.proxy_encrypted if item.snapshot.get('proxy_source') == 'template' else None)
                if channel.key_mode == 'multiple' and item.snapshot.get('proxy_source') == 'partition':
                    part = partition_for(channel, fmt, item.snapshot['partition_key'], site=site)
                    if (decrypt(item.proxy_encrypted) if item.proxy_encrypted else '') != part['entries'][0]['proxy']:
                        raise WriteStopped('密钥分区代理与任务不匹配')
                    encrypted_proxy = item.proxy_encrypted
                if (fingerprint(encrypted_proxy) if encrypted_proxy else None) != item.snapshot.get('proxy_fingerprint'):
                    raise WriteStopped('渠道代理已改变，旧创建任务已停止')
                if (fingerprint(item.proxy_encrypted) if item.proxy_encrypted else None) != item.snapshot.get('proxy_fingerprint'):
                    raise WriteStopped('任务代理快照不匹配，旧创建任务已停止')
    else:
        allowed = scope_owner_ids(db, actor)
        if any(owner not in allowed for owner in item.snapshot.get('owner_ids', [])):
            raise WriteStopped('同步任务的本地授权范围已改变')
    if (write or item.operation in ('sync_usage', 'force_delete')) and task.cancelled:
        raise WriteStopped('任务已取消，未开始的写步骤已停止')
    if write and not site.enabled and item.operation != 'force_delete':
        raise WriteStopped('站点已停用，未开始的写步骤已停止')
    return actor, site


def record_remote(db, dist, remote, key_version=None):
    # A remote read may have started before local removal. Refresh under a row
    # lock before overwriting status/JSON; stale ORM state is never authoritative.
    if dist is None:
        raise WorkRemoved('本地分发已彻底删除，忽略远端晚到结果')
    with db.no_autoflush:
        dist = db.scalar(select(Distribution).where(Distribution.id == dist.id)
                         .with_for_update().execution_options(populate_existing=True))
    if not dist:
        raise WorkRemoved('本地分发已彻底删除，忽略远端晚到结果')
    if locally_deleted(dist):
        raise WriteStopped('此分发已强制移除本地关联，不再同步远端状态')
    if dist.remote_id != str(remote['id']):
        from .remote_cleanup import is_reserved, lock_target
        lock_target(db, dist.site_id, str(remote['id']))
        if is_reserved(db, dist.site_id, str(remote['id'])):
            raise WriteStopped('此远端渠道正在等待清理，不能关联到新的本地渠道')
        if db.scalar(select(Distribution.id).where(
                Distribution.site_id.in_(same_deployment_site_ids(dist.site_id)),
                Distribution.remote_id == str(remote['id']), Distribution.id != dist.id).limit(1)):
            raise WriteStopped('此远端渠道已有同一部署的历史本地关联，不能重复关联')
    dist.remote_id = str(remote['id'])
    dist.remote_name = str(remote.get('name', dist.remote_name))[:160]
    dist.models = [m for m in str(remote.get('models', '')).split(',') if m]
    dist.routing_group = str(remote.get('group', 'default'))
    dist.status = remote_channel_state(remote.get('status'))['status']
    observations = monitoring_snapshot(dist)
    dist.remote_snapshot = {**public_remote(remote), **({'_monitoring': observations} if observations else {})}
    dist.last_sync_at, dist.error = utcnow(), None
    if key_version is not None:
        latest = db.scalar(select(DistributionVersion).where(DistributionVersion.distribution_id == dist.id,
                                                            DistributionVersion.valid_to.is_(None)))
        if not latest or latest.key_version != key_version:
            if latest:
                latest.valid_to = utcnow()
            db.add(DistributionVersion(distribution_id=dist.id, key_version=key_version))
        dist.key_version = key_version


def lock_result_distribution(db, item, *, refresh=True, stop=True):
    """Fence a late response after another transaction removed this partition."""
    # The old session may contain a stale JSON snapshot or a task outcome that
    # would undo cancellation. Lock without first autoflushing either object.
    with db.no_autoflush:
        if not db.scalar(select(TaskItem.id).where(TaskItem.id == item.id).with_for_update()):
            raise WorkRemoved('本地任务步骤已彻底删除，忽略远端晚到结果')
        if not db.scalar(select(Task.id).where(Task.id == item.task_id)):
            raise WorkRemoved('本地任务已彻底删除，忽略远端晚到结果')
        if not item.distribution_id:
            return None
        row = db.execute(select(Distribution.remote_snapshot).where(
            Distribution.id == item.distribution_id).with_for_update()).one_or_none()
        if row is None:
            raise WorkRemoved('本地分发已彻底删除，忽略远端晚到结果')
        dist = db.get(Distribution, item.distribution_id)
        marker = (row[0] or {}).get('_local_deletion')
        removed = (isinstance(marker, dict) and marker.get('remote_confirmed') is False
                   and item.operation not in ('force_delete', 'test'))
        if dist and (refresh or removed):
            db.refresh(dist)
        if removed:
            db.refresh(item)
            if stop:
                raise WriteStopped('此分发已强制移除本地关联，已忽略远端晚到结果')
    return dist


def validate_created(remote, item, dist, adapter=None):
    expected = {'name': dist.remote_name, 'type': item.snapshot.get('channel_type', 1), 'models': ','.join(item.snapshot['models']),
                'group': item.snapshot.get('routing_group', 'default'),
                'status': (item.snapshot.get('channel_config') or {}).get('status', 2)}
    if any(remote.get(k) != v for k, v in expected.items()):
        raise RemoteError('远端创建后的配置与提交快照不同，需人工核实', unknown=True)
    if item.snapshot.get('key_count', 1) > 1:
        from .adapters.channel_config import validate_multikey_readback
        validate_multikey_readback(remote, item.snapshot['key_count'])
    if (item.snapshot.get('config_explicit') or item.snapshot.get('channel_type', 1) != 1) and callable(
            validator := getattr(adapter, 'validate_created_config', None)):
        acknowledged_validator = getattr(adapter, 'validate_acknowledged_create_config', None)
        if (getattr(item, 'operation', None) == 'create'
                and getattr(item, 'stage', None) == 'created_pending_verification'
                and getattr(item, 'remote_write_attempted', False) is True
                and callable(acknowledged_validator)):
            validator = acknowledged_validator
        validator(remote, config={**(item.snapshot.get('channel_config') or {}),
                                  'credential_format': item.snapshot.get('wire_format_schema', item.snapshot.get('format_schema', {}))},
                  proxy=decrypt(item.proxy_encrypted) if item.proxy_encrypted else '',
                  channel_type=item.snapshot.get('channel_type', 1))


def reconcile_item(db, adapter, item, dist):
    if item.operation == 'test':
        result = item.snapshot.get('operation_result')
        if isinstance(result, dict) and type(result.get('success')) is bool:
            if not result['success']:
                raise RemoteError('所选模型连通性测试未通过；如需再次测试，请在渠道详情重新发起')
            return
        raise RemoteError('模型测试已发送但结果未能确认；不会自动重复测试，请在渠道详情决定是否重新发起', unknown=True)
    with db.no_autoflush:
        dist = db.get(Distribution, dist.id, populate_existing=True)
    if not dist:
        raise WorkRemoved('本地分发已彻底删除，不再核实旧任务')
    if locally_deleted(dist) and item.operation != 'test':
        raise WriteStopped('此分发已强制移除本地关联，不再核实旧任务')
    if item.operation == 'create':
        if (item.snapshot.get('reupload') is True
                and item.snapshot.get('reupload_create_acknowledged') is not True):
            raise RemoteError('本次重新上传尚未确认创建成功，不能把同名远端关联为当前密钥；请人工核实', unknown=True)
        remote = adapter.detail(dist.remote_id) if dist.remote_id else adapter.find_unique_name(dist.remote_name)
        if not remote:
            raise RemoteError('按稳定名称未找到已创建渠道，保持待核实；不会自动重复新增', unknown=True)
        validate_created(remote, item, dist, adapter)
        linked = db.scalar(select(Distribution.id).where(Distribution.site_id == dist.site_id,
                                                        Distribution.remote_id == str(remote['id']), Distribution.id != dist.id))
        if linked:
            raise RemoteError('远端渠道已存在其他本地关联，需人工核实', unknown=True)
        record_remote(db, dist, remote, item.key_version)
        return
    if not dist.remote_id:
        raise RemoteError('没有远端编号可用于核实', unknown=True)
    if item.operation == 'rotate' and item.stage == 'updated_pending_verification' and dist.key_version == item.key_version:
        # A successful update acknowledgement and applied version were committed before readback.
        record_remote(db, dist, adapter.detail(dist.remote_id))
        return
    if item.operation == 'delete_remote':
        if item.snapshot.get('delete_acknowledged') is True and dist.status == 'deleted':
            return
        read_delete_target(adapter, item, dist)
        # A read-only check can prove it still exists. Absence cannot distinguish
        # deletion from lost visibility, so it never constitutes a success.
        raise RemoteError('已核实原远端渠道仍存在；本项可在重新确认后重试删除')
    if item.operation == 'rotate':
        # The seller contract has no credential digest.
        raise RemoteError('该写入结果不能由现有卖家契约可靠核实，需人工处理', unknown=True)
    remote = adapter.detail(dist.remote_id)
    if item.operation in ('enable', 'disable'):
        desired = 'enabled' if item.operation == 'enable' else 'disabled'
        observed = remote_channel_state(remote.get('status'))['status']
        if observed != desired:
            record_remote(db, dist, remote)
            if observed == 'unavailable':
                raise RemoteError('远端渠道状态无法确认，保持待核实；不会重复发送启停请求', unknown=True)
            raise ConfirmedToggleMismatch('回读状态与目标不一致；本项已转为失败，可明确重试')
    elif item.operation == 'edit':
        if any(remote.get(k) != v for k, v in item.snapshot.get('changes', {}).items()):
            record_remote(db, dist, remote)
            raise RemoteError('回读配置与提交不同；本项已转为失败，请先核对远端配置')
    record_remote(db, dist, remote)


def read_delete_target(adapter, item, dist):
    try:
        remote = adapter.detail(dist.remote_id)
    except RemoteError as exc:
        # Neither a missing seller list entry nor an ambiguous detail response
        # proves deletion. Keep the confirmation for read-only reconciliation.
        raise RemoteError('无法核实远端渠道是否仍存在或当前账号是否有权查看，未确认删除完成',
                          unknown=True, category=exc.category, status_code=exc.status_code) from None
    validate_delete_identity(remote, item.snapshot.get('delete_target'))
    return remote


def execute_item(db: Session, item: TaskItem, lease_check=lambda: None):
    """One independently durable step; injectable lease checker for deterministic tests."""
    if item.operation == 'remote_cleanup':
        from .remote_cleanup import execute_cleanup
        execute_cleanup(db, item, lease_check)
        return
    if (item.operation == 'force_delete' and item.channel_id is None and item.distribution_id is None
            and item.snapshot.get('operation_result', {}).get('remote_confirmed') is True):
        return  # Cleanup committed; only the queue's final status remains to publish.
    task = db.get(Task, item.task_id)
    actor, site = assert_execution(db, task, item)
    dist = db.get(Distribution, item.distribution_id) if item.distribution_id else None
    if item.operation == 'test':
        from .local_test_tasks import execute_local_test
        execute_local_test(db, task, item, lease_check)
        return

    def before_write():
        lease_check()
        assert_execution(db, task, item, write=True)
        if item.operation == 'test' and item.remote_write_attempted:
            raise WriteStopped('此模型测试已经发送，停止重复请求')
        # This callback runs inside the adapter immediately before the actual unsafe
        # request, after permissions and metadata reads have completed successfully.
        item.stage = 'create_sent' if item.operation == 'create' else 'test_sent' if item.operation == 'test' else 'write_sent'
        item.remote_write_attempted = True
        db.commit()
        # Persist the uncertain-write stage first, then retain row locks across
        # the HTTP request. A paused process must not send after force removal
        # has returned; a dead process releases these locks automatically.
        lock_result_distribution(db, item)

    adapter = get_adapter(site, before_write=before_write)
    from .adapters.new_api import NewAPIAdapter
    if isinstance(adapter, NewAPIAdapter):
        adapter.frozen_bedrock_api_key_sdk = item.snapshot.get('bedrock_api_key_sdk', False)
    if 'site_build_id' in item.snapshot:
        adapter.frozen_build_id = item.snapshot['site_build_id']
    if item.operation == 'sync':
        sync_site(db, adapter, task, item, actor, site)
        return
    if not dist:
        raise RemoteError('本地分发记录不存在')
    if item.operation == 'force_delete':
        from .force_deletion import execute_force_delete
        execute_force_delete(db, adapter, task, item, dist, actor, lease_check)
        return
    if item.stage in ('create_sent', 'write_sent', 'test_sent', 'created_pending_verification', 'updated_pending_verification', 'reconcile'):
        reconcile_item(db, adapter, item, dist)
        return
    if item.operation == 'sync_usage':
        lease_check()
        values = adapter.usage(dist.remote_id, expected=item.snapshot['operation_target'])
        dist = lock_result_distribution(db, item)
        lease_check()
        assert_execution(db, task, item)
        # Re-normalize the public observation instead of persisting arbitrary
        # provider fields or treating a remote counter as an invoice fact.
        observed = normalize_usage_observation(values)
        item.snapshot = {**item.snapshot, 'operation_result': {
            'remote_usage': observed, 'synced_at': utcnow().isoformat() + 'Z',
            'message': '已同步远端原始消耗状态（未作为结算依据）'}}
        dist.last_sync_at = utcnow()
        record_observation(dist, item, status='succeeded')
        db.commit()
        return
    lease_check()
    assert_execution(db, task, item, write=True)
    channel = db.get(Channel, item.channel_id)
    fmt = db.get(CredentialFormat, channel.format_id)
    category = db.get(Category, channel.category_id)
    if not supported_format(category, fmt) or fmt.version != item.snapshot.get('format_version'):
        raise WriteStopped('渠道格式已停用或版本不再匹配')
    if item.operation == 'create':
        issues = target_issues(site, fmt, item.snapshot['models'], proxy=item.snapshot.get('proxy_requested', False),
                               rpm=item.snapshot.get('rpm_enabled', False), strategy=item.snapshot.get('enable_strategy', 'disabled'),
                               remark_length=len(item.snapshot.get('remark', '')),
                               routing_group=item.snapshot.get('routing_group'),
                               channel_config=item.snapshot.get('channel_config'), wire_schema=item.snapshot.get('wire_format_schema'))
        if issues:
            raise RemoteError('；'.join(issues))
        if dist.remote_id:
            reconcile_item(db, adapter, item, dist)
            return
        # Check stable name BEFORE first write too. A collision never triggers another POST.
        found = adapter.find_unique_name(dist.remote_name)
        if found:
            if item.snapshot.get('reupload') is True:
                # The current key may differ from an earlier failed attempt.
                # Matching public configuration cannot prove which key exists.
                raise RemoteError('远端已存在同名渠道，需核实原创建结果；未重新上传或覆盖密钥', unknown=True)
            validate_created(found, item, dist, adapter)
            if db.scalar(select(Distribution.id).where(Distribution.site_id == site.id, Distribution.remote_id == str(found['id']), Distribution.id != dist.id)):
                raise RemoteError('稳定名称对应远端渠道已有本地关联', unknown=True)
            record_remote(db, dist, found, item.key_version)
            return
        version = db.scalar(select(KeyVersion).where(KeyVersion.channel_id == channel.id, KeyVersion.version == item.key_version))
        if not version:
            raise RemoteError('任务凭据版本不存在')
        options = {}
        if item.snapshot.get('key_mode') == 'multiple' or item.snapshot.get('config_explicit') or format_spec(fmt)['wire_code'] != 'api_key-v1':
            proxy = item.proxy_encrypted if item.snapshot.get('upload_template_id') else channel.proxy_encrypted
            options = {'channel_type': (fmt.schema_config or {}).get('remote_type', 1),
                       'config': {**(item.snapshot.get('channel_config') or {}),
                                  'credential_format': item.snapshot.get('wire_format_schema', fmt.schema_config)},
                       'proxy': decrypt(proxy) if proxy else ''}
        part = partition_for(channel, fmt, item.snapshot.get('partition_key', ''), ciphertext=version.key_encrypted, site=site)
        keys = wire_keys(part, fmt)
        remote_key = keys[0]
        if len(keys) > 1:
            options['keys'] = keys
        remote_id = adapter.create(name=dist.remote_name, key=remote_key,
                                   models=item.snapshot['models'], remark=item.snapshot['remark'],
                                   group=item.snapshot.get('routing_group', 'default'), **options)
        dist = lock_result_distribution(db, item)
        if item.snapshot.get('reupload') is True:
            item.snapshot = {**item.snapshot, 'reupload_create_acknowledged': True}
        if remote_id:
            dist.remote_id = remote_id
            dist.status = 'created_pending_verification'
        item.stage = 'created_pending_verification'
        db.commit()
        # Missing ID means lookup, not a duplicate create.
        reconcile_item(db, adapter, item, dist)
        return
    if not dist.remote_id or dist.status == 'deleted':
        raise RemoteError('远端分发不存在或已删除')
    if item.operation in ('enable', 'disable'):
        adapter.toggle(dist.remote_id, item.operation == 'enable')
        dist = lock_result_distribution(db, item)
        item.stage = 'updated_pending_verification'
        db.commit()
        reconcile_item(db, adapter, item, dist)
    elif item.operation in ('edit', 'rotate'):
        key = None
        if item.operation == 'rotate':
            version = db.scalar(select(KeyVersion).where(KeyVersion.channel_id == channel.id, KeyVersion.version == item.key_version))
            if not version:
                raise RemoteError('任务凭据版本不存在')
            part = partition_for(channel, fmt, item.snapshot.get('partition_key', ''), ciphertext=version.key_encrypted, site=site)
            keys = wire_keys(part, fmt)
            from .adapters.channel_config import wire_key_text
            key = wire_key_text(keys, part['wire_format_schema']) if len(keys) > 1 else keys[0]
        expected = {k: v for k, v in item.snapshot.get('expected', {}).items() if k in ('name', 'models', 'group', 'type')}
        if item.operation == 'rotate' and (fmt.schema_config or {}).get('type') in ('azure_gpt', 'azure_claude'):
            expected.update(item.snapshot['credential_channel_config'])
        if item.operation == 'rotate' and (fmt.schema_config or {}).get('type') in ('vertex_gemini', 'vertex_claude'):
            from .newapi_formats import validate_vertex_authentication
            remote = adapter.detail(dist.remote_id)
            try:
                validate_vertex_authentication(remote, item.snapshot['wire_format_schema'])
            except ValueError as exc:
                raise RemoteError(str(exc), category='configuration_error') from None
            # edit() reads again and compares expected immediately before PUT.
            expected['settings'] = remote.get('settings')
        if item.operation == 'rotate' and (fmt.schema_config or {}).get('remote_type') == 33:
            from .adapters.channel_config import json_object
            remote = adapter.detail(dist.remote_id)
            schema = item.snapshot.get('wire_format_schema', fmt.schema_config)
            wanted = 'api_key' if schema.get('type') == 'aws_api_key' and not item.snapshot.get('bedrock_api_key_sdk') else 'ak_sk'
            try:
                actual = json_object(remote.get('settings')).get('aws_key_type', 'ak_sk')
            except (ValueError, TypeError):
                actual = None
            if actual != wanted:
                raise RemoteError('远端 Bedrock 认证方式已改变，停止更换密钥', category='configuration_error')
            expected['settings'] = remote.get('settings')
        options = {}
        if item.operation == 'rotate' and item.snapshot.get('key_count', 1) > 1:
            from .adapters.channel_config import validate_multikey_readback
            remote = adapter.detail(dist.remote_id)
            validate_multikey_readback(remote, item.snapshot['key_count'])
            expected['channel_info'] = remote['channel_info']
            options['multikey'] = True
        adapter.edit(dist.remote_id, changes=item.snapshot.get('changes', {}), key=key, expected=expected, **options)
        dist = lock_result_distribution(db, item)
        item.stage = 'updated_pending_verification'
        if item.operation == 'rotate':
            # Distinguish an acknowledged rotation from an unknown network write.
            previous = db.scalar(select(DistributionVersion).where(DistributionVersion.distribution_id == dist.id, DistributionVersion.valid_to.is_(None)))
            if previous and previous.key_version != item.key_version:
                previous.valid_to = utcnow()
            if not previous or previous.key_version != item.key_version:
                db.add(DistributionVersion(distribution_id=dist.id, key_version=item.key_version))
            dist.key_version = item.key_version
        db.commit()
        remote = adapter.detail(dist.remote_id)
        if any(remote.get(k) != v for k, v in item.snapshot.get('changes', {}).items()):
            raise RemoteError('远端编辑后回读配置与目标不同', unknown=True)
        record_remote(db, dist, remote, item.key_version if item.operation == 'rotate' else None)
        if item.operation == 'edit' and 'remark' in item.snapshot.get('changes', {}) and dist.upload_template_id:
            dist.template_snapshot = {**dist.template_snapshot, 'effective_remark': item.snapshot['changes']['remark']}
    elif item.operation == 'delete_remote':
        read_delete_target(adapter, item, dist)
        # The documented provider has no final-usage endpoint. Keep this explicit gap.
        dist.usage_status = 'final_usage_unavailable'
        db.commit()
        try:
            adapter.delete(dist.remote_id, expected=item.snapshot['delete_target'])
        except RemoteError as exc:
            if not item.remote_write_attempted and (exc.status_code == 404 or exc.category in ('business_error', 'protocol_error')):
                raise RemoteError('删除前再次核实远端渠道失败，未确认删除完成，请核实远端状态',
                                  unknown=True, category=exc.category, status_code=exc.status_code) from None
            raise
        dist = lock_result_distribution(db, item)
        dist.status, dist.error = 'deleted', None
        for history in db.scalars(select(DistributionVersion).where(DistributionVersion.distribution_id == dist.id, DistributionVersion.valid_to.is_(None))):
            history.valid_to = utcnow()
        # Preserve the explicit success acknowledgement across worker restarts;
        # a lost HTTP acknowledgement still stays unresolved and is never resent.
        item.snapshot = {**item.snapshot, 'delete_acknowledged': True}
        db.commit()
    else:
        raise RemoteError('尚未实现的任务操作')


def sync_site(db, adapter, task, item, actor, site):
    records = adapter.channels()
    conversion = None
    if callable(converter := getattr(adapter, 'usage_conversion', None)):
        try:
            conversion = converter(refresh=True)
        except RemoteError as exc:
            if (item.snapshot.get('site_build_id')
                    or exc.reason in ('build_changed', 'unrecognized_build', 'build_unavailable',
                                      'incompatible_interface', 'interface_changed', 'permissions_changed')
                    or exc.category in ('identity_mismatch', 'permission_denied', 'authentication_error')):
                raise
            # A quota read remains useful when conversion metadata is unavailable.
            # Interface and access checks must still pass before persisting it.
            # Never substitute a default ratio or label raw counters as USD.
    owners = item.snapshot.get('owner_ids', [])
    distributions = list(db.scalars(select(Distribution).join(Channel, Channel.id == Distribution.channel_id)
                                    .where(Distribution.site_id == site.id, Channel.owner_id.in_(owners), Distribution.remote_id.is_not(None))))
    by_id = {str(r['id']): r for r in records if isinstance(r, dict) and r.get('id') is not None}
    for dist in distributions:
        # A manual row action can be submitted while the remote list is read.
        # Lock and refresh before replacing JSON so its pending result survives.
        with db.no_autoflush:
            dist = db.scalar(select(Distribution).where(Distribution.id == dist.id)
                             .with_for_update().execution_options(populate_existing=True))
        if not dist or locally_deleted(dist):
            continue
        remote = by_id.get(dist.remote_id)
        if remote:
            # Sync observed remote state without modifying local desired configuration.
            record_remote(db, dist, remote)
            observed = extract_usage(remote, conversion=conversion)
            if any(observed.get(field) is not None for field in ('used_quota', 'balance', 'balance_updated_at')):
                record_sync_observation(dist, task.id, observed)
        elif dist.status != 'deleted':
            dist.status, dist.error = 'missing', '卖家列表中未找到；可能删除或权限改变，历史数据已保留'
    if item.snapshot.get('discover') and actor.role == 'superadmin':
        from .remote_cleanup import is_reserved, lock_target
        linked = set(db.scalars(select(Distribution.remote_id).where(
            Distribution.site_id.in_(same_deployment_site_ids(site.id)), Distribution.remote_id.is_not(None))))
        for remote_id, remote in by_id.items():
            if remote_id in linked:
                continue
            # Coordinate with local-first deletion/adoption. A read started
            # before purge must not rediscover its now-reserved remote target.
            try:
                lock_target(db, site.id, remote_id)
            except HTTPException:
                continue
            if is_reserved(db, site.id, remote_id):
                continue
            found = db.scalar(select(UnclaimedChannel).where(UnclaimedChannel.site_id == site.id, UnclaimedChannel.remote_id == remote_id))
            if found is None:
                found = UnclaimedChannel(site_id=site.id, remote_id=remote_id, remote_name=str(remote.get('name', ''))[:160])
                db.add(found)
            found.snapshot = public_remote(remote)
            found.last_seen_at = utcnow()
    site.last_sync_at, site.health = utcnow(), 'healthy'
    # No consumption data is synthesized from channel counts or personal request logs.


class SiteLease:
    def __init__(self, client, site_id, item_id):
        self.lock = client.lock('keyacross:site:' + site_id, timeout=LEASE_SECONDS, thread_local=False)
        self.item_id, self.stop, self.lost = item_id, threading.Event(), False
        self.thread = None

    def acquire(self):
        if not self.lock.acquire(blocking=False):
            return False
        self.thread = threading.Thread(target=self.renew, daemon=True)
        self.thread.start()
        return True

    def renew(self):
        while not self.stop.wait(20):
            try:
                self.lock.extend(LEASE_SECONDS, replace_ttl=True)
                with SessionLocal() as db:
                    item = db.get(TaskItem, self.item_id)
                    if item and item.status == 'running':
                        item.lease_until = utcnow() + timedelta(seconds=LEASE_SECONDS)
                        db.commit()
            except Exception:  # noqa: BLE001 - any lease renewal failure stops writes
                self.lost = True
                return

    def check(self):
        if self.lost or not self.lock.owned():
            raise WriteStopped('站点执行锁已失效，停止新的远端写入')

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)
        try:
            self.lock.release()
        except RedisError:
            pass


def recover_expired(db):
    items = list(db.scalars(select(TaskItem).where(TaskItem.status == 'running',
                          or_(TaskItem.lease_until.is_(None), TaskItem.lease_until < utcnow())).with_for_update(skip_locked=True)))
    for item in items:
        item.status, item.lease_until = 'pending', None
        if item.distribution_id and item.operation in MONITOR_OPERATIONS:
            record_observation(db.get(Distribution, item.distribution_id), item)
        # create_sent/write_sent stay intact and force read-only reconciliation.
        item.error = '执行器中断，恢复时将先核实已发出的远端写入'
        refresh_task(db, db.get(Task, item.task_id))
    db.commit()


def run_once(client=None):
    client = client or Redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
    with SessionLocal() as db:
        recover_expired(db)
        item = db.scalar(select(TaskItem).where(TaskItem.status == 'pending',
                          or_(TaskItem.next_attempt_at.is_(None), TaskItem.next_attempt_at <= utcnow()))
                         .order_by(TaskItem.created_at).with_for_update(skip_locked=True).limit(1))
        if not item:
            return False
        lease = SiteLease(client, item.site_id, item.id)
        try:
            if not lease.acquire():
                item.next_attempt_at = utcnow() + timedelta(seconds=2)
                db.commit()
                return False
        except RedisError:
            db.rollback()
            return False
        item.status, item.lease_until = 'running', utcnow() + timedelta(seconds=LEASE_SECONDS)
        item.attempts += 1
        item_id = item.id
        task = db.get(Task, item.task_id)
        if task is None:
            db.rollback()
            lease.close()
            return True
        task.status = 'running'
        if item.distribution_id and item.operation in MONITOR_OPERATIONS:
            record_observation(db.get(Distribution, item.distribution_id), item)
        db.commit()
        try:
            execute_item(db, item, lease.check)
            item.status, item.stage, item.error = 'succeeded', 'complete', None
        except WorkRemoved:
            # A purge owns removal of the queue/history. Never recreate its audit
            # or flush any stale ORM state after a remote request completes.
            db.rollback()
            item = None
        except WriteStopped as exc:
            item.status, item.error = 'cancelled', str(exc)
        except ConfirmedToggleMismatch as exc:
            # A verified opposite state is a definite mismatch, even after an
            # acknowledged write. The generic post-write guard below must not
            # turn this safe explicit-retry outcome back into needs_review.
            item.status, item.error = 'failed', str(exc)
        except RemoteError as exc:
            item.error = str(exc)
            if exc.unknown or item.stage in ('created_pending_verification', 'updated_pending_verification'):
                item.status = 'needs_review'
            elif exc.retryable and item.attempts < 3 and item.stage not in ('create_sent', 'write_sent', 'test_sent', 'reconcile'):
                item.status = 'pending'
                item.next_attempt_at = utcnow() + timedelta(seconds=2 ** item.attempts * 5)
            else:
                # Failure before sending a write is safe to retry; unknown writes never arrive here.
                item.status = 'failed'
        except Exception:  # noqa: BLE001 - persist failures without exposing secret-bearing exception text
            # Never log exception values: a transport/parser can contain a credential.
            db.rollback()
            item = db.get(TaskItem, item_id)
            if item is not None:
                item.status = 'needs_review' if item.stage in ('create_sent', 'write_sent', 'test_sent', 'created_pending_verification', 'updated_pending_verification', 'reconcile') else 'failed'
                item.error = '执行器异常，详情已隐藏；请核对任务状态'
                log.error('Task item failed: %s', item.id)
        finally:
            try:
                if item is not None:
                    try:
                        dist = lock_result_distribution(db, item, refresh=False, stop=False)
                        item.updated_at, item.lease_until = utcnow(), None
                        if dist and item.operation in MONITOR_OPERATIONS:
                            record_observation(dist, item)
                        if dist and item.error and item.operation not in MONITOR_OPERATIONS and not locally_deleted(dist):
                            dist.error = item.error
                            if not dist.remote_id:
                                dist.status = 'needs_review' if item.status == 'needs_review' else item.status
                        task = db.get(Task, item.task_id)
                        db.flush()
                        refresh_task(db, task)
                        actor = db.get(User, task.execution_actor_id or task.actor_id)
                        audit(db, actor, 'task.item.' + item.status, 'task', task.id,
                              {'item_id': item.id, 'site_id': item.site_id, 'operation': item.operation, 'stage': item.stage})
                        db.commit()
                    except WorkRemoved:
                        db.rollback()
            finally:
                lease.close()
        return True


def sync_interval_seconds():
    # Invalid intervals must not turn the polling loop into continuous batches.
    interval = settings.sync_interval_seconds
    return interval if interval > 0 else DEFAULT_SYNC_INTERVAL_SECONDS


def schedule_sync():
    with SessionLocal() as db:
        # Redis limits scheduler contention, but its expiring lease cannot fence
        # a slow database transaction. Hold the DB lock through the batch commit.
        if (db.get_bind().dialect.name == 'postgresql'
                and not db.scalar(select(func.pg_try_advisory_xact_lock(_SCHEDULER_LOCK)))):
            return
        actor = db.scalar(select(User).where(User.role == 'superadmin', User.active.is_(True), User.archived.is_(False)))
        if not actor:
            return
        recent = db.scalar(select(Task.id).where(Task.kind == 'scheduled_sync', Task.created_at > utcnow() - timedelta(seconds=sync_interval_seconds())).limit(1))
        active = db.scalar(select(Task.id).where(Task.kind == 'scheduled_sync', Task.status.in_(['queued', 'running'])).limit(1))
        if recent or active:
            return
        sites = list(db.scalars(select(Site).where(Site.archived.is_(False), Site.collect_enabled.is_(True))))
        if not sites:
            return
        owners = list(db.scalars(select(User.id)))
        task = new_task(db, actor, actor.id, 'scheduled_sync', snapshot={'owner_ids': owners})
        for site in sites:
            from .adapters.newapi_builds import build_snapshot
            db.add(TaskItem(task_id=task.id, site_id=site.id, operation='sync',
                            snapshot={'owner_ids': owners, 'discover': True, 'site_adapter': site.adapter,
                                      'site_base_url': site.base_url, 'seller_user_id': str(site.seller_user_id),
                                      **build_snapshot(site)}))
        db.commit()


def main():
    logging.basicConfig(level=logging.INFO)
    for name in ('SIGTERM', 'SIGINT'):
        signal.signal(getattr(signal, name), lambda *_: STOP.set())
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
    last_schedule = 0
    while not STOP.is_set():
        try:
            if time.monotonic() - last_schedule >= min(60, sync_interval_seconds()):
                # Only one scheduler publishes a batch at each interval.
                lock = client.lock('keyacross:scheduler', timeout=30)
                if lock.acquire(blocking=False):
                    try:
                        schedule_sync()
                    finally:
                        lock.release()
                last_schedule = time.monotonic()
            if not run_once(client):
                client.blpop('keyacross:work', timeout=2)
        except Exception:  # noqa: BLE001 - daemon recovers durable work after infrastructure errors
            log.error('Worker service unavailable; durable tasks will resume after recovery')
            STOP.wait(3)


if __name__ == '__main__':
    main()
