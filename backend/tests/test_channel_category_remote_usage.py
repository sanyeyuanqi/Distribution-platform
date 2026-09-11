"""Service-menu remote amounts share channel totals without replacing usage facts."""
# ruff: noqa: F811
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from app.adapters.channel_observation import extract_usage, quota_conversion
from app.db import engine, uid
from app.models_channels import Task, TaskItem
from sqlalchemy import event
from test_channel_categories import menu, service_case  # noqa: F401

STAMP = datetime(2026, 9, 10, 1, tzinfo=UTC)


def observe(db, pair, quota, *, manual=False):
    channel, dist = pair
    conversion = quota_conversion({'version': 'v1.0.0-rc.32-colin', 'quota_per_unit': 500000},
        adapter_kind='tcp-red-v1', verified_version='v1.0.0-rc.32-colin')
    result = {'synced_at': (STAMP + timedelta(microseconds=1)).isoformat(),
              'remote_usage': extract_usage({'used_quota': quota}, conversion=conversion)}
    dist.remote_id, dist.status = uid(), 'disabled'
    dist.last_sync_at = STAMP.replace(tzinfo=None)
    observation = deepcopy(result)
    if manual:
        task = Task(id=uid(), actor_id=channel.owner_id, owner_id=channel.owner_id,
                    actor_session_version=1, kind='sync_usage', status='succeeded')
        db.add(task)
        db.flush()
        dist.last_sync_at += timedelta(microseconds=2)
        db.add(TaskItem(task_id=task.id, channel_id=channel.id, distribution_id=dist.id,
            site_id=dist.site_id, operation='sync_usage', status='succeeded',
            updated_at=STAMP.replace(tzinfo=None) + timedelta(microseconds=3),
            snapshot={'operation_target': {'id': dist.remote_id}, 'operation_result': result}))
        observation['task_id'] = task.id
    dist.remote_snapshot = {'id': dist.remote_id, 'status': 3,
                            '_monitoring': {'usage_sync': observation}}
    db.flush()
    return dist


@pytest.mark.parametrize(('family', 'first', 'second', 'variants'), [
    ('AWS', 'newapi-33-aws-bedrock-v1', 'newapi-14-aws-claude-v1', ('bedrock', 'aws_claude')),
    ('Azure', 'newapi-3-azure-gpt-v1', 'newapi-14-azure-claude-v1', ('azure_gpt', 'azure_claude')),
    ('Google', 'newapi-41-vertex-gemini-v1', 'newapi-41-vertex-claude-v1', ('vertex_gemini', 'vertex_claude')),
])
def test_remote_zero_and_nonzero_are_service_isolated_and_leave_fact_fields_unchanged(
        db, service_case, login, family, first, second, variants):
    case = service_case
    zero = case.channel(family, first)
    nonzero = case.channel(family, second)
    observe(db, zero, 0)
    observe(db, nonzero, 1250000)
    case.fact(nonzero, '99', unit='EUR')
    db.commit()
    client = login('user')
    result = menu(client)
    a, b = [result[(case.categories[family].id, variant)] for variant in variants]
    assert a['remote_usage_total'] == {'amount': '0', 'unit': 'USD', 'covered': 1, 'total': 1}
    assert b['remote_usage_total'] == {'amount': '2.5', 'unit': 'USD', 'covered': 1, 'total': 1}
    assert a['verified_usage_by_unit'] == {} and a['data_status'] == 'missing'
    assert b['verified_usage_by_unit'] == {'EUR': '99.00000000'} and b['data_status'] == 'verified'
    assert client.get('/api/channels/' + zero[0].id).json()['remote_usage_total'] == a['remote_usage_total']
    assert client.get('/api/channels/' + nonzero[0].id).json()['remote_usage_total'] == b['remote_usage_total']
    empty = result[(case.categories['OpenRouter'].id, '')]
    assert empty['channel_count'] == 0
    assert empty['remote_usage_total'] == {'amount': '0', 'unit': 'USD', 'covered': 0, 'total': 0}


def test_full_scope_manual_sync_totals_do_not_grow_queries_or_load_credentials(db, users, service_case, login):
    case = service_case
    family, code = 'AWS', 'newapi-33-aws-bedrock-v1'
    client = login('user')
    key = (case.categories[family].id, 'bedrock')

    def measured_menu():
        statements = []

        def record(*args):
            statements.append(args[2])

        event.listen(engine, 'before_cursor_execute', record)
        try:
            result = menu(client)[key]
        finally:
            event.remove(engine, 'before_cursor_execute', record)
        assert not any('key_encrypted' in sql or 'token_encrypted' in sql for sql in statements)
        return result, len(statements)

    observe(db, case.channel(family, code), 100000, manual=True)
    db.commit()
    first, first_count = measured_menu()
    assert first['remote_usage_total']['amount'] == '0.2'
    for _ in range(52):
        observe(db, case.channel(family, code), 100000, manual=True)
    observe(db, case.channel(family, code, owner='sibling'), 1000000, manual=True)
    observe(db, case.channel(family, code, owner='other_user'), 999999999, manual=True)
    observe(db, case.channel(family, code, archived=True), 2000000, manual=True)
    db.commit()
    all_rows, all_count = measured_menu()
    assert all_rows['channel_count'] == 53
    assert all_rows['remote_usage_total'] == {'amount': '10.6', 'unit': 'USD', 'covered': 53, 'total': 53}
    assert all_count <= first_count + 1
    assert menu(login('admin'))[key]['remote_usage_total']['amount'] == '12.6'
    assert menu(login('admin'), owner_id=users['user'].id)[key]['remote_usage_total']['amount'] == '10.6'
    assert menu(client, archived='true')[key]['remote_usage_total']['amount'] == '4'
    assert menu(login('admin'), owner_id=users['admin'].id)[key]['remote_usage_total']['amount'] == '0'
    assert client.get('/api/channel-categories', params={'owner_id': users['other_user'].id}).status_code == 404


def test_missing_stale_and_removed_remote_data_never_become_zero(db, service_case, login):
    case = service_case
    code = 'newapi-33-aws-bedrock-v1'
    missing = case.channel('AWS', code)
    db.commit()
    key = (case.categories['AWS'].id, 'bedrock')
    client = login('user')
    assert menu(client)[key]['remote_usage_total'] == {
        'amount': None, 'unit': 'USD', 'covered': 0, 'total': 0}
    stale = observe(db, case.channel('AWS', code), 500000)
    stale.last_sync_at += timedelta(seconds=1)
    unknown = observe(db, case.channel('AWS', code), 500000)
    unknown.status = 'needs_review'
    absent_ratio = observe(db, case.channel('AWS', code), 500000)
    snapshot = deepcopy(absent_ratio.remote_snapshot)
    snapshot['_monitoring']['usage_sync']['remote_usage']['conversion'] = None
    absent_ratio.remote_snapshot = snapshot
    deleted = observe(db, case.channel('AWS', code), 500000)
    deleted.status = 'deleted'
    local = observe(db, case.channel('AWS', code), 500000)
    local.remote_snapshot = {**local.remote_snapshot, '_local_deletion': {'remote_confirmed': False}}
    db.commit()
    assert menu(client)[key]['remote_usage_total'] == {
        'amount': None, 'unit': 'USD', 'covered': 0, 'total': 3}
    observe(db, missing, 0)
    db.commit()
    assert menu(client)[key]['remote_usage_total'] == {
        'amount': '0', 'unit': 'USD', 'covered': 1, 'total': 4}
