"""Removed views have no API surface; historical records remain untouched."""
# ruff: noqa: F811
from copy import deepcopy

from app.main import app
from app.models_channels import UnclaimedChannel, UploadGroup
from sqlalchemy import event, select
from test_channels import setup_catalog  # noqa: F401


def test_retired_view_routes_and_request_schemas_are_absent():
    schema = app.openapi()
    for path in ('/api/groups', '/api/groups/{group_id}', '/api/unclaimed', '/api/unclaimed/{record_id}/adopt'):
        assert path not in schema['paths']
    assert not {'PatchGroup', 'Adoption'} & schema['components']['schemas'].keys()


def test_retired_view_requests_return_404_without_accessing_database(db, users, login, client, setup_catalog):
    category, fmt, sites = setup_catalog
    group = UploadGroup(owner_id=users['user'].id, category_id=category.id, format_id=fmt.id,
                        tag='retired-view-fixture', name='Historical group', remark='Keep history')
    record = UnclaimedChannel(site_id=sites[0].id, remote_id='909', remote_name='historical-discovery',
                              snapshot={'id': 909, 'name': 'historical-discovery', 'type': 1,
                                        'status': 2, 'models': 'test-model', 'group': 'default'})
    db.add_all([group, record])
    db.commit()
    requests = [
        ('GET', '/api/groups', None),
        ('PATCH', f'/api/groups/{group.id}', {'name': 'Must not rename', 'remark': 'Must not replace'}),
        ('GET', '/api/unclaimed', None),
        ('POST', f'/api/unclaimed/{record.id}/adopt', {
            'owner_id': users['user'].id, 'category_id': category.id, 'format_id': fmt.id,
            'key': 'retired-route-placeholder-secret', 'baseline_amount': '0', 'baseline_unit': 'USD',
            'reason': 'Former valid request must not create ownership'}),
    ]
    clients = [client, *(login(role) for role in ('root', 'admin', 'user'))]

    def history():
        return {model.__tablename__: deepcopy([dict(row) for row in db.execute(select(model.__table__)).mappings()])
                for model in (UploadGroup, UnclaimedChannel)}

    before = history()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.bind, 'before_cursor_execute', capture)
    try:
        for actor in clients:
            for method, path, body in requests:
                response = actor.request(method, path, json=body)
                assert response.status_code == 404, response.text
                assert 'retired-route-placeholder-secret' not in response.text
    finally:
        event.remove(db.bind, 'before_cursor_execute', capture)
    # Routing stops before reads, writes, ownership changes, or audit inserts.
    assert statements == []
    assert history() == before
