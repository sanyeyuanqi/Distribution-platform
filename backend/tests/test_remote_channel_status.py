"""Known disabled states normalize consistently without disguising unknown/local failures."""
# ruff: noqa: F811
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app import worker
from app.models_channels import Distribution
from app.remote_channel_status import distribution_remote_state, remote_channel_state
from sqlalchemy import select
from test_channels import delete_case, setup_catalog  # noqa: F401

STATES = [
    (1, 'enabled', None), (2, 'disabled', 'manual'), (3, 'disabled', 'automatic'),
    (0, 'unavailable', None), (4, 'unavailable', None), (-1, 'unavailable', None),
    (None, 'unavailable', None), (True, 'unavailable', None), (False, 'unavailable', None),
    ('1', 'unavailable', None), ('2', 'unavailable', None), ('3', 'unavailable', None),
    (1.0, 'unavailable', None), (3.0, 'unavailable', None), ([], 'unavailable', None),
    ({}, 'unavailable', None),
]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Status normalization must not contact a real remote')
    monkeypatch.setattr('app.adapters.silicon.safe_request', forbidden)


@pytest.mark.parametrize(('raw', 'status', 'reason'), STATES)
def test_remote_states_are_strict_and_only_known_disabled_values_have_reasons(raw, status, reason):
    assert remote_channel_state(raw) == {'status': status, 'disable_reason': reason}


@pytest.mark.parametrize('local_status', ['failed', 'pending', 'running', 'needs_review', 'created_pending_verification',
                                         'missing', 'deleted', 'cancelled'])
def test_remote_snapshot_does_not_replace_unresolved_or_deleted_local_state(local_status):
    dist = SimpleNamespace(status=local_status, remote_id='91', remote_snapshot={'status': 3})
    assert distribution_remote_state(dist) == {'status': local_status, 'disable_reason': None}


@pytest.mark.parametrize('condition', ['no_remote', 'tombstone', 'no_snapshot_status'])
def test_projection_requires_live_remote_link_and_saved_status(condition):
    dist = SimpleNamespace(status='unavailable', remote_id='91', remote_snapshot={'status': 3})
    if condition == 'no_remote':
        dist.remote_id = None
    elif condition == 'tombstone':
        dist.remote_snapshot['_local_deletion'] = {'remote_confirmed': False}
    else:
        dist.remote_snapshot = {}
    assert distribution_remote_state(dist) == {'status': 'unavailable', 'disable_reason': None}


def test_record_remote_persists_known_status_without_losing_raw_evidence(db, delete_case):
    case = delete_case
    sibling = db.scalar(select(Distribution).where(Distribution.channel_id == case.channel.id,
                                                   Distribution.id != case.dist.id))
    sibling_before = deepcopy(sibling.remote_snapshot)
    for raw, status, reason in STATES:
        worker.record_remote(db, case.dist, {**case.remote, 'status': raw})
        db.commit()
        db.refresh(case.dist)
        assert case.dist.status == status
        assert case.dist.remote_snapshot['status'] == raw
        assert type(case.dist.remote_snapshot['status']) is type(raw)
        assert distribution_remote_state(case.dist)['disable_reason'] == reason
    db.refresh(sibling)
    assert sibling.remote_snapshot == sibling_before and sibling.status == 'missing'


def test_historical_auto_disabled_snapshot_projects_immediately_without_database_update(db, login, delete_case):
    case = delete_case
    clients = [case.client, login('admin'), login('root')]
    for local_status in ('enabled', 'disabled', 'unavailable'):
        case.dist.status = local_status
        case.dist.remote_snapshot = {**case.remote, 'status': 3}
        db.commit()
        for client in clients:
            detail = client.get(f'/api/channels/{case.channel.id}')
            assert detail.status_code == 200
            row = next(row for row in detail.json()['distributions'] if row['id'] == case.dist.id)
            assert row['status'] == 'disabled' and row['disable_reason'] == 'automatic'
        listing = case.client.get('/api/channels').json()['items']
        row = next(row for channel in listing for row in channel['distributions'] if row['id'] == case.dist.id)
        assert row['status'] == 'disabled' and row['disable_reason'] == 'automatic'
        db.refresh(case.dist)
        assert case.dist.status == local_status and case.dist.remote_snapshot['status'] == 3


def test_api_keeps_local_failure_and_never_reports_bad_remote_values_as_disabled(db, delete_case):
    case = delete_case
    for local_status, raw, expected, reason in [
        ('failed', 3, 'failed', None), ('needs_review', 3, 'needs_review', None),
        ('created_pending_verification', 3, 'created_pending_verification', None),
        ('missing', 3, 'missing', None), ('deleted', 3, 'deleted', None),
        ('disabled', 2, 'disabled', 'manual'), ('disabled', True, 'unavailable', None),
        ('disabled', '3', 'unavailable', None), ('disabled', 0, 'unavailable', None),
        ('disabled', 7, 'unavailable', None),
    ]:
        case.dist.status, case.dist.error = local_status, 'original safe error'
        case.dist.remote_snapshot = {**case.remote, 'status': raw}
        db.commit()
        response = case.client.get(f'/api/channels/{case.channel.id}')
        assert response.status_code == 200
        row = next(row for row in response.json()['distributions'] if row['id'] == case.dist.id)
        assert (row['status'], row['disable_reason']) == (expected, reason)
        assert row['error'] == 'original safe error'
        db.refresh(case.dist)
        assert case.dist.status == local_status
