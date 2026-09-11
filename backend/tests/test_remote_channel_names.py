"""Readable remote names stay tied to the original group and reserved identity."""
# ruff: noqa: F811
import pytest
import test_multikey_containers
from app.models_channels import (
    Channel,
    ChannelCredential,
    Distribution,
    KeyVersion,
    Task,
    TaskItem,
    UnclaimedChannel,
    UploadGroup,
)
from app.remote_channel_names import new_remote_channel_name
from fastapi import HTTPException
from sqlalchemy import func, select
from test_catalog_policy import catalog_setup  # noqa: F401
from test_channel_reupload import failed_case, send  # noqa: F401
from test_channels import payload as advanced_payload
from test_channels import setup_catalog  # noqa: F401
from test_multikey_containers import (
    AWS_KEYS,
    complete,
    container_site,  # noqa: F401
    remotes,  # noqa: F401
    request_body,
    setup_template,
)
from test_multikey_containers import (
    post as container_submit,
)
from test_upload_templates import (
    add_templates,
    catalog,  # noqa: F401
    no_network,  # noqa: F401
    template_body,
    upload,
)
from test_upload_templates import (
    submit as simple_submit,
)


@pytest.fixture(autouse=True)
def remote_defaults(monkeypatch):
    # The shared container fake normally receives explicit wire options. Plain
    # API-key uploads rely on the real adapter's channel_type/config defaults.
    original = test_multikey_containers.create_payload

    def create_with_defaults(*, channel_type=1, config=None, **values):
        return original(channel_type=channel_type, config=config, **values)

    monkeypatch.setattr(test_multikey_containers, 'create_payload', create_with_defaults)


def group_for(db, result):
    channel = db.get(Channel, result['items'][0]['channel_id'])
    return db.get(UploadGroup, channel.group_id)


def test_simple_names_use_full_chinese_site_name_and_server_tag(db, login, catalog, remotes):
    sites = catalog[2]
    sites[0].name, sites[0].prefix = '三野正式站点', 'DO-NOT-USE-PREFIX'
    sites[1].name, sites[1].prefix = '中文备用站', 'OTHER-PREFIX'
    db.commit()
    add_templates(login('root'), catalog, count=2)
    client = login('user')
    batch = 'remote-naming-label-batch'
    label = client.get('/api/uploads/label', params={
        'category_id': catalog[0].id, 'format_id': catalog[1].id, 'batch_token': batch})
    assert label.status_code == 200, label.text
    result = simple_submit(client, catalog, batch_token=batch)
    group = group_for(db, result)
    assert group.tag == label.json()['group_tag'] == result['group_tag']
    expected = {site.id: f'{site.name}-{group.tag}' for site in sites[:2]}
    distributions = list(db.scalars(select(Distribution)))
    assert {row.site_id: row.remote_name for row in distributions} == expected
    complete(db, result)
    assert {row['site_id']: row['name'] for row in remotes[1]} == expected
    assert all(row.remote_id for row in distributions)


def test_mixed_aws_partitions_get_distinct_stable_ordinals_and_both_create(db, login, container_site, remotes):
    site = container_site
    site.name, site.prefix = '中文 Bedrock 站点', 'ignored'
    db.commit()
    category, fmt = setup_template(db, login('root'), site)
    result = container_submit(login('user'), request_body(category, fmt, AWS_KEYS))
    group = group_for(db, result)
    base = f'{site.name}-{group.tag}'
    distributions = list(db.scalars(select(Distribution)))
    assert len(distributions) == 2 and len({row.partition_key for row in distributions}) == 2
    assert {row.remote_name for row in distributions} == {base, base + '-2'}
    complete(db, result)
    assert len(remotes[1]) == 2
    assert {row['name'] for row in remotes[1]} == {base, base + '-2'}
    assert len({row.remote_id for row in distributions}) == 2


def test_advanced_keys_share_original_tag_but_not_remote_name(db, login, setup_catalog, remotes):
    site = setup_catalog[2][0]
    site.name, site.prefix = '高级上传站点', 'unused-prefix'
    for other in setup_catalog[2][1:]:
        other.enabled = False
    db.commit()
    client = login('user')
    body = advanced_payload(setup_catalog, keys='naming-key-alpha\nnaming-key-beta')
    response = client.post('/api/uploads/submit', json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result['items']) == 2
    group = group_for(db, result)
    assert db.scalar(select(func.count()).select_from(UploadGroup)) == 1
    assert db.scalar(select(func.count()).select_from(Channel)) == 2
    base = f'{site.name}-{group.tag}'
    assert set(db.scalars(select(Distribution.remote_name))) == {base, base + '-2'}
    complete(db, result)
    assert len(remotes[1]) == 2 and {row['name'] for row in remotes[1]} == {base, base + '-2'}
    replay = client.post('/api/uploads/submit', json=body)
    assert replay.status_code == 200 and replay.json()['id'] == result['id']
    assert db.scalar(select(func.count()).select_from(Distribution)) == 2


