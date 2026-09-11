"""AWS subtype routing and persisted configuration; no upstream writes."""
import pytest
from app.db import uid, utcnow
from app.models import Category, CredentialFormat, Site, SiteUploadTemplate
from app.models_channels import Channel, Distribution, Task, TaskItem
from app.newapi_formats import get_format_specs
from app.security import encrypt
from app.upload_templates import template_config


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('AWS routing tests must not access upstream services')
    monkeypatch.setattr('app.adapters.silicon.safe_request', forbidden)
    monkeypatch.setattr('app.routers.uploads.notify_worker', lambda: None)
    monkeypatch.setattr('app.routers.tasks.notify_worker', lambda: None)


@pytest.fixture
def aws(db, users):
    category = Category(id=uid(), name='AWS', family='AWS', active=True)
    db.add(category)
    db.flush()
    formats = {}
    for spec in get_format_specs():
        if spec['family'] != 'AWS':
            continue
        row = CredentialFormat(id=uid(), category_id=category.id, code=spec['code'], name=spec['name'],
            version=spec['version'], enabled=True, schema_config=spec['schema_config'], default_models=[])
        db.add(row)
        formats[spec['schema_config']['type']] = row
    sites = []
    for index in range(2):
        site = Site(id=uid(), name=f'AWS site {index}', prefix=f'A{index}',
            base_url=f'https://private-{index}.example', seller_user_id='33', adapter='silicon-v1',
            token_encrypted=encrypt('private-seller-key'), token_hint='masked', enabled=True,
            health='healthy', verified_at=utcnow(), capabilities={
                'models': ['bedrock-model', 'proxy-model', 'new-proxy-model'], 'groups': ['default'],
                'formats': [fmt.code for fmt in formats.values()], 'channel_types': [33, 14],
                'can_write': True, 'create': 'supported', 'channel_config': 'supported',
                'can_toggle': True, 'remark_max_length': 255})
        db.add(site)
        sites.append(site)
    db.commit()
    return category, formats, sites


def create_template(root, aws, kind='aws_bedrock', index=0, **values):
    category, formats, sites = aws
    response = root.post('/api/upload-templates', json={
        'site_id': sites[index].id, 'category_id': category.id, 'format_id': formats[kind].id,
        'models': ['proxy-model' if kind == 'aws_claude' else 'bedrock-model'], 'enabled': True, **values})
    assert response.status_code == 201, response.text
    return response.json()


def preview(client, aws, kind='aws_bedrock', **values):
    return client.post('/api/uploads/simple-preview', json={
        'category_id': aws[0].id, 'format_id': aws[1][kind].id,
        'credentials': 'proxy-private-key' if kind == 'aws_claude' else 'AKIA_TEST|private-sk|us-east-1', **values})


def submit(client, aws, kind='aws_bedrock', **values):
    response = client.post('/api/uploads/simple-submit', json={
        'category_id': aws[0].id, 'format_id': aws[1][kind].id,
        'credentials': 'proxy-private-key' if kind == 'aws_claude' else 'AKIA_TEST|private-sk|us-east-1',
        'idempotency_key': 'aws-template-subtype-001', **values})
    assert response.status_code == 200, response.text
    return response.json()


def test_two_subtypes_coexist_per_site_but_duplicate_subtype_rejected(db, login, aws):
    root = login('root')
    bedrock = create_template(root, aws)
    proxy = create_template(root, aws, 'aws_claude')
    assert bedrock['variant'] == 'bedrock' and proxy['variant'] == 'aws_claude'
    duplicate = root.post('/api/upload-templates', json={
        'site_id': aws[2][0].id, 'category_id': aws[0].id, 'format_id': aws[1]['aws_ak_sk'].id,
        'models': ['bedrock-model'], 'enabled': True})
    assert duplicate.status_code == 409
    assert root.get('/api/upload-templates').json()['total'] == 2
    assert 'variant' not in template_config(db.get(SiteUploadTemplate, bedrock['id']))


