"""Platform demand-minus-declared-capacity, with isolated metadata fixtures only."""
import json
from types import SimpleNamespace

import pytest
from app.bootstrap import seed_catalog
from app.db import uid
from app.models import Category, CredentialFormat, Site, SiteUploadTemplate
from app.models_channels import Channel, Distribution, UnclaimedChannel, UploadGroup
from sqlalchemy import event, select


@pytest.fixture
def capacity_case(db, users, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Capacity planning must never decrypt credentials or access remote services')
    monkeypatch.setattr('app.security.decrypt', forbidden)
    monkeypatch.setattr('app.adapters.silicon.safe_request', forbidden)
    seed_catalog(db)
    categories = {row.family: row for row in db.scalars(select(Category))}
    formats = {(row.category_id, row.code): row for row in db.scalars(select(CredentialFormat))}

    def site(*, archived=False, enabled=True):
        token = uid()
        row = Site(name='Private site ' + token, prefix=token[:12], base_url='https://' + token + '.invalid',
            seller_user_id='private-seller', token_encrypted='must-not-read-site-token',
            enabled=enabled, archived=archived)
        db.add(row)
        db.flush()
        return row

    first = site()

    def template(*, family='OpenAI', code='api_key-v1', target=None, models=None, rpm=None, tpm=None,
                 enabled=True, mapping=None):
        category = categories[family]
        fmt = formats[(category.id, code)]
        row = SiteUploadTemplate(site_id=(target or first).id, category_id=category.id, format_id=fmt.id,
            variant=uid()[:20], models=['model-a'] if models is None else models, enabled=enabled,
            model_rpm_requirements=rpm or {}, model_tpm_requirements=tpm or {}, model_mapping=mapping or {})
        db.add(row)
        db.flush()
        return row

    def channel(*, family='OpenAI', code='api_key-v1', owner='user', models=None, account=None,
                archived=False, key_count=1):
        category = categories[family]
        fmt = formats[(category.id, code)]
        group = UploadGroup(owner_id=users[owner].id, category_id=category.id, format_id=fmt.id, tag=uid())
        db.add(group)
        db.flush()
        row = Channel(owner_id=users[owner].id, group_id=group.id, category_id=category.id, format_id=fmt.id,
            key_encrypted='must-not-read-channel-key', key_hint='private-hint', fingerprint=uid(),
            models=['model-a'] if models is None else models, archived=archived,
            key_count=key_count, key_mode='multiple' if key_count > 1 else 'single',
            upload_settings={'account_info': account} if account is not None else {})
        db.add(row)
        db.flush()
        return row

    def distribution(channel, *, target=None, models=None, status='enabled', remote_id='generated',
                     legacy=None, tombstone=False):
        row = Distribution(channel_id=channel.id, site_id=(target or first).id,
            remote_id=uid() if remote_id == 'generated' else remote_id, remote_name='private-remote-name',
            models=channel.models if models is None else models, partition_key=uid(), key_count=channel.key_count,
            status=status, test_status='failed', template_snapshot=legacy or {},
            remote_snapshot={'_local_deletion': {'remote_confirmed': False}} if tombstone else {})
        db.add(row)
        db.flush()
        return row

    def unclaimed(*, target=None, adopted=None):
        row = UnclaimedChannel(site_id=(target or first).id, remote_id=uid(), remote_name='private-unclaimed',
            adopted_channel_id=adopted.id if adopted else None,
            snapshot={'models': 'unclaimed-only', 'rpm': 999999, 'tpm': 9999999})
        db.add(row)
        db.flush()
        return row

    db.commit()
    return SimpleNamespace(categories=categories, formats=formats, first=first, site=site, template=template,
                           channel=channel, distribution=distribution, unclaimed=unclaimed)


def read(client, **params):
    response = client.get('/api/model-gaps', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def rows(result):
    return {(row['category_id'], row['variant'], row['model']): row for row in result['items']}


def test_platform_all_roles_aggregate_all_owners_without_private_identifiers_or_secret_columns(db, users, login, capacity_case):
    case = capacity_case
    case.template(rpm={'model-a': 100}, tpm={'model-a': 1000})
    private_ids = [user.id for user in users.values()]
    for index in range(53):
        channel = case.channel(owner=('user', 'sibling', 'other_user', 'admin')[index % 4],
                               account={'rpm': 1, 'tpm': 3})
        dist = case.distribution(channel)
        private_ids.extend([channel.id, channel.group_id, dist.id, dist.remote_id])
    db.commit()
    results = []
    for role in ('root', 'admin', 'user'):
        client = login(role)
        queries = []
        def capture(conn, cursor, statement, parameters, context, executemany, queries=queries):
            queries.append(statement)
        event.listen(db.bind, 'before_cursor_execute', capture)
        try:
            result = read(client, scope='mine', owner_id=users['other_user'].id)
        finally:
            event.remove(db.bind, 'before_cursor_execute', capture)
        assert result['scope'] == 'platform' and result['capacity_basis'] == 'declared_channel'
        assert result['total'] == 1
        row = result['items'][0]
        assert (row['channel_count'], row['supplied_rpm'], row['supplied_tpm']) == (53, '53', '159')
        assert (row['gap_rpm'], row['gap_tpm']) == ('47', '841')
        assert row['data_status'] == 'declared' and not row['rpm_gap_estimated']
        encoded = json.dumps(result)
        assert not any(value in encoded for value in [*private_ids, case.first.id, case.first.name,
            'must-not-read', 'private-remote', 'private-hint', 'private-seller', 'upload_settings', 'snapshot'])
        for statement in queries:
            assert 'channels.key_encrypted' not in statement and 'sites.token_encrypted' not in statement
        results.append(result['items'])
    assert results[0] == results[1] == results[2]


def test_requires_login_empty_platform_and_retired_target_endpoints(client, login, capacity_case):
    assert client.get('/api/model-gaps').status_code == 401
    result = read(login('user'))
    assert result['items'] == [] and result['total'] == 0 and result['unclaimed_channel_count'] == 0
    root = login('root')
    assert root.get('/api/model-targets').status_code == 404
    assert root.put('/api/model-targets', json={'items': []}).status_code == 404


def test_demand_sums_saved_templates_including_disabled_but_not_archived_sites(db, login, capacity_case):
    case = capacity_case
    case.template(models=['model-a', 'unset'], rpm={'model-a': 70}, mapping={'model-a': 'mapped-upstream'})
    case.template(target=case.site(enabled=False), models=['model-a'], enabled=False,
                  rpm={'model-a': 30}, tpm={'model-a': 2000})
    case.template(target=case.site(archived=True), models=['model-a', 'archived-only'],
                  rpm={'model-a': 999, 'archived-only': 999})
    db.commit()
    result = read(login('user'))
    keyed = rows(result)
    row = keyed[(case.categories['OpenAI'].id, '', 'model-a')]
    assert (row['required_rpm'], row['required_tpm']) == ('100', '2000')
    assert (row['supplied_rpm'], row['gap_rpm'], row['channel_count']) == ('0', '100', 0)
    assert not row['rpm_gap_estimated']
    unset = keyed[(case.categories['OpenAI'].id, '', 'unset')]
    assert unset['required_rpm'] is unset['required_tpm'] is unset['gap_rpm'] is unset['gap_tpm'] is None
    assert {row['model'] for row in result['items']} == {'model-a', 'unset'}


def test_supply_counts_local_channel_once_per_actual_model_across_sites_partitions_and_keys(db, login, capacity_case):
    case = capacity_case
    channel = case.channel(models=['model-a', 'model-b', 'not-distributed'], account={'rpm': 50, 'tpm': 1000}, key_count=7)
    case.distribution(channel, models=['model-a', 'model-a'])
    case.distribution(channel, target=case.site(enabled=False), models=['model-b', 'supply-only'], status='disabled')
    case.distribution(channel, models=['model-a', 'model-b'])
    case.distribution(channel, models=['failed-only'], status='failed')
    db.commit()
    result = read(login('admin'))
    assert {row['model'] for row in result['items']} == {'model-a', 'model-b', 'supply-only'}
    for row in result['items']:
        assert row['channel_count'] == 1 and row['supplied_rpm'] == '50' and row['supplied_tpm'] == '1000'
        assert row['gap_rpm'] is None and row['required_rpm'] is None


def test_nonexistent_archived_or_removed_distributions_supply_no_capacity(db, login, capacity_case):
    case = capacity_case
    for status in ('pending', 'failed', 'missing', 'deleted', 'unavailable', 'error', 'needs_review'):
        case.distribution(case.channel(models=[status], account={'rpm': 100}), status=status)
    for remote_id in (None, ''):
        case.distribution(case.channel(models=['no-remote'], account={'rpm': 100}), remote_id=remote_id)
    case.distribution(case.channel(models=['archived-channel'], account={'rpm': 100}, archived=True))
    case.distribution(case.channel(models=['archived-site'], account={'rpm': 100}), target=case.site(archived=True))
    case.distribution(case.channel(models=['old-tombstone'], account={'rpm': 100}), tombstone=True)
    # Existing disabled inventory counts despite an absent/failed connectivity test.
    case.distribution(case.channel(models=['existing-disabled'], account={'rpm': 4}), status='disabled')
    db.commit()
    result = read(login('root'))
    assert [row['model'] for row in result['items']] == ['existing-disabled']
    assert result['items'][0]['supplied_rpm'] == '4'


def test_primary_declarations_override_frozen_values_and_zero_is_known(db, login, capacity_case):
    case = capacity_case
    case.template(rpm={'model-a': 10}, tpm={'model-a': 100})
    channel = case.channel(account={'rpm': 0, 'tpm': None})
    case.distribution(channel, legacy={'effective_channel_config': {'account_info': {'rpm': 999, 'tpm': 999}}})
    db.commit()
    row = read(login('user'))['items'][0]
    assert row['supplied_rpm'] == row['supplied_tpm'] == '0'
    assert row['unknown_rpm_channels'] == 0 and row['unknown_tpm_channels'] == 1
    assert not row['rpm_gap_estimated'] and row['tpm_gap_estimated']
    assert row['data_status'] == 'partial'


def test_500_rpm_demand_minus_250_supply_and_independent_unknown_tpm(db, login, capacity_case):
    case = capacity_case
    case.template(rpm={'model-a': 500}, tpm={'model-a': 1_000_000})
    case.distribution(case.channel(account={'rpm': 250}))
    db.commit()
    row = read(login('user'))['items'][0]
    assert (row['required_rpm'], row['supplied_rpm'], row['gap_rpm']) == ('500', '250', '250')
    assert row['unknown_rpm_channels'] == 0 and not row['rpm_gap_estimated']
    assert row['unknown_tpm_channels'] == 1 and row['tpm_gap_estimated']
    assert row['gap_tpm'] == '1000000'


@pytest.mark.parametrize('marker', [False, True, {'remote_confirmed': True}, {}])
def test_only_unconfirmed_local_deletion_markers_exclude_existing_supply(db, login, capacity_case, marker):
    case = capacity_case
    dist = case.distribution(case.channel(account={'rpm': 25, 'tpm': 50}))
    dist.remote_snapshot = {'_local_deletion': marker}
    db.commit()
    row = read(login('user'))['items'][0]
    assert row['channel_count'] == 1 and row['supplied_rpm'] == '25'


@pytest.mark.parametrize(('legacy', 'expected_rpm', 'unknown'), [
    ([{'rpm': 11, 'tpm': 100}, {'rpm': 11, 'tpm': 100}], '11', 0),
    ([{'rpm': 11, 'tpm': 100}, {'rpm': 12, 'tpm': 100}], '0', 1),
    ([{'rpm': 11, 'tpm': 100}, {'tpm': 100}], '0', 1),
])
def test_legacy_capacity_fallback_requires_consistent_copies_without_summing(db, login, capacity_case, legacy, expected_rpm, unknown):
    case = capacity_case
    channel = case.channel()
    for account in legacy:
        case.distribution(channel, legacy={'effective_channel_config': {'account_info': account}})
    db.commit()
    row = read(login('user'))['items'][0]
    assert row['supplied_rpm'] == expected_rpm and row['unknown_rpm_channels'] == unknown
    assert row['supplied_tpm'] == '100' and row['unknown_tpm_channels'] == 0
    assert row['channel_count'] == 1


@pytest.mark.parametrize('invalid', [True, False, '123', 1.0, -1, None, [], {}])
def test_invalid_or_missing_capacity_is_unknown_and_never_replaced_by_rpm_limiter(db, login, capacity_case, invalid):
    case = capacity_case
    case.template(rpm={'model-a': 10}, tpm={'model-a': 100})
    channel = case.channel(account={'rpm': invalid, 'tpm': invalid})
    case.distribution(channel, legacy={'effective_channel_config': {'account_info': {'rpm': 999, 'tpm': 999},
        'rpm_enabled': True, 'rpm_limit': 9999}})
    db.commit()
    row = read(login('user'))['items'][0]
    assert row['supplied_rpm'] == row['supplied_tpm'] == '0'
    assert row['unknown_rpm_channels'] == row['unknown_tpm_channels'] == 1
    assert row['gap_rpm'] == '10' and row['gap_tpm'] == '100'
    assert row['rpm_gap_estimated'] and row['tpm_gap_estimated']


def test_large_integer_capacity_remains_exact_and_satisfied_gaps_are_not_estimated(db, login, capacity_case):
    case = capacity_case
    case.template(rpm={'model-a': 100}, tpm={'model-a': 1000})
    huge = 10 ** 80 + 17
    for account in ({'rpm': huge, 'tpm': huge + 1}, {'rpm': huge, 'tpm': huge + 1}, {}):
        case.distribution(case.channel(account=account))
    db.commit()
    row = read(login('user'))['items'][0]
    assert row['supplied_rpm'] == str(huge * 2) and row['supplied_tpm'] == str((huge + 1) * 2)
    assert row['gap_rpm'] == row['gap_tpm'] == '0'
    assert row['unknown_rpm_channels'] == row['unknown_tpm_channels'] == 1
    assert not row['rpm_gap_estimated'] and not row['tpm_gap_estimated']


@pytest.mark.parametrize(('family', 'definitions'), [
    ('AWS', [('newapi-33-aws-ak-sk-v1', 'bedrock'), ('newapi-33-aws-api-key-v1', 'bedrock'),
             ('newapi-14-aws-claude-v1', 'aws_claude')]),
    ('Azure', [('newapi-3-azure-gpt-v1', 'azure_gpt'), ('newapi-14-azure-claude-v1', 'azure_claude')]),
    ('Google', [('newapi-24-api-key-v1', 'ai_studio_gemini'), ('newapi-41-vertex-gemini-v1', 'vertex_gemini'),
                ('newapi-41-vertex-claude-v1', 'vertex_claude')]),
])
def test_service_variants_and_original_model_mapping_do_not_mix_supply(db, login, capacity_case, family, definitions):
    case = capacity_case
    expected = {}
    for code, variant in definitions:
        case.template(family=family, code=code, models=['original'], rpm={'original': 20},
                      mapping={'original': 'must-not-create-a-target-row'})
        case.distribution(case.channel(family=family, code=code, models=['original'], account={'rpm': 5}))
        expected[variant] = expected.get(variant, 0) + 1
    # Same model in another provider is a separate supply pool.
    case.distribution(case.channel(models=['original'], account={'rpm': 999}))
    db.commit()
    result = rows(read(login('user')))
    for variant, count in expected.items():
        row = result[(case.categories[family].id, variant, 'original')]
        assert row['required_rpm'] == str(20 * count) and row['supplied_rpm'] == str(5 * count)
        assert row['channel_count'] == count and row['type_label'] and row['type_label_en']
    assert all(model != 'must-not-create-a-target-row' for _, _, model in result)


def test_unclaimed_count_is_platform_only_and_supplies_no_invented_models_or_capacity(db, login, capacity_case):
    case = capacity_case
    case.unclaimed()
    case.unclaimed(target=case.site(enabled=False))
    case.unclaimed(target=case.site(archived=True))
    case.unclaimed(adopted=case.channel())
    db.commit()
    result = read(login('user'))
    assert result['unclaimed_channel_count'] == 2 and result['items'] == []
    assert 'private-unclaimed' not in json.dumps(result)
