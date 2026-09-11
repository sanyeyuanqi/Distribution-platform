"""Specific upload diagnostics without changing deduplication or exposing values."""
# ruff: noqa: F811
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.channel_service import parse_rows
from app.db import uid
from app.models_channels import Channel, Distribution, Task, TaskItem
from app.security import encrypt
from app.upload_templates import SimpleUploadInput, channel_conflict_reasons
from sqlalchemy import func, select
from test_catalog_policy import catalog_setup  # noqa: F401
from test_multikey_containers import (  # noqa: F401
    container_site,
    post,
    request_body,
    setup_template,
)


@pytest.fixture(params=[1, 2], ids=['single', 'multiple'])
def existing(db, login, container_site, request):
    client = login('user')
    category, fmt = setup_template(db, login('root'), container_site)
    body = request_body(category, fmt, '\n'.join(f'fixture-private-key-{n}' for n in range(request.param)))
    result = post(client, body)
    channel = db.get(Channel, result['items'][0]['channel_id'])
    dist = db.get(Distribution, result['items'][0]['distribution_id'])
    return SimpleNamespace(client=client, body=body, channel=channel, dist=dist, count=request.param)


def preview(case, **values):
    response = case.client.post('/api/uploads/simple-preview', json={
        **{k: v for k, v in case.body.items() if k != 'idempotency_key'}, **values})
    assert response.status_code == 200, response.text
    return response.json()


def reasons(result, count):
    assert not result['can_submit'] and result['conflict_count'] == count
    rows = result['rows']
    assert all(row['message'] == '；'.join(row['reasons']) for row in rows)
    assert all(len(row['reasons']) == len(set(row['reasons'])) for row in rows)
    return rows[0]['reasons']


def test_archived_and_forced_deleted_reports_both_actual_causes_and_submit_stays_rejected(db, existing):
    case = existing
    case.channel.archived = True
    case.dist.status = 'deleted'
    case.dist.remote_snapshot = {**case.dist.remote_snapshot,
        '_local_deletion': {'remote_confirmed': False, 'at': 'fixture'}}
    db.commit()
    task_count = db.scalar(select(func.count()).select_from(Task))
    saved = [(item.id, deepcopy(item.snapshot)) for item in db.scalars(select(TaskItem))]
    expected = ['已有本地渠道已归档，不能通过重复上传自动恢复',
                '已有分发已强制删除（本地），远端是否删除尚未确认，不能通过重复上传恢复']
    result = preview(case)
    assert reasons(result, case.count) == expected
    assert all(row['existing_channel_id'] == case.channel.id for row in result['rows'])
    blocked = case.client.post('/api/uploads/simple-submit', json={**case.body, 'idempotency_key': 'new-conflict-attempt'})
    assert blocked.status_code == 422
    assert blocked.json()['detail']['preview']['rows'][0]['reasons'] == expected
    db.expire_all()
    assert case.dist.status == 'deleted' and case.channel.archived
    assert db.scalar(select(func.count()).select_from(Task)) == task_count
    assert all(db.get(TaskItem, item_id).snapshot == snap for item_id, snap in saved)


@pytest.mark.parametrize('local', [True, False])
def test_local_removal_is_distinguished_from_remote_deletion(db, existing, local):
    case = existing
    case.dist.status = 'deleted'
    if local:
        case.dist.remote_snapshot = {'_local_deletion': {'remote_confirmed': False}}
    db.commit()
    found = reasons(preview(case), case.count)
    assert len(found) == 1
    assert ('远端是否删除尚未确认' in found[0]) is local
    if not local:
        assert found == ['已有远端分发已删除，不能通过重复上传重新创建']


def test_changed_user_model_scope_is_not_misreported_as_other_configuration(db, existing):
    found = reasons(preview(existing, models=['fixture-model']), existing.count)
    assert found == ['本次选择的模型范围与原渠道不同']


def test_changed_note_uses_specific_reason_without_echoing_new_note(db, existing):
    found = reasons(preview(existing, remarks='private-note-value'), existing.count)
    assert '本次备注与原渠道保存的备注不同' in found
    assert 'private-note-value' not in '；'.join(found)


@pytest.mark.parametrize('field,expected', [
    ('models', '原分发模型与当前站点模板的有效模型范围不同'),
    ('routing_group', '原分发的目标渠道分组与当前站点模板不同'),
    ('effective_remark', '原分发的生效备注与本次上传或当前模板不同'),
    ('effective_channel_config', '原分发的生效渠道配置与当前站点模板不同'),
    ('upload_template_id', '原分发关联的站点模板与当前模板不同'),
])
def test_receiver_differences_are_classified_without_exposing_hidden_values(db, existing, field, expected):
    case = existing
    secret = 'private-site-setting-must-not-leak'
    if field == 'models':
        case.dist.models = [secret]
    elif field == 'upload_template_id':
        case.dist.upload_template_id = None
    elif field == 'routing_group':
        case.dist.routing_group = secret
    else:
        case.dist.template_snapshot = {**case.dist.template_snapshot, field: {'base_url': secret} if
                                       field == 'effective_channel_config' else secret}
    db.commit()
    result = preview(case)
    assert reasons(result, case.count) == [expected]
    assert secret not in str(result)