def test_public_formats_and_revisions_are_isolated_by_subtype(db, login, aws):
    root = login('root')
    bedrock = create_template(root, aws, 'aws_ak_sk')
    proxy = create_template(root, aws, 'aws_claude', index=1,
                            channel_config={'base_url': 'https://claude-live.api.aws'})
    client = login('user')
    item = client.get('/api/uploads/options').json()['items'][0]
    assert item['format_id'] == aws[1]['aws_bedrock'].id
    assert item['models'] == ['bedrock-model'] and item['target_count'] == 1
    assert {variant['variant'] for variant in item['variants']} == {'bedrock', 'aws_claude'}
    assert {fmt['id'] for variant in item['variants'] for fmt in variant['formats']} == {
        fmt.id for fmt in aws[1].values()}
    assert all(fmt['schema_config']['remote_type'] == (33 if variant['variant'] == 'bedrock' else 14)
        for variant in item['variants'] for fmt in variant['formats'])
    before = preview(client, aws).json()
    assert before['can_submit'] and before['targets'][0]['id'] == bedrock['site_id']
    assert root.patch('/api/upload-templates/' + proxy['id'], json={'models': ['new-proxy-model']}).status_code == 200
    after = preview(client, aws).json()
    assert after['configuration_revision'] == before['configuration_revision']
    other = preview(client, aws, 'aws_claude', api_base_url='https://claude-live.api.aws').json()
    assert other['can_submit'] and other['targets'][0]['id'] == proxy['site_id']
    assert other['models'] == ['new-proxy-model']
    invalid = preview(client, aws, models=['new-proxy-model']).json()
    assert not invalid['can_submit'] and any('模型列表' in error for error in invalid['errors'])


def test_disabled_configured_model_previews_stay_isolated_by_aws_subtype(db, login, aws):
    root = login('root')
    create_template(root, aws, 'aws_ak_sk', enabled=False)
    create_template(root, aws, 'aws_claude', enabled=False,
                    channel_config={'base_url': 'https://claude-live.api.aws'})
    client = login('user')
    option = client.get('/api/uploads/options').json()['items'][0]
    variants = {item['variant']: item for item in option['variants']}
    assert option['configured_models'] == ['bedrock-model']
    assert variants['bedrock']['configured_models'] == ['bedrock-model']
    assert variants['aws_claude']['configured_models'] == ['proxy-model']
    assert all(item['models'] == [] and not item['ready'] and item['target_count'] == 0
               for item in variants.values())
    wrong_variant = preview(client, aws, models=['proxy-model']).json()
    assert not wrong_variant['can_submit'] and any('模型列表' in error for error in wrong_variant['errors'])
    selected = preview(client, aws, 'aws_claude', models=['proxy-model']).json()
    assert not selected['can_submit'] and selected['configured_models'] == ['proxy-model']


def test_default_falls_back_to_claude_only_when_no_bedrock_target(login, aws):
    create_template(login('root'), aws, 'aws_claude', channel_config={'base_url': 'https://claude-live.api.aws'})
    options = login('user').get('/api/uploads/options').json()['items'][0]
    assert options['variant'] == 'aws_claude' and options['format_id'] == aws[1]['aws_claude'].id
    assert options['api_base_url_required'] is True
    assert preview(login('user'), aws, 'aws_claude', api_base_url='https://claude-live.api.aws').json()['can_submit']


