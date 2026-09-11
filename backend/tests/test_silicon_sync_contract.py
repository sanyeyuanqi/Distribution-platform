"""Pure worker simulations: a failed interface recheck cannot become a healthy sync."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from app import worker
from app.adapters.silicon import RemoteError


@pytest.mark.parametrize(('reason', 'category'), [
    ('incompatible_interface', 'protocol_error'),
    ('interface_changed', 'protocol_error'),
    ('permissions_changed', 'permission_denied'),
    (None, 'identity_mismatch'),
    (None, 'permission_denied'),
    (None, 'authentication_error'),
    ('build_changed', 'protocol_error'),
    ('unrecognized_build', 'protocol_error'),
    ('build_unavailable', 'protocol_error'),
])
def test_sync_recheck_errors_stop_before_local_data_or_health_changes(reason, category):
    calls = []
    failure = RemoteError('fixed safe message', reason=reason, category=category)
    def channels():
        calls.append('channels')
        return [{'id': 91, 'name': 'existing', 'type': 1, 'status': 1,
                 'models': 'model', 'group': 'default', 'used_quota': 123}]
    def conversion(*, refresh):
        assert refresh is True
        calls.append('recheck')
        raise failure
    def unexpected_database_read(*args):
        pytest.fail('A rejected interface must not start applying remote rows')
    site = SimpleNamespace(id='site', health='error', last_sync_at=None)
    db = SimpleNamespace(scalars=unexpected_database_read)
    adapter = SimpleNamespace(channels=channels, usage_conversion=conversion)
    with pytest.raises(RemoteError) as caught:
        worker.sync_site(db, adapter, SimpleNamespace(id='task'),
                         SimpleNamespace(snapshot={'owner_ids': ['owner']}),
                         SimpleNamespace(role='user'), site)
    assert caught.value is failure and calls == ['channels', 'recheck']
    assert site.health == 'error' and site.last_sync_at is None


@pytest.mark.parametrize('conversion_error', [None, 'unsupported', 'remote_error'])
def test_optional_conversion_failure_keeps_raw_usage_without_inventing_usd(monkeypatch, conversion_error):
    remote = {'id': 91, 'name': 'existing', 'type': 1, 'status': 1,
              'models': 'model', 'group': 'default', 'used_quota': 123}
    dist = SimpleNamespace(id='distribution', remote_id='91', remote_snapshot={})
    site = SimpleNamespace(id='site', health='error', last_sync_at=None)
    writes = []
    def conversion(*, refresh):
        assert refresh is True
        if conversion_error:
            raise RemoteError('Optional conversion metadata unavailable', category=conversion_error)
    def record_remote(db, target, result):
        assert target is dist and result == remote
        writes.append('remote')
    def record_observation(target, task_id, observed):
        assert target is dist and task_id == 'task'
        assert observed['used_quota'] == 123 and observed['conversion'] is None
        assert observed['used_amount'] is None and observed['used_amount_unit'] is None
        assert observed['settlement_verified'] is False
        writes.append('raw_usage')
    monkeypatch.setattr(worker, 'record_remote', record_remote)
    monkeypatch.setattr(worker, 'record_sync_observation', record_observation)
    db = SimpleNamespace(scalars=lambda statement: [dist], scalar=lambda statement: dist,
                         no_autoflush=nullcontext())
    adapter = SimpleNamespace(channels=lambda: [remote], usage_conversion=conversion)
    worker.sync_site(db, adapter, SimpleNamespace(id='task'),
                     SimpleNamespace(snapshot={'owner_ids': ['owner']}),
                     SimpleNamespace(role='user'), site)
    assert writes == ['remote', 'raw_usage']
    assert site.health == 'healthy' and site.last_sync_at is not None
