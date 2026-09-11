"""Pure version-gate checks: no provider traffic or database operations."""
import pytest
from app.adapters.vertex_capabilities import vertex_claude_api_key_capability


@pytest.mark.parametrize('version', ['v0.13.2', 'v1.0.0-rc.1'])
def test_old_official_routing_is_explicitly_unsupported(version):
    assert vertex_claude_api_key_capability('new-api-v1', version) == 'unsupported'


@pytest.mark.parametrize('version', [
    'v1.0.0-rc.11', 'v1.0.0-rc.19-i18nfix.2', 'v1.0.0-rc.20', 'v1.0.0-rc.25', 'v1.0.0-rc.35',
])
def test_reviewed_official_routes_support_claude_api_keys(version):
    assert vertex_claude_api_key_capability('new-api-v1', version) == 'supported'


@pytest.mark.parametrize('version', [
    'v1.0.0-rc.2', 'v1.0.0-rc.36', 'v1.0.0', 'v2.0.0',
    'v1.0.0-rc.35-custom', 'v1.0.0-rc.25-fix-38', 'v1.0.0-rc.32-colin',
    ' v1.0.0-rc.35', 'v1.0.0-rc.35 ', '', None, {}, True,
])
def test_unreviewed_management_or_relay_versions_cannot_opt_in(version):
    assert vertex_claude_api_key_capability('new-api-v1', version) == 'unknown'


@pytest.mark.parametrize(('adapter', 'version'), [
    ('silicon-v1', 'v1.0.0-rc.25-fix-36'),
    ('silicon-v1', 'v1.0.0-rc.25-fix-38'),
    ('silicon-v1', 'v1.0.0-rc.25-fix-22-multiseller-2'),
    ('tcp-red-v1', 'v1.0.0-rc.32-colin'),
    ('tcp-red-v1', 'v1.0.0-rc.35'),
    ('silicon-v1', 'v1.0.0-rc.35'),
    ('unknown', 'v1.0.0-rc.35'),
    (None, 'v1.0.0-rc.35'),
])
def test_fork_identity_never_inherits_official_support(adapter, version):
    assert vertex_claude_api_key_capability(adapter, version) == 'unknown'