def test_proxy_url_override_persists_and_reprepare_keeps_subtype(db, login, aws):
    root = login('root')
    create_template(root, aws)
    proxy = create_template(root, aws, 'aws_claude', channel_config={'base_url': 'https://claude-default.api.aws'})
    client = login('user')
    result = submit(client, aws, 'aws_claude', api_base_url='https://claude-live.api.aws/')
    item = db.get(TaskItem, result['items'][0]['id'])
    channel = db.get(Channel, item.channel_id)
    assert channel.upload_settings['api_base_url'] == 'https://claude-live.api.aws'
    assert item.snapshot['channel_type'] == 14 and item.snapshot['upload_template_id'] == proxy['id']
    assert item.snapshot['channel_config']['base_url'] == 'https://claude-live.api.aws'
    assert db.get(Distribution, item.distribution_id).template_snapshot['effective_channel_config']['base_url'] == 'https://claude-live.api.aws'
    task = db.get(Task, result['id'])
    task.status, item.status = 'cancelled', 'cancelled'
    db.commit()
    response = client.post('/api/tasks/' + task.id + '/reprepare-templates')
    assert response.status_code == 200, response.text
    fresh = db.get(TaskItem, response.json()['items'][0]['id'])
    assert fresh.snapshot['upload_template_id'] == proxy['id'] and fresh.snapshot['channel_type'] == 14
    assert fresh.snapshot['channel_config']['base_url'] == 'https://claude-live.api.aws'


def test_proxy_missing_url_blocks_upload_but_can_be_supplied_by_user(login, aws):
    create_template(login('root'), aws, 'aws_claude')
    client = login('user')
    assert client.get('/api/uploads/options').json()['items'][0]['api_base_url_required'] is True
    unavailable = preview(client, aws, 'aws_claude').json()
    assert not unavailable['can_submit']
    assert any('API 地址' in issue for target in unavailable['targets'] for issue in target['issues'])
    supplied = preview(client, aws, 'aws_claude', api_base_url='https://claude-live.api.aws').json()
    assert supplied['can_submit']


@pytest.mark.parametrize('address', ['https://api.aws', 'https://other.example', 'http://claude-live.api.aws',
                                    'https://claude-live.api.aws/v1', 'https://user:secret@claude-live.api.aws',
                                    'https://bedrock-mantle.us-east-1.api.aws'])
def test_template_url_is_discarded_and_illegal_upload_url_is_rejected(login, aws, address):
    root = login('root')
    response = root.post('/api/upload-templates', json={
        'site_id': aws[2][0].id, 'category_id': aws[0].id, 'format_id': aws[1]['aws_claude'].id,
        'models': ['proxy-model'], 'enabled': False, 'channel_config': {'base_url': address}})
    assert response.status_code == (422 if 'user:secret@' in address else 201)
    if response.status_code == 201:
        assert response.json()['channel_config'] == {'status': 2}
    assert preview(login('user'), aws, 'aws_claude', api_base_url=address).status_code == 422


def test_bedrock_rejects_proxy_url_override_and_accepts_mixed_keys(login, aws):
    create_template(login('root'), aws)
    client = login('user')
    invalid = preview(client, aws, api_base_url='https://claude-live.api.aws').json()
    assert not invalid['can_submit'] and any('仅用于' in issue for issue in invalid['errors'])
    valid = preview(client, aws, credentials='AKIA_TEST|private-sk|us-east-1\nprivate-api-key|eu-west-1').json()
    assert valid['can_submit'] and valid['valid_count'] == 2


def test_merged_format_matches_legacy_channel_without_changing_historical_schema(db, login, aws):
    root = login('root')
    create_template(root, aws, 'aws_ak_sk')
    client = login('user')
    original = submit(client, aws, 'aws_ak_sk')
    channel = db.get(Channel, original['items'][0]['channel_id'])
    create_template(root, aws, 'aws_bedrock', index=1)
    plan = preview(client, aws).json()
    assert plan['can_submit'] and plan['rows'][0]['status'] == 'redistribute'
    result = submit(client, aws, idempotency_key='aws-legacy-supplement-002')
    item = db.get(TaskItem, result['items'][0]['id'])
    db.refresh(channel)
    assert channel.format_id == aws[1]['aws_ak_sk'].id
    assert item.snapshot['format_code'] == aws[1]['aws_ak_sk'].code
    assert item.snapshot['format_schema'] == aws[1]['aws_ak_sk'].schema_config