def test_unchanged_duplicate_retains_existing_behavior(db, existing):
    result = preview(existing)
    assert not result['can_submit'] and result['conflict_count'] == 0
    assert all(row['status'] == 'already_distributed' and not row.get('reasons') for row in result['rows'])


@pytest.mark.parametrize('existing', [1], indirect=True)
def test_stored_proxy_difference_does_not_expose_address_or_credentials(db, existing):
    case = existing
    # A historical single-Key proxy is compared without publishing its value.
    case.channel.proxy_encrypted = encrypt('http://private-user:private-secret@private-host:8080')
    db.commit()
    result = preview(case)
    assert '本次代理设置与原渠道保存的设置不同' in reasons(result, case.count)
    assert all(value not in str(result) for value in ('private-user', 'private-secret', 'private-host'))


def test_channel_identity_and_option_diagnostics_do_not_publish_values():
    channel = SimpleNamespace(archived=False, upload_mode='advanced', category_id='old-private-category')
    payload = SimpleUploadInput(category_id='new-category', credentials='not-used')
    found = channel_conflict_reasons(channel, payload, {'inventory': True, 'rpm_limit': 7},
        format_matches=False, remark_matches=True, proxy_matches=True,
        stored_settings={'inventory': False, 'rpm_limit': 1000})
    assert found == ['原渠道不是通过站点模板上传，不能用本次上传覆盖', '本次上传分类与原渠道不同',
                     '本次凭据格式与原渠道不同', '本次入库存设置与原渠道不同', '本次其他上传选项与原渠道不同']
    assert all(value not in '；'.join(found) for value in ('old-private-category', 'new-category', '1000'))


@pytest.mark.parametrize('address', ['http://host:private-secret-value', 'http://[private-secret-value]:8080',
    'http://host:999999999999', 'http://host:not-an-integer'])
def test_invalid_proxy_diagnostic_never_echoes_parser_input(address):
    rows, errors = parse_rows('key-placeholder', proxies=address)
    assert not errors and rows[0]['status'] == 'invalid'
    assert rows[0]['message'] == '代理需要合法协议、主机及端口'
    assert 'private-secret-value' not in rows[0]['message']


def test_parse_conflicts_and_validation_errors_keep_their_safe_message_fallback():
    rows, _ = parse_rows('same-key\nsame-key', 'note-one\nnote-two')
    assert all(row['status'] == 'conflict' and '凭据相同但配置不同' in row['message'] for row in rows)
    assert all(not row.get('reasons') for row in rows)


def test_ordinary_new_upload_is_still_accepted(db, login, container_site):
    client = login('user')
    category, fmt = setup_template(db, login('root'), container_site)
    body = request_body(category, fmt, uid())
    result = client.post('/api/uploads/simple-preview', json={k: v for k, v in body.items() if k != 'idempotency_key'})
    assert result.status_code == 200 and result.json()['can_submit']


@pytest.mark.parametrize('safe', [
    '此站点版本尚未验证 Vertex Claude API Key 支持，请使用服务账号 JSON',
    '站点没有创建渠道权限', '站点尚未验证，请先验证站点',
    '站点尚未验证渠道启用权限，不能使用创建后启用的模板', '模板尚未配置模型',
])
def test_public_target_reasons_keep_only_exact_safe_labels(db, login, container_site, monkeypatch, safe):
    client = login('user')
    category, fmt = setup_template(db, login('root'), container_site)
    private_issues = ['不支持的模型：private-model', '站点不允许路由组 private-route',
                      '站点验证失败：private-provider-error', safe + ':private-extra']
    monkeypatch.setattr('app.upload_templates.template_issues', lambda *args, **kwargs: [safe, *private_issues])
    body = request_body(category, fmt, 'placeholder-key')
    response = client.post('/api/uploads/simple-preview', json={k: v for k, v in body.items() if k != 'idempotency_key'})
    assert response.status_code == 200 and not response.json()['can_submit']
    target = response.json()['targets'][0]
    assert target['issues'] == [safe, '站点模板配置不可用，请联系管理员检查']
    assert all(value not in response.text for value in ('private-model', 'private-route', 'private-provider-error', 'private-extra'))


def test_note_limit_does_not_hide_another_safe_target_failure(db, login, container_site, monkeypatch):
    client = login('user')
    category, fmt = setup_template(db, login('root'), container_site)
    container_site.capabilities = {**container_site.capabilities, 'remark_max_length': 10}
    db.commit()
    safe = '此站点版本尚未验证 Vertex Claude API Key 支持，请使用服务账号 JSON'
    monkeypatch.setattr('app.upload_templates.template_issues', lambda *args, **kwargs: [safe])
    body = request_body(category, fmt, 'placeholder-key', remarks='this note is too long')
    response = client.post('/api/uploads/simple-preview', json={k: v for k, v in body.items() if k != 'idempotency_key'})
    assert response.status_code == 200
    assert response.json()['targets'][0]['issues'] == [safe, '备注超过此站点的 10 字限制']