def test_advanced_redistribute_uses_original_tag_and_current_target_name(db, login, setup_catalog, remotes):
    first, second, third = setup_catalog[2]
    second.enabled = third.enabled = False
    db.commit()
    client = login('user')
    response = client.post('/api/uploads/submit', json=advanced_payload(setup_catalog))
    assert response.status_code == 200, response.text
    original = response.json()
    complete(db, original)
    group = group_for(db, original)
    tag, channel_id = group.tag, original['items'][0]['channel_id']
    original_dist = db.get(Distribution, original['items'][0]['distribution_id'])
    frozen_name = original_dist.remote_name
    group.name = '只修改展示名称，不是上传标签'
    first.name = '原站点已改名'
    second.name, second.enabled = '补分发目标中文名', True
    db.commit()
    response = client.post('/api/channels/' + channel_id + '/actions', json={
        'action': 'redistribute', 'site_ids': [second.id]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['total'] == 1
    created = db.get(Distribution, result['items'][0]['distribution_id'])
    assert created.remote_name == f'{second.name}-{tag}'
    assert db.get(Distribution, original_dist.id).remote_name == frozen_name
    complete(db, result)
    assert remotes[1][-1]['name'] == f'{second.name}-{tag}'
    assert db.scalar(select(func.count()).select_from(UploadGroup)) == 1


def test_template_supplement_ignores_new_batch_label_and_edited_group_title(db, login, catalog, remotes):
    root, client = login('root'), login('user')
    add_templates(root, catalog, count=1)
    original = simple_submit(client, catalog)
    complete(db, original)
    group = group_for(db, original)
    tag, group_id = group.tag, group.id
    group.name = '用户修改的分组名称'
    target = catalog[2][1]
    target.name = '新增目标站'
    db.commit()
    response = root.post('/api/upload-templates', json=template_body(catalog, index=1, models=['model-a']))
    assert response.status_code == 201, response.text
    batch = 'new-supplemental-batch-label'
    new_label = client.get('/api/uploads/label', params={
        'category_id': catalog[0].id, 'format_id': catalog[1].id, 'batch_token': batch}).json()['group_tag']
    assert new_label != tag
    result = simple_submit(client, catalog, batch_token=batch, idempotency_key='supplemental-naming-0002')
    assert result['total'] == 1
    channel = db.get(Channel, result['items'][0]['channel_id'])
    dist = db.get(Distribution, result['items'][0]['distribution_id'])
    assert channel.group_id == group_id and dist.remote_name == f'{target.name}-{tag}'
    assert dist.remote_name != f'{target.name}-{new_label}'
    complete(db, result)
    assert remotes[1][-1]['name'] == f'{target.name}-{tag}'
    assert db.scalar(select(func.count()).select_from(UploadGroup)) == 1


def test_retry_preserves_legacy_frozen_name_after_site_and_group_title_changes(db, login, catalog, remotes):
    add_templates(login('root'), catalog, count=1)
    client = login('user')
    original = simple_submit(client, catalog)
    item = db.get(TaskItem, original['items'][0]['id'])
    dist = db.get(Distribution, item.distribution_id)
    legacy = 'legacy-prefix-original-stable-name'
    dist.remote_name, dist.status = legacy, 'failed'
    item.status, item.stage = 'failed', 'validation'
    db.get(Task, item.task_id).status = 'failed'
    catalog[2][0].name = '重试之前已改站点名'
    group_for(db, original).name = '重试之前已改分组标题'
    db.commit()
    response = client.post('/api/tasks/' + original['id'] + '/retry')
    assert response.status_code == 200, response.text
    complete(db, response.json())
    assert db.get(Distribution, dist.id).remote_name == legacy
    assert [row['name'] for row in remotes[1]] == [legacy]


def test_reupload_keeps_original_distribution_name_instead_of_allocating_again(db, failed_case):
    case = failed_case
    legacy = 'unchanged-name-from-original-failed-upload'
    case.dist.remote_name = legacy
    case.site.name = '重新上传前修改站点名'
    db.get(UploadGroup, case.channel.group_id).name = '重新上传前修改组标题'
    db.commit()
    response = send(case)
    assert response.status_code == 200, response.text
    complete(db, response.json())
    assert db.get(Distribution, case.dist.id).remote_name == legacy
    assert [row['name'] for row in case.remotes[1]] == [legacy]


@pytest.mark.parametrize('entrypoint', ['simple', 'advanced'])
def test_overlong_combined_name_rejects_whole_upload_without_truncating(db, login, request, monkeypatch, entrypoint):
    data = request.getfixturevalue('catalog' if entrypoint == 'simple' else 'setup_catalog')
    data[2][0].name = '正常短名称'
    data[2][1].name = '站' * 120  # Valid Site.name; the added immutable tag exceeds the remote limit.
    data[2][2].enabled = False
    db.commit()
    # Exercise the valid longer tag produced for an account with a longer numeric ID.
    long_tag = '123456-OpenAI-api-key-20260909-15:30:45-ab12'
    monkeypatch.setattr('app.upload_templates.new_upload_group_tag', lambda *args: long_tag)
    monkeypatch.setattr('app.routers.uploads.new_upload_group_tag', lambda *args: long_tag)
    assert len(data[2][1].name + '-' + long_tag) > 160
    if entrypoint == 'simple':
        add_templates(login('root'), data, count=2)
        path, body = '/api/uploads/simple-submit', upload(data, idempotency_key='overlong-naming-simple')
    else:
        path, body = '/api/uploads/submit', advanced_payload(data, nonce='overlong-naming-advanced')
    response = login('user').post(path, json=body)
    assert response.status_code == 422, response.text
    assert '160' in response.text and '缩短站点名称' in response.text
    db.expire_all()
    for model in (UploadGroup, Channel, ChannelCredential, KeyVersion, Distribution, Task, TaskItem):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    assert data[2][1].name == '站' * 120


def test_existing_unclaimed_and_unfinished_cleanup_names_are_reserved(db, users, login, catalog):
    add_templates(login('root'), catalog, count=1)
    result = simple_submit(login('user'), catalog)
    channel = db.get(Channel, result['items'][0]['channel_id'])
    site, group = catalog[2][0], group_for(db, result)
    base = f'{site.name}-{group.tag}'
    existing = db.get(Distribution, result['items'][0]['distribution_id'])
    existing.status = 'deleted'  # Retained rows still own their stable names.
    task = Task(actor_id=users['user'].id, owner_id=users['user'].id,
                actor_session_version=users['user'].session_version, kind='force_delete_async', status='failed')
    db.add(task)
    db.flush()
    cleanup = TaskItem(task_id=task.id, site_id=site.id, operation='remote_cleanup', status='pending',
                       snapshot={'delete_target': {'id': '900', 'name': base + '-2', 'type': 1}})
    db.add_all([cleanup, UnclaimedChannel(site_id=site.id, remote_id='901', remote_name=base + '-3', snapshot={})])
    db.commit()
    for state in ('pending', 'failed', 'cancelled', 'needs_review'):
        cleanup.status = state
        db.commit()
        assert new_remote_channel_name(db, site, channel) == base + '-4'
    cleanup.snapshot = {**cleanup.snapshot, 'delete_acknowledged': True}
    db.commit()
    assert new_remote_channel_name(db, site, channel) == base + '-2'


def test_exact_160_character_base_is_preserved_but_required_suffix_is_rejected(db, login, catalog):
    add_templates(login('root'), catalog, count=1)
    result = simple_submit(login('user'), catalog)
    channel = db.get(Channel, result['items'][0]['channel_id'])
    site, group = catalog[2][0], group_for(db, result)
    site.name, group.tag = '站' * 120, 't' * 39
    db.commit()
    base = new_remote_channel_name(db, site, channel)
    assert len(base) == 160 and base == site.name + '-' + group.tag
    dist = db.get(Distribution, result['items'][0]['distribution_id'])
    dist.remote_name = base
    db.commit()
    with pytest.raises(HTTPException) as caught:
        new_remote_channel_name(db, site, channel)
    assert caught.value.status_code == 422 and '160' in caught.value.detail
    assert dist.remote_name == base and group.tag == 't' * 39 and site.name == '站' * 120
