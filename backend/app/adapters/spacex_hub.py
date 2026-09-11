"""SpaceX Hub templates and their explicitly selected downstream mappings.

Hub IDs are not New API channel IDs. No inherited standard management route is
allowed. Creating a template starts asynchronous publication exactly once.
"""
import re

from ..newapi_formats import get_format_specs
from ..template_settings import ChannelConfig
from .channel_config import checked_config, create_payload
from .channel_observation import extract_usage, test_result
from .silicon import RemoteError, SiliconAdapter
from .spacex_hub_build import BUILD, BUILD_ID, BUILD_REVIEWED_AT, ORIGIN, identify_build

PREFIX = '/api/admin-hub/channels'
PLATFORMS = {
    1: ('openai_gpt', 'openai.gpt'),
    3: ('azure_gpt', 'azure.gpt'),
    14: ('anthropic_claude', 'anthropic.claude'),
    20: ('openrouter_claude', 'openrouter.claude'),
    24: ('ai_studio_gemini', 'ai_studio.gemini'),
    33: ('aws_claude', 'aws.claude'),
    41: ('vertex_gemini', 'vertex_ai.gemini'),
}
SUPPLIER_CREATE_REASON = 'SpaceX 主供应商账号只负责管理；上传 Key 需要已启用并有上传权限的用户子账号'
USER_CREATE_REASON = 'SpaceX 用户子账号尚未获得明确的渠道上传权限'
PUBLISH_OK = frozenset({'created', 'updated', 'site_configured', 'succeeded'})
TASK_OK = frozenset({'succeeded', ''})


def template_config_issues(cap, schema, config, *, routing_group=None, proxy=False):
    """Local preparation checks; runtime repeats these against fresh metadata."""
    cap, schema = cap or {}, schema or {}
    issues = []
    try:
        cfg = ChannelConfig.model_validate({k: v for k, v in (config or {}).items()
                                           if k != 'credential_format'}).model_dump(mode='json')
    except (ValueError, TypeError, AttributeError):
        return ['SpaceX 渠道模板配置无效']
    channel_type, kind = schema.get('remote_type', 1), schema.get('type', 'api_key')
    if channel_type not in PLATFORMS:
        issues.append('SpaceX 尚未核实此渠道类型')
    if kind in ('vertex_claude', 'vertex_api_key'):
        issues.append('SpaceX 的 Vertex 接入目前仅支持 Gemini 服务账号 JSON，尚不支持 Claude 或 Vertex API Key')
    elif kind not in ('api_key', 'aws_ak_sk', 'aws_api_key', 'aws_bedrock', 'vertex_json', 'vertex_gemini', 'azure_gpt'):
        issues.append('SpaceX 尚未核实此凭据服务与平台类型的对应关系')
    if cfg['status'] != cap.get('hub_create_status'):
        issues.append('SpaceX 创建状态由远端站点决定；请将模板状态设为与远端一致，系统不会自动修改启停状态')
    if type(cap.get('hub_target_site_id')) is not int or cap['hub_target_site_id'] <= 0:
        issues.append('SpaceX 上传需要账号只有一个明确可见的目标站点')
    if proxy:
        issues.append('SpaceX 尚不支持此代理配置')
    defaults = ChannelConfig().model_dump(mode='json')
    if any(cfg[field] != defaults[field] for field in ('organization', 'weight', 'auto_ban',
            'model_mapping', 'status_code_mapping', 'azure_responses_version', 'rpm_enabled', 'rpm_limit')):
        issues.append('SpaceX 尚不支持模板中的组织、权重、自动禁用、映射、Responses 版本或 RPM 设置')
    if cfg['other'] and channel_type not in (3, 24, 41):
        issues.append('SpaceX 此平台类型不支持额外参数')
    if routing_group is not None and (not isinstance(routing_group, str)
            or any(not part or part not in cap.get('groups', []) for part in routing_group.split(','))):
        issues.append('SpaceX 目标站点不允许所选分组')
    return issues


