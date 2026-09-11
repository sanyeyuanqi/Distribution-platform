"""Pure protocol selection checks; the test does not invoke AWS."""
import pytest
from app.adapters.bedrock_capabilities import bedrock_api_key_sdk_mode


@pytest.mark.parametrize('version', [
    'v0.13.2', 'v1.0.0-rc.11', 'v1.0.0-rc.19-i18nfix.2',
    'v1.0.0-rc.20', 'v1.0.0-rc.25', 'v1.0.0-rc.35',
])
def test_reviewed_official_release_uses_sdk_for_bearer_credentials(version):
    assert bedrock_api_key_sdk_mode('new-api-v1', version) is True


@pytest.mark.parametrize('version', [
    'v0.13.1', 'v1.0.0-rc.10', 'v1.0.0-rc.36', 'v1.0.0',
    'v2.0.0', 'v1.0.0-rc.35-custom', 'v1.0.0-rc.25-fix-38',
    'v1.0.0-rc.32-colin', 'v1.0.0-rc.35 ', '', None, {}, True,
])
def test_unknown_versions_do_not_inherit_sdk_compatibility(version):
    assert bedrock_api_key_sdk_mode('new-api-v1', version) is False


@pytest.mark.parametrize(('adapter', 'version'), [
    ('silicon-v1', 'v1.0.0-rc.25-fix-36'),
    ('silicon-v1', 'v1.0.0-rc.25-fix-38'),
    ('tcp-red-v1', 'v1.0.0-rc.32-colin'),
    ('silicon-v1', 'v1.0.0-rc.35'),
    ('tcp-red-v1', 'v1.0.0-rc.35'),
    ('unknown', 'v0.13.2'),
    (None, 'v0.13.2'),
])
def test_forks_require_independent_relay_evidence(adapter, version):
    assert bedrock_api_key_sdk_mode(adapter, version) is False
