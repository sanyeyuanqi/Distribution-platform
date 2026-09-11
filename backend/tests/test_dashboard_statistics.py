"""Dashboard SQL summaries preserve historical scope without loading every fact."""
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import scope_owner_ids
from app.billing import record, serial
from app.db import engine, uid
from app.models import Category
from app.models_channels import Channel, Distribution, Task, UploadGroup
from app.routers import stats
from sqlalchemy import event, select
from test_channel_categories import service_case as _service_fixture

service_case = _service_fixture
NOW = datetime(2026, 9, 11, 16, 30, tzinfo=UTC).replace(tzinfo=None)


def _dashboard(api, **params):
    response = api.get('/api/dashboard', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _legacy_summary(db, actor, *, scope='team', owner_id=None):
    """Independent reference for the previously shipped, row-based contract."""
    owners = scope_owner_ids(db, actor, scope, owner_id)
    channels = list(db.scalars(select(Channel).where(Channel.owner_id.in_(owners))))
    distributions = list(db.scalars(select(Distribution)
        .where(Distribution.channel_id.in_([row.id for row in channels]))))
    facts = stats.usage_rows(db, actor, scope, owner_id)
    verified = [fact for fact in facts if fact.verified and fact.amount is not None]
    covered = {fact.distribution_id for fact in verified}
    names = {row.id: row.name for row in db.scalars(select(Category))}
    categories = [{'category_id': category_id, 'category_name': names[category_id],
        'channels': sum(row.category_id == category_id for row in channels),
        'totals_by_unit': stats.totals([fact for fact in verified if fact.category_id == category_id])}
        for category_id in sorted({row.category_id for row in channels} | {fact.category_id for fact in facts})]
    days = [(NOW + timedelta(hours=8) - timedelta(days=day)).date() for day in reversed(range(14))]
    recent = list(db.scalars(select(Task).where(Task.owner_id.in_(owners))
        .order_by(Task.created_at.desc()).limit(5))) if actor.role in ('superadmin', 'user') else []
    return serial({'local_channels': len(channels),
        'remote_channels': sum(row.remote_id is not None for row in distributions),
        'groups': len(list(db.scalars(select(UploadGroup).where(UploadGroup.owner_id.in_(owners))))),
        'users': len(owners), 'usage_by_unit': stats.totals(verified),
        'known_usage_by_unit': stats.totals(facts),
        'coverage': {'covered': len(covered), 'total': len(distributions)},
        'categories': categories,
        'trend': [{'date': str(day), 'totals_by_unit': stats.totals([fact for fact in verified
            if (fact.occurred_at + timedelta(hours=8)).date() == day])} for day in days],
        'recent_tasks': [record(task, ('payload', 'config_snapshot', 'snapshot')) for task in recent],
        'unverified_count': sum(not fact.verified for fact in facts),
        'source_cutoff': max((fact.occurred_at for fact in facts), default=None),
        'scope': scope,
        'data_status': 'partial' if len(covered) < len(distributions)
            else ('empty' if not facts else 'verified_import_only')})


@pytest.mark.parametrize(('actor', 'scope', 'owner'), [
    ('root', 'team', None), ('admin', 'team', None), ('user', 'team', None),
    ('admin', 'mine', None), ('root', 'mine', None), ('root', 'team', 'user'),
])
def test_sql_summary_matches_historical_scope_units_nulls_and_shanghai_boundaries(
        db, users, service_case, login, monkeypatch, actor, scope, owner):
    monkeypatch.setattr(stats, 'utcnow', lambda: NOW)
    case = service_case
    main = case.channel('AWS', 'newapi-33-aws-bedrock-v1')
    archived = case.channel('AWS', 'newapi-33-aws-bedrock-v1', owner='sibling', archived=True)
    historical = case.channel('Azure', 'newapi-3-azure-gpt-v1', owner='other_user')
    case.channel('OpenAI', 'api_key-v1', owner='admin')
    # The start/end boundaries use Shanghai calendar dates, independently of UTC.
    today_start = datetime(2026, 9, 11, 16, tzinfo=UTC).replace(tzinfo=None)
    window_start = today_start - timedelta(days=13)
    for amount, when in [('0.12345678', window_start), ('2', window_start - timedelta(microseconds=1)),
                         ('3', today_start - timedelta(microseconds=1)), ('4', today_start),
                         ('5', today_start + timedelta(days=1))]:
        case.fact(main, amount).occurred_at = when
    case.fact(main, '0', unit='EUR').occurred_at = NOW
    case.fact(main, '7', verified=False).occurred_at = NOW
    case.fact(main, None, unit='JPY').occurred_at = NOW
    case.fact(main, None, verified=False).occurred_at = NOW
    case.fact(archived, '11').occurred_at = NOW
    # Frozen historical ownership is independent of the current channel owner.
    case.fact(historical, '13.00000001', unit='EUR', owner='user').occurred_at = NOW
    case.fact(historical, '999').occurred_at = NOW
    for index in range(7):
        db.add(Task(id=uid(), actor_id=users['user'].id, owner_id=users['user'].id,
            actor_session_version=users['user'].session_version, kind='sync', status='succeeded',
            snapshot={'private': 'excluded-large-task-payload'}, created_at=NOW - timedelta(hours=index)))
    db.commit()
    owner_id = users[owner].id if owner else None
    expected = _legacy_summary(db, users[actor], scope=scope, owner_id=owner_id)
    params = {'scope': scope, **({'owner_id': owner_id} if owner_id else {})}
    actual = _dashboard(login(actor), **params)
    assert {key: actual[key] for key in expected} == expected


@pytest.mark.parametrize('with_facts', [False, True])
def test_empty_and_verified_only_status_are_preserved(db, users, service_case, login, monkeypatch, with_facts):
    monkeypatch.setattr(stats, 'utcnow', lambda: NOW)
    if with_facts:
        pair = service_case.channel('AWS', 'newapi-33-aws-bedrock-v1')
        service_case.fact(pair, '0').occurred_at = NOW
        db.commit()
    data = _dashboard(login('user'))
    assert data['data_status'] == ('verified_import_only' if with_facts else 'empty')
    assert len(data['trend']) == 14
    assert data['usage_by_unit'] == ({'USD': '0'} if with_facts else {})


def test_dashboard_query_size_does_not_grow_with_fact_history_and_omits_unused_payloads(
        db, users, service_case, login, monkeypatch):
    monkeypatch.setattr(stats, 'utcnow', lambda: NOW)
    pair = service_case.channel('AWS', 'newapi-33-aws-bedrock-v1')
    service_case.fact(pair, '1').occurred_at = NOW
    db.add(Task(id=uid(), actor_id=users['user'].id, owner_id=users['user'].id,
        actor_session_version=users['user'].session_version, kind='sync',
        snapshot={'private': 'unused-task-snapshot' * 1000}))
    db.commit()
    api = login('root')

    def capture_request():
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append((statement.lower(), cursor.rowcount))

        event.listen(engine, 'after_cursor_execute', capture)
        try:
            result = _dashboard(api)
        finally:
            event.remove(engine, 'after_cursor_execute', capture)
        return result, statements

    _, before = capture_request()
    for index in range(250):
        fact = service_case.fact(pair, '0.1')
        fact.evidence = 'unused historical evidence ' * 100
        fact.occurred_at = NOW - timedelta(days=index + 20)
    db.commit()
    result, after = capture_request()
    assert result['usage_by_unit'] == {'USD': '26'}
    assert len(after) == len(before)
    fact_queries = [(sql, count) for sql, count in after if 'from usage_facts' in sql]
    assert fact_queries
    assert all(('group by' in sql or 'count(' in sql) and count <= 1 for sql, count in fact_queries)
    assert not any(any(field in sql for field in (
        'usage_facts.evidence', 'usage_facts.raw_amount', 'channels.key_encrypted',
        'channels.upload_settings', 'upload_groups.remark', 'tasks.snapshot', 'sites.token_encrypted',
    )) for sql, _ in after)
    assert not any(sql.lstrip().startswith(('insert ', 'update ', 'delete ')) for sql, _ in after)
