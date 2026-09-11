"""Fixed catalog selectors and template write boundaries."""
# ruff: noqa: F811
import pytest
from app.catalog_policy import get_catalog_format_specs
from app.channel_service import supported_format
from app.db import SessionLocal, uid
from app.models import Category, CredentialFormat, SiteUploadTemplate
from app.routers.uploads import lock_catalog_for_upload
from app.upload_templates import effective_template
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from test_catalog_policy import catalog_setup, template_body  # noqa: F401


@pytest.mark.parametrize('role', ['root', 'admin', 'user'])
def test_catalog_only_exposes_fixed_choices_without_write_routes(db, login, catalog_setup, role):
    category = db.scalar(select(Category).where(Category.family == 'OpenAI'))
    category.name = 'Previously editable name'
    custom = CredentialFormat(id=uid(), category_id=category.id, code='custom-v9', version='9',
        name='Historical custom', enabled=True, schema_config={'type': 'api_key', 'remote_type': 1})
    db.add(custom)
    db.commit()
    client = login(role)
    listing = client.get('/api/categories').json()['items']
    assert next(row for row in listing if row['id'] == category.id)['name'] == 'OpenAI'
    assert custom.id not in str(listing)
    assert client.get('/api/formats').json()['total'] == len(get_catalog_format_specs()) == 13
    assert client.post('/api/formats', json={}).status_code == 405
    assert client.post('/api/categories', json={}).status_code == 405
    assert client.patch('/api/formats/' + custom.id, json={'enabled': False}).status_code == 404
    assert client.patch('/api/categories/' + category.id, json={'active': False}).status_code == 404
    assert supported_format(category, custom)


def test_fixed_api_ignores_stored_display_metadata_and_model_defaults(db, login, catalog_setup):
    category = db.scalar(select(Category).where(Category.family == 'OpenAI'))
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == category.id))
    fmt.name, fmt.default_models = 'Edited name', ['untrusted-default']
    fmt.schema_config = {**fmt.schema_config, 'help': 'Edited help'}
    db.commit()
    row = next(row for row in login('root').get('/api/formats').json()['items'] if row['id'] == fmt.id)
    assert row['name'] == 'API 密钥' and row['default_models'] == []
    assert 'Edited help' not in row['help'] and row['schema_config'] == {'type': 'api_key', 'remote_type': 1}


def test_template_writes_cannot_restore_removed_configuration(db, login, catalog_setup):
    root = login('root')
    category = db.scalar(select(Category).where(Category.family == 'OpenAI'))
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == category.id))
    advanced = {'base_url': 'https://custom.invalid', 'organization': 'old-org', 'other': 'old-value',
        'status': 2, 'priority': 99, 'model_mapping': {'fixture-model': 'unwanted'}, 'default_upload_mode': 'single'}
    response = root.post('/api/upload-templates', json=template_body(category, fmt, catalog_setup,
        enabled=True, channel_config=advanced, proxy='http://secret:password@proxy.invalid:81'))
    assert response.status_code == 201, response.text
    row = db.get(SiteUploadTemplate, response.json()['id'])
    assert row.channel_config == {'status': 2} and row.proxy_encrypted is None
    patched = root.patch('/api/upload-templates/' + row.id, json={'channel_config': advanced, 'remark': 'kept'})
    assert patched.status_code == 200 and patched.json()['remark'] == 'kept'
    db.refresh(row)
    assert row.channel_config == {'status': 2} and row.proxy_encrypted is None
    effective = effective_template(row, category=category, fmt=fmt)
    assert effective['channel_config']['base_url'] == '' and effective['channel_config']['model_mapping'] == {}
    assert effective['channel_config']['priority'] == 0


def test_upload_reference_lock_serializes_first_use(db, catalog_setup):
    category = db.scalar(select(Category).where(Category.family == 'OpenAI'))
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == category.id))
    lock_catalog_for_upload(db, category.id)
    with SessionLocal() as other:
        other.execute(text("SET LOCAL lock_timeout = '50ms'"))
        with pytest.raises(OperationalError):
            other.scalar(select(Category).where(Category.id == category.id).with_for_update())
        other.rollback()
        with pytest.raises(OperationalError):
            other.scalar(select(CredentialFormat).where(CredentialFormat.id == fmt.id).with_for_update(nowait=True))
        other.rollback()
    db.rollback()