class SpaceXHubAdapter(SiliconAdapter):
    VERSION = BUILD_ID
    SELLER_API = False
    CHANNEL_TYPES = frozenset(PLATFORMS)
    SUPPORTS_PROXY = False
    SUPPORTS_RPM = False
    OBJECT_SETTINGS = True

    @staticmethod
    def _unsupported(message='SpaceX Hub 尚未支持此操作'):
        raise RemoteError(message, category='unsupported')

    @staticmethod
    def _invalid(message='SpaceX Hub 响应结构不符合已核对的接口'):
        raise RemoteError(message, category='protocol_error')

    def _request_envelope(self, method, path, **kwargs):
        # A final route firewall also protects against future inherited helpers.
        get_paths = {'/api/status', '/api/user/self', PREFIX, PREFIX + '/platform-types',
                     PREFIX + '/model-options', '/api/admin-hub/usage-logs/sites'}
        read = method == 'GET' and (path in get_paths or re.fullmatch(
            r'/api/admin-hub/(?:channels/[1-9][0-9]*(?:/realtime-used-quota)?|sites/[1-9][0-9]*/groups|site-publish-fields/[1-9][0-9]*)', path))
        write = method == 'POST' and (path == PREFIX or re.fullmatch(
            r'/api/admin-hub/channels/[1-9][0-9]*/(?:status|test)', path))
        if not read and not write:
            self._unsupported('SpaceX Hub 不允许调用未经核实的管理接口')
        if self.site.base_url.rstrip('/') != ORIGIN:
            raise RemoteError('SpaceX Hub 适配器仅支持已核对的站点地址', category='configuration_error')
        if write:
            previous = getattr(self, '_hub_identity', None)
            current = self.permissions()
            if previous is None or current != previous:
                raise RemoteError('SpaceX 写入前账号身份或权限发生变化', category='identity_mismatch')
            if path == PREFIX and current['can_write'] is not True:
                raise RemoteError(current['create_block_reason'], category='permission_denied')
        return super()._request_envelope(method, path, **kwargs)

    def _version(self):
        if self.site.base_url.rstrip('/') != ORIGIN:
            raise RemoteError('SpaceX Hub 适配器仅支持已核对的站点地址', category='configuration_error')
        version = identify_build(self.site.base_url, self.request('GET', '/api/status'), self.transport)
        if version != BUILD_ID or getattr(self, 'frozen_build_id', version) != version:
            raise RemoteError('SpaceX Hub 构建与冻结任务不一致，请重新准备任务',
                              category='protocol_error', reason='build_changed')
        return version

    def permissions(self):
        self._current_version = self._version()
        identity = self.request('GET', '/api/user/self')
        if (not isinstance(identity, dict) or type(identity.get('id')) is not int or identity['id'] <= 0
                or str(identity['id']) != str(self.site.seller_user_id)):
            raise RemoteError('SpaceX 令牌用户编号与本地绑定不匹配', category='identity_mismatch')
        hub = identity.get('admin_hub')
        role = identity.get('role')
        if (type(identity.get('status')) is not int or identity['status'] != 1
                or type(role) is not int or role not in (5, 8) or not isinstance(hub, dict)
                or hub.get('role') != {5: 'user', 8: 'supplier'}[role]
                or type(hub.get('is_primary_account')) is not int
                or hub['is_primary_account'] != (1 if role == 8 else 0)
                or type(hub.get('supplier_id')) is not int or hub['supplier_id'] <= 0):
            raise RemoteError('SpaceX 需要已启用且身份明确的供应商或用户子账号', category='permission_denied')
        capabilities = hub.get('capabilities')
        can_create = role == 5 and isinstance(capabilities, dict) and capabilities.get('channel_write') is True
        result = {'identity_id': str(identity['id']), 'supplier_id': hub['supplier_id'], 'hub_role': hub['role'],
                  'can_write': can_create, 'can_toggle': role == 8 or can_create, 'can_edit_routing': False,
                  'create_block_reason': '' if can_create else SUPPLIER_CREATE_REASON if role == 8 else USER_CREATE_REASON}
        self._hub_identity = result
        self._hub_profile = hub
        return result

    def _visible_sites(self):
        sites = self.request('GET', '/api/admin-hub/usage-logs/sites')
        if (not isinstance(sites, list) or any(not isinstance(row, dict)
                or type(row.get('id')) is not int or row['id'] <= 0 for row in sites)
                or len({row['id'] for row in sites}) != len(sites)):
            self._invalid('SpaceX 可见站点列表不完整或不明确')
        visible = getattr(self, '_hub_profile', {}).get('visible_site_ids')
        if visible is not None and (not isinstance(visible, list)
                or any(type(value) is not int or value <= 0 for value in visible)
                or set(visible) != {row['id'] for row in sites}):
            raise RemoteError('SpaceX 账号可见站点与站点列表不一致', category='identity_mismatch')
        return sites

    def metadata(self):
        if not getattr(self, '_hub_identity', None):
            self.permissions()
        sites = self._visible_sites()
        data = self.request('GET', PREFIX + '/platform-types')
        rows = data.get('platform_types') if isinstance(data, dict) else None
        if not isinstance(rows, list):
            self._invalid('SpaceX 未返回明确的平台类型列表')
        platforms = {}
        for row in rows:
            if not isinstance(row, dict):
                self._invalid()
            channel_type = row.get('new_api_type')
            if type(channel_type) is int and channel_type in PLATFORMS:
                expected = PLATFORMS[channel_type]
                if (row.get('platform_type_key'), row.get('model_series')) != expected or channel_type in platforms:
                    self._invalid('SpaceX 平台类型映射发生变化')
                if row.get('key_fields') is not None and not isinstance(row['key_fields'], list):
                    self._invalid()
                platforms[channel_type] = row
        target = sites[0] if len(sites) == 1 and sites[0].get('api_protocol') == 'new_api' and sites[0].get('status') == 'online' else None
        groups, models, publish = [], [], None
        if target:
            group_data = self.request('GET', f"/api/admin-hub/sites/{target['id']}/groups")
            if (not isinstance(group_data, dict) or group_data.get('error')
                    or not isinstance(group_data.get('groups'), list)
                    or any(not isinstance(value, str) or not value for value in group_data['groups'])):
                self._invalid('SpaceX 未返回可核实的目标站点分组')
            groups = group_data['groups']
            publish = self.request('GET', f"/api/admin-hub/site-publish-fields/{target['id']}")
            if (not isinstance(publish, dict) or publish.get('site_id') != target['id']
                    or not isinstance(publish.get('fields'), list) or not isinstance(publish.get('site_defaults'), dict)):
                self._invalid('SpaceX 未返回可核实的发布字段与创建默认值')
            for channel_type, row in platforms.items():
                options = self.request('GET', PREFIX + '/model-options',
                                       params={'platform_type': row['platform_type_key'], 'site_id': target['id']})
                if (not isinstance(options, dict) or options.get('platform_type') != row['platform_type_key']
                        or options.get('new_api_type') != channel_type or options.get('model_series') != row['model_series']
                        or not isinstance(options.get('items'), list)):
                    self._invalid('SpaceX 模型目录与平台类型不一致')
                for model in options['items']:
                    if not isinstance(model, dict) or not isinstance(model.get('model_name'), str) or not model['model_name']:
                        self._invalid('SpaceX 模型目录无效')
                    models.append({'id': model['model_name']})
        result = {'models': models, 'groups': groups, 'platforms': platforms, 'target': target,
                  'publish': publish, 'sites': sites}
        self._hub_metadata = result
        return result

    @staticmethod
    def _meta_cap(meta):
        target, publish = meta['target'], meta['publish']
        defaults = publish['site_defaults'] if publish else {}
        return {'hub_target_site_id': target['id'] if target else None,
                'hub_create_status': defaults.get('create_status'), 'hub_site_defaults': defaults,
                'hub_platform_types': [PLATFORMS[value][0] for value in sorted(meta['platforms'])],
                'groups': list(meta['groups'])}

    def supported_types(self):
        return frozenset(getattr(self, '_hub_metadata', {}).get('platforms', PLATFORMS))

    def verify(self):
        permissions = self.permissions()
        self._page(page_size=1)
        meta = self.metadata()
        cap = {'read': 'supported', 'create': 'supported' if permissions['can_write'] else 'permission_denied',
               'create_block_reason': permissions['create_block_reason'], 'can_write': permissions['can_write'],
               'can_toggle': permissions['can_toggle'], 'can_edit_routing': False,
               'toggle': 'supported' if permissions['can_toggle'] else 'permission_denied',
               'test': 'supported' if permissions['can_toggle'] else 'permission_denied',
               'usage': 'supported', 'channel_config': 'supported', 'account_info': 'local_only',
               'custom_models': True, 'model_max_bytes': 255, 'remark_max_length': 255,
               'models': list(dict.fromkeys(row['id'] for row in meta['models'])),
               'channel_types': sorted(self.supported_types()),
               'formats': [spec['code'] for spec in get_format_specs() if spec['remote_type'] in self.supported_types()
                           and spec['schema_config']['type'] in ('api_key', 'aws_ak_sk', 'aws_api_key', 'aws_bedrock', 'vertex_json', 'vertex_gemini', 'azure_gpt')],
               'verified_build': dict(BUILD), 'verified_version': BUILD_ID,
               'compatibility_reviewed_at': BUILD_REVIEWED_AT, 'version_contract': 'spacex_hub',
               'usage_conversion': None, 'hub_role': permissions['hub_role'], **self._meta_cap(meta)}
        cap.update({action: 'unsupported' for action in ('edit', 'delete', 'rotate', 'multi_key', 'proxy',
                    'rpm', 'tpm', 'stats', 'vertex_claude_api_key', 'channel_status')})
        return {'identity': {'id': permissions['identity_id']}, 'capabilities': cap, 'verified_version': BUILD_ID}

    def check_connection(self):
        self.permissions()
        self._page(page_size=1)
        self._visible_sites()
        return {'version': BUILD_ID, 'endpoints': ['/api/status', '/api/user/self', PREFIX,
                                                  '/api/admin-hub/usage-logs/sites']}

    def _page(self, *, page=1, page_size=100, keyword=None):
        params = {'p': page, 'page_size': page_size}
        if keyword is not None:
            params['keyword'] = keyword
        data = self.request('GET', PREFIX, params=params)
        if (not isinstance(data, dict) or not isinstance(data.get('items'), list)
                or type(data.get('total')) is not int or data['total'] < 0
                or any(not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0 for row in data['items'])):
            self._invalid('SpaceX 渠道分页响应无效')
        return data

    def channels(self, keyword=None):
        self.permissions()
        records, seen, total = [], set(), None
        for page in range(1, 1001):
            data = self._page(page=page, keyword=keyword)
            if total is not None and data['total'] != total:
                self._invalid('SpaceX 渠道列表在分页期间发生变化，请重新同步')
            total = data['total']
            for row in data['items']:
                if row['id'] in seen:
                    self._invalid('SpaceX 渠道分页重复，请重新同步')
                seen.add(row['id'])
                raw = self._raw_detail(str(row['id']))
                self._owned(raw)
                try:
                    mapping = self._mapping(raw, self._publish_state(str(row['id'])))
                except RemoteError as exc:
                    if not exc.unknown:
                        raise
                    mapping = None
                records.append(self._project(raw, mapping))
            if len(records) == total:
                return records
            if not data['items'] or len(records) > total:
                self._invalid('SpaceX 渠道分页不完整，请重新同步')
        self._invalid('SpaceX 渠道数量超出分页上限')

    def _raw_detail(self, remote_id):
        data = self.request('GET', PREFIX + '/' + remote_id)
        if not isinstance(data, dict) or type(data.get('id')) is not int or str(data['id']) != remote_id:
            self._invalid('SpaceX 渠道详情编号不匹配')
        return data

    def _publish_state(self, remote_id):
        # This reviewed POST is a read-only query. Do not set the durable
        # write-attempt marker during sync, lookup, or archive checks.
        query = SiliconAdapter(self.site, transport=self.transport)
        result = query.request('POST', PREFIX + '/publish-status', body={'ids': [int(remote_id)]})
        value = result.get(remote_id) if isinstance(result, dict) else None
        if not isinstance(value, dict) or type(value.get('has_active_tasks')) is not bool:
            raise RemoteError('SpaceX 发布任务状态无法核实', unknown=True, category='protocol_error')
        return value

    def _owned(self, raw):
        identity = self._hub_identity
        if type(raw.get('supplier_id')) is not int or raw['supplier_id'] != identity['supplier_id']:
            raise RemoteError('SpaceX 渠道供应商归属不匹配，已停止操作', category='identity_mismatch')
        owner_field = 'supplier_id' if identity['hub_role'] == 'supplier' else 'owner_user_id'
        expected = identity['supplier_id'] if owner_field == 'supplier_id' else int(identity['identity_id'])
        if type(raw.get(owner_field)) is not int or raw[owner_field] != expected:
            raise RemoteError('SpaceX 渠道归属无法核实，已停止操作', category='identity_mismatch')

    def _mapping(self, raw, publication):
        selected, sites = raw.get('selected_site_ids'), raw.get('sites')
        if (not isinstance(selected, list) or len(selected) != 1 or type(selected[0]) is not int or selected[0] <= 0
                or not isinstance(sites, list) or len(sites) != 1 or not isinstance(sites[0], dict)
                or sites[0].get('site_id') != selected[0]
                or type(sites[0].get('remote_channel_id')) is not int or sites[0]['remote_channel_id'] <= 0):
            raise RemoteError('SpaceX 需要唯一、已绑定的下游渠道；多站点或未绑定渠道需先在远端处理', unknown=True)
        mapping = sites[0]
        tasks = publication.get('sites')
        if (publication.get('has_active_tasks') is not False or not isinstance(tasks, list) or len(tasks) != 1
                or not isinstance(tasks[0], dict) or tasks[0].get('site_id') != selected[0]
                or tasks[0].get('remote_channel_id') != mapping['remote_channel_id']
                or tasks[0].get('publish_status') not in PUBLISH_OK
                or tasks[0].get('task_status') not in TASK_OK
                or mapping.get('last_publish_status') not in PUBLISH_OK):
            raise RemoteError('SpaceX 下游发布尚未明确完成，保持待核实', unknown=True)
        return mapping

    def _project(self, raw, mapping=None):
        for key, kind in (('id', int), ('name', str), ('type', int), ('models', str), ('group', str)):
            if type(raw.get(key)) is not kind:
                self._invalid('SpaceX 渠道公开字段不完整')
        # Keep the template identity separate from its downstream identity;
        # never return raw channel_json, keys or remote error messages.
        result = {key: raw[key] for key in ('id', 'name', 'type', 'models', 'group')}
        result['status'] = 0
        if mapping and type(mapping.get('remote_status')) is int and mapping['remote_status'] in (1, 2, 3):
            result['status'] = mapping['remote_status']
        for field in ('priority', 'remark', 'test_model', 'platform_channel_type'):
            if isinstance(raw.get(field), (str, int)) and not isinstance(raw.get(field), bool):
                result[field] = raw[field]
        config = raw.get('channel_json')
        if isinstance(config, dict):
            for field in ('base_url', 'other'):
                if isinstance(config.get(field), str):
                    result[field] = config[field]
        result['_hub_publication_verified'] = mapping is not None
        result['_hub_status_source'] = 'cached_downstream_status'
        return result

    def detail(self, remote_id):
        remote_id = self._observation_id(remote_id)
        self.permissions()
        raw = self._raw_detail(remote_id)
        self._owned(raw)
        try:
            mapping = self._mapping(raw, self._publish_state(remote_id))
        except RemoteError as exc:
            if not exc.unknown:
                raise
            mapping = None
        return self._project(raw, mapping)

    def find_unique_name(self, name):
        rows = [row for row in self.channels(name) if row['name'] == name]
        if len(rows) > 1:
            raise RemoteError('SpaceX 同名渠道不唯一，请人工核实；不会重新创建', unknown=True)
        return self.detail(rows[0]['id']) if rows else None

    def _target(self, remote_id, expected=None):
        raw = self._raw_detail(remote_id)
        self._owned(raw)
        if (type(raw.get('type')) is not int or raw['type'] not in PLATFORMS
                or raw.get('platform_channel_type') != PLATFORMS[raw['type']][0]):
            raise RemoteError('SpaceX 此渠道的平台类型未核实', category='unsupported')
        if expected is not None and (not isinstance(expected, dict)
                or str(expected.get('id')) != remote_id or not expected.get('name')
                or raw.get('name') != expected['name'] or type(expected.get('type')) is not int
                or raw.get('type') != expected['type']):
            raise RemoteError('SpaceX 渠道身份与本地记录不一致', category='identity_mismatch')
        mapping = self._mapping(raw, self._publish_state(remote_id))
        visible = self._visible_sites()
        if (len(visible) != 1 or visible[0]['id'] != mapping['site_id']
                or visible[0].get('api_protocol') != 'new_api' or visible[0].get('status') != 'online'):
            raise RemoteError('SpaceX 目标站点身份或在线状态无法核实', category='identity_mismatch')
        if type(mapping.get('remote_status')) is not int or mapping['remote_status'] not in (1, 2, 3):
            raise RemoteError('SpaceX 下游渠道状态未知，已停止操作', unknown=True)
        return raw, mapping

    def _recheck_target(self, remote_id, expected=None):
        identity = self.permissions()
        if not identity['can_toggle']:
            raise RemoteError('SpaceX 账号没有明确的渠道操作权限', category='permission_denied')
        first, mapping = self._target(remote_id, expected)
        if self.permissions() != identity:
            raise RemoteError('SpaceX 账号身份或权限发生变化', category='identity_mismatch')
        second, current = self._target(remote_id, expected)
        if (first.get('name'), first.get('type'), first.get('models'), mapping) != (
                second.get('name'), second.get('type'), second.get('models'), current):
            raise RemoteError('SpaceX 下游映射在操作前发生变化', unknown=True, category='identity_mismatch')
        return second, current

    def toggle(self, remote_id, enabled):
        if type(enabled) is not bool:
            raise RemoteError('SpaceX 启停目标必须明确', category='configuration_error')
        remote_id = self._observation_id(remote_id)
        _, mapping = self._recheck_target(remote_id)
        result = self.request('POST', PREFIX + '/' + remote_id + '/status',
                              body={'status': 1 if enabled else 2, 'site_id': mapping['site_id']})
        if isinstance(result, dict) and (result.get('partial') or result.get('failed') or result.get('success') is False):
            raise RemoteError('SpaceX 启停未全部完成，结果需核实', unknown=True)

    def test_channel(self, remote_id, model, *, expected=None):
        self._validate_model_syntax([model])
        remote_id = self._observation_id(remote_id)
        raw, mapping = self._recheck_target(remote_id, expected)
        if model not in raw.get('models', '').split(','):
            raise RemoteError('所选模型不在 SpaceX 渠道范围内', category='configuration_error')
        payload = self._request_envelope('POST', PREFIX + '/' + remote_id + '/test',
            body={'site_id': mapping['site_id'], 'model': model}, side_effect=True, allow_business_failure=True)
        result = payload.get('data')
        if payload['success'] is False:
            return test_result({'success': False})
        if not isinstance(result, dict) or type(result.get('success')) is not bool:
            raise RemoteError('SpaceX 模型测试结果不明确，不会自动重复测试', unknown=True)
        return test_result({'success': result['success'], 'time': result.get('time')})

    def usage(self, remote_id, *, expected=None):
        remote_id = self._observation_id(remote_id)
        self.permissions()
        _, mapping = self._target(remote_id, expected)
        result = self.request('GET', PREFIX + '/' + remote_id + '/realtime-used-quota',
                              params={'site_id': mapping['site_id']})
        _, after = self._target(remote_id, expected)
        if (after['site_id'], after['remote_channel_id']) != (mapping['site_id'], mapping['remote_channel_id']):
            raise RemoteError('SpaceX 用量读取期间下游映射变化', unknown=True, category='identity_mismatch')
        if not isinstance(result, dict) or type(result.get('used_quota')) is not int or result['used_quota'] < 0:
            self._invalid('SpaceX 未返回有效的原始消耗额度')
        return extract_usage({'used_quota': result['used_quota']}, conversion=None)

    def usage_conversion(self, *, refresh=False):
        return None

    def channel_status(self, remote_id, *, expected):
        remote_id = self._observation_id(remote_id)
        self.permissions()
        self._target(remote_id, expected)
        # Hub GET returns cached downstream status; a POST refresh changes
        # remote state. Neither a cached value nor a test authorizes archival.

    def validate_models(self, models, group='default'):
        self._validate_model_syntax(models)
        meta = self.metadata()
        if not isinstance(group, str) or any(not value or value not in meta['groups'] for value in group.split(',')):
            raise RemoteError('SpaceX 目标站点不允许所选分组', category='configuration_error')

    def _create_body(self, *, name, key, models, remark, group, channel_type, config, proxy, meta):
        if not isinstance(name, str) or not name or name != name.strip() or len(name) > 160:
            raise RemoteError('SpaceX 渠道名称无效', category='configuration_error')
        self._validate_model_syntax(models)
        if channel_type == 41:
            from ..google_services import service_model_issues
            if issues := service_model_issues({'type': 'vertex_gemini', 'remote_type': 41}, models):
                raise RemoteError('；'.join(issues), category='configuration_error')
        cfg, spec = checked_config(config, channel_type, frozenset(meta['platforms']))
        issues = template_config_issues(self._meta_cap(meta), spec['schema_config'], cfg,
                                       routing_group=group, proxy=bool(proxy))
        if issues:
            raise RemoteError('；'.join(issues), category='configuration_error')
        publish, target = meta['publish'], meta['target']
        defaults = publish['site_defaults']
        if (type(defaults.get('create_status')) is not int or defaults['create_status'] not in (1, 2)
                or defaults.get('encrypt_channel_name') is not False
                or defaults.get('site_rewrites_routing') is not False):
            raise RemoteError('SpaceX 站点会改写名称或路由，当前无法核实创建结果', category='unsupported')
        if any(not isinstance(field, dict) or field.get('required') is True for field in publish['fields']):
            raise RemoteError('SpaceX 目标站点存在尚未支持的必填发布字段', category='unsupported')
        if not isinstance(key, str) or not key.strip():
            raise RemoteError('SpaceX 需要单个有效 Key', category='configuration_error')
        channel = create_payload(name=name, key=key, models=models, remark=remark, group=group,
            channel_type=channel_type, config=config, supported_types=self.CHANNEL_TYPES, object_settings=True)
        # JSON credentials can be multiline input; normalization produces one
        # credential. Plain API keys must never smuggle a second key.
        if '\n' in channel['key'] or '\r' in channel['key']:
            raise RemoteError('SpaceX 一条渠道只能包含一个 Key', category='configuration_error')
        platform = meta['platforms'][channel_type]
        key_fields = platform.get('key_fields') or []
        declared_fields, seen_fields = set(), set()
        for field in key_fields:
            if not isinstance(field, dict) or not isinstance(field.get('name'), str):
                self._invalid('SpaceX 平台密钥字段定义无效')
            if field['name'] in seen_fields:
                self._invalid('SpaceX 平台密钥字段定义重复')
            seen_fields.add(field['name'])
            if field.get('type') != 'note':
                declared_fields.add(field['name'])
        # Client hashes do not pin this server-provided field catalog. Missing
        # fields must not silently discard the chosen AWS authentication mode
        # or provider parameters, including parameters derived from credentials.
        required_fields = {'settings.aws_key_type'} if channel_type == 33 else set()
        if channel_type in (3, 41) or channel.get('other'):
            required_fields.add('other')
        if not required_fields <= declared_fields:
            raise RemoteError('SpaceX 平台密钥字段已变化，无法完整提交认证方式或额外参数，请重新验证站点',
                              category='protocol_error', reason='platform_fields_changed')
        body = {'name': name, 'platform_channel_type': platform['platform_type_key'],
                'type': channel_type, 'model_series': platform['model_series'], 'key': channel['key'],
                'models': list(models), 'group': group, 'siteIds': [target['id']], 'remark': remark,
                'test_model': models[0], 'site_publish_settings': {}, 'priority': cfg['priority']}
        if channel.get('base_url'):
            body['base_url'] = channel['base_url']
        known_fields = {'base_url', 'other', 'settings.aws_key_type', 'settings.allow_claude_fallbacks'}
        for field in key_fields:
            field_name = field['name']
            if field.get('type') == 'note':
                continue
            if field_name not in known_fields:
                raise RemoteError('SpaceX 平台出现尚未支持的密钥配置字段', category='unsupported')
            value = channel.get(field_name)
            if field_name.startswith('settings.'):
                value = channel.get('settings', {}).get(field_name.split('.', 1)[1])
            if value in (None, ''):
                value = field.get('default')
            if value in (None, ''):
                if field.get('required') is True:
                    raise RemoteError('SpaceX 平台必填密钥配置缺失', category='configuration_error')
                continue
            if field.get('type') == 'bool':
                if value in ('true', 'false'):
                    value = value == 'true'
                if type(value) is not bool:
                    raise RemoteError('SpaceX 平台布尔配置无效', category='configuration_error')
            body[field_name] = value
        return body

    def create(self, *, name, key, models, remark='', group='default', channel_type=1, config=None, proxy='', keys=None):
        if keys is not None:
            self._unsupported('SpaceX 一条渠道只能上传一个 Key，不支持多密钥容器')
        identity = self.permissions()
        if not identity['can_write']:
            raise RemoteError(identity['create_block_reason'], category='permission_denied')
        args = {'name': name, 'key': key, 'models': models, 'remark': remark, 'group': group,
                'channel_type': channel_type, 'config': config, 'proxy': proxy}
        body = self._create_body(**args, meta=self.metadata())
        if self.find_unique_name(name) is not None:
            raise RemoteError('SpaceX 已有同名渠道，需核实原创建结果；不会重复上传', unknown=True)
        if self.permissions() != identity:
            raise RemoteError('SpaceX 上传前账号身份或权限发生变化', category='identity_mismatch')
        if self._create_body(**args, meta=self.metadata()) != body:
            raise RemoteError('SpaceX 上传前站点配置发生变化，请重新准备任务', category='configuration_error')
        result = self.request('POST', PREFIX, body=body)
        if isinstance(result, dict) and (result.get('publish_error') or result.get('partial')
                or isinstance(result.get('site_selection'), dict) and result['site_selection'].get('failed')):
            raise RemoteError('SpaceX 渠道已提交，但后台发布未明确成功；不会重复上传', unknown=True)
        # Only a later unique-name detail with a completed, single binding can
        # confirm this create. Do not replay publish or force-retry automatically.

    def validate_created_config(self, remote, config, proxy, channel_type):
        if remote.get('_hub_publication_verified') is not True:
            raise RemoteError('SpaceX 下游发布尚未明确完成', unknown=True)
        cfg, _ = checked_config(config, channel_type, self.CHANNEL_TYPES)
        expected = {'priority': cfg['priority']}
        if cfg['base_url']:
            expected['base_url'] = cfg['base_url']
        if cfg['other']:
            expected['other'] = cfg['other']
        if proxy or any(remote.get(field) != value for field, value in expected.items()):
            raise RemoteError('SpaceX 创建后的配置与模板不一致或不可见', unknown=True)

    def edit(self, remote_id, *, changes, key=None, expected=None, multikey=False):
        self._unsupported('SpaceX 暂不支持在本地编辑或更换 Key；远端渠道 Key 创建后不可修改')

    def delete(self, remote_id, *, expected=None):
        self._unsupported('SpaceX 未提供已核实的删除接口；请先在远端管理渠道')
