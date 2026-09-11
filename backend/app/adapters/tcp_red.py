"""Colin supplier API observed on api.tcp.red, v1.0.0-rc.32-colin.

The public channel form and authenticated read endpoints define this contract.
It uses /api/channel/, explicit RBAC, channel-scoped groups, and string settings.
No credential-reveal endpoint is used. Model tests are explicit unsafe actions.
"""
import json

from ..newapi_formats import get_format_specs
from .silicon import RemoteError, SiliconAdapter, validate_delete_identity


class TcpRedAdapter(SiliconAdapter):
    VERSION = 'v1.0.0-rc.32-colin'
    CHANNEL_TYPES = frozenset(spec['remote_type'] for spec in get_format_specs())
    SUPPORTS_PROXY = True
    SUPPORTS_RPM = True
    OBJECT_SETTINGS = False
    SELLER_API = False
    CONNECTION_GROUP_SCOPE = 'channel'
    CREATE_PATH = '/api/channel/'
    MASKED_CONFIG_FIELDS = frozenset({'base_url'})

    def request(self, method, path, *, body=None, params=None):
        try:
            if method not in ('GET', 'HEAD'):
                status = super().request('GET', '/api/status')
                if not isinstance(status, dict) or status.get('version') != self.VERSION:
                    raise RemoteError('平台版本与已验证的 Colin 接入契约不一致', category='protocol_error')
            return super().request(method, path, body=body, params=params)
        except RemoteError as exc:
            # Only fixed API paths and numeric IDs; never include headers/bodies.
            exc.endpoint, exc.method = path, method
            raise

    def permissions(self):
        identity = self.request('GET', '/api/user/self')
        if not isinstance(identity, dict) or str(identity.get('id')) != str(self.site.seller_user_id):
            raise RemoteError('供应商用户编号与令牌身份不匹配', category='identity_mismatch')
        if identity.get('status') != 1:
            raise RemoteError('供应商账号未启用', category='permission_denied')
        permissions = identity.get('permissions')
        admin = permissions.get('admin_permissions') if isinstance(permissions, dict) else None
        channel = admin.get('channel') if isinstance(admin, dict) else None
        if not isinstance(channel, dict):
            raise RemoteError('平台未返回明确的渠道权限', category='permission_denied')
        # Never infer permission merely from a role number or a truthy string.
        flags = {name: channel.get(name) is True for name in
                 ('read', 'write', 'operate', 'sensitive_write', 'sensitive_write_all', 'secret_view')}
        if not flags['read']:
            raise RemoteError('供应商账号没有渠道读取权限', category='permission_denied')
        return {**flags, 'identity_id': str(identity['id']),
                'can_write': flags['write'] and flags['sensitive_write'],
                'can_toggle': flags['operate'], 'can_edit_routing': flags['write']}

    def metadata(self):
        models = self.request('GET', '/api/channel/models')
        groups = self.request('GET', '/api/group/', params={'scope': 'channel'})
        if (not isinstance(models, list)
                or any(not isinstance(m, dict) or not isinstance(m.get('id'), str) or not m['id'] for m in models)
                or not isinstance(groups, list)
                or any(not isinstance(g, str) or not g for g in groups)):
            raise RemoteError('平台模型或渠道分组响应不符合接入契约', category='protocol_error')
        return {'models': models, 'groups': groups}

    def validate_models(self, models, group='default'):
        # The reviewed Colin channel form permits custom model names; its
        # metadata catalog can lag behind upstream provider releases.
        self._validate_model_syntax(models)
        meta = self.metadata()
        if not isinstance(group, str):
            raise RemoteError('远端渠道分组格式无效', category='protocol_error')
        requested = [part.strip() for part in group.split(',')]
        if any(not part or part not in meta['groups'] for part in requested):
            raise RemoteError('远端不允许指定渠道分组')

    def _page(self, *, page=1, page_size=100, keyword=None):
        params = {'p': page, 'page_size': page_size, 'id_sort': 'true'}
        path = '/api/channel/'
        if keyword is not None:
            path = '/api/channel/search'
            params['keyword'] = keyword
        data = self.request('GET', path, params=params)
        if (not isinstance(data, dict) or not isinstance(data.get('items'), list)
                or type(data.get('total')) is not int or data['total'] < 0
                or any(not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0
                       for row in data['items'])):
            raise RemoteError('平台渠道列表响应不符合接入契约', category='protocol_error')
        return {**data, 'items': [self._annotate_remote(row) for row in data['items']]}

    def verify(self):
        permissions = self.permissions()
        status = self.request('GET', '/api/status')
        if not isinstance(status, dict) or status.get('version') != self.VERSION:
            raise RemoteError('平台版本与已验证的 Colin 接入契约不一致', category='protocol_error')
        self._page(page_size=1)
        meta = self.metadata()
        cap = {'read': 'supported', 'formats': [s['code'] for s in get_format_specs()],
               'channel_types': sorted(self.CHANNEL_TYPES), 'channel_config': 'supported',
               'account_info': 'local_only', 'custom_models': True, 'model_max_bytes': 255,
               'models': list(dict.fromkeys(m['id'] for m in meta['models'])),
               'groups': list(dict.fromkeys(meta['groups'])),
               'can_write': permissions['can_write'], 'can_toggle': permissions['can_toggle'],
               'can_edit_routing': permissions['can_edit_routing'],
               'remote_permissions': {name: permissions[name] for name in
                                      ('read', 'write', 'operate', 'sensitive_write', 'sensitive_write_all', 'secret_view')},
               'remark_max_length': 255}
        for action in ('create', 'edit', 'delete'):
            cap[action] = 'supported' if permissions['can_write'] else 'permission_denied'
        cap['toggle'] = 'supported' if permissions['can_toggle'] else 'permission_denied'
        cap.update({name: 'unsupported' for name in ('stats', 'tpm')})
        cap['test'] = 'supported' if permissions['can_toggle'] else 'permission_denied'
        cap['usage'] = 'supported'
        cap['multi_key'] = 'supported'
        from .vertex_capabilities import vertex_claude_api_key_capability
        cap['vertex_claude_api_key'] = vertex_claude_api_key_capability('tcp-red-v1', self.VERSION)
        cap.update(proxy='supported', rpm='supported', rpm_runtime='not_measured')
        cap['tags'] = 'unverified'
        cap['usage_conversion'] = self.usage_conversion()
        return {'identity': {'id': permissions['identity_id']}, 'capabilities': cap,
                'verified_version': status['version']}

    def channels(self, keyword=None):
        self.permissions()
        records, seen = [], set()
        for page in range(1, 1001):
            data = self._page(page=page, keyword=keyword)
            rows = data['items']
            for row in rows:
                if row['id'] in seen:
                    raise RemoteError('平台分页结果重复，请重新同步', category='protocol_error')
                seen.add(row['id'])
                records.append(row)
            if len(records) >= data['total']:
                return records
            if not rows:
                raise RemoteError('平台分页提前结束，请重新同步', category='protocol_error')
        raise RemoteError('平台渠道数量超过分页上限，请联系管理员')

    @staticmethod
    def _remote_id(remote_id):
        value = str(remote_id)
        if not value.isascii() or not value.isdigit() or int(value) <= 0:
            raise RemoteError('远端渠道编号无效', category='configuration_error')
        return value

    def detail(self, remote_id):
        remote_id = self._remote_id(remote_id)
        data = self.request('GET', '/api/channel/' + remote_id)
        if not isinstance(data, dict) or type(data.get('id')) is not int or str(data['id']) != remote_id:
            raise RemoteError('平台渠道详情响应不符合接入契约', category='protocol_error')
        return self._annotate_remote(data)

    def _annotate_remote(self, remote):
        # This flag describes visibility only, never a successful write or a
        # verified value. Discard arbitrary flags supplied by the remote site.
        data = {k: v for k, v in remote.items() if k != '_unverified_config_fields'}
        hidden = sorted(field for field in self.MASKED_CONFIG_FIELDS if data.get(field) == '***')
        if hidden:
            data['_unverified_config_fields'] = hidden
        return data

    def _validate_visible_created_config(self, remote, config, proxy, channel_type):
        hidden = sorted(field for field in self.MASKED_CONFIG_FIELDS if remote.get(field) == '***')
        visible = remote
        if hidden:
            from .channel_config import config_fields

            routing = (getattr(self.site, 'capabilities', None) or {}).get('can_edit_routing') is True
            expected, _ = config_fields(config, channel_type, proxy=proxy,
                                         **self._config_options(routing=routing))
            # Compare all visible fields. The real remote value stays masked;
            # the caller must explicitly record that it could not be verified.
            visible = {**remote, **{field: expected[field] for field in hidden}}
        super().validate_created_config(visible, config, proxy, channel_type)
        return hidden

    def validate_created_config(self, remote, config, proxy, channel_type):
        if self._validate_visible_created_config(remote, config, proxy, channel_type):
            raise RemoteError('接口地址由站点隐藏，无法核对远端配置；需人工核实', unknown=True,
                              category='configuration_unverified', reason='masked_base_url')

    def validate_acknowledged_create_config(self, remote, config, proxy, channel_type):
        # Only the worker's durable successful-create stage may choose this
        # method. A stable-name match or timed-out write is not an acknowledgement.
        hidden = self._validate_visible_created_config(remote, config, proxy, channel_type)
        remote.pop('_unverified_config_fields', None)
        if hidden:
            remote['_unverified_config_fields'] = hidden

    def _assert_owned(self, remote, permissions):
        # Match the supplier UI ownership check. Local RBAC is checked separately
        # by the worker before dispatch and immediately before the HTTP write.
        if permissions['sensitive_write_all']:
            return
        if type(remote.get('created_by')) is not int or str(remote['created_by']) != permissions['identity_id']:
            raise RemoteError('供应商账号没有此远端渠道的管理权限', category='permission_denied')

    def _observation_owned(self, remote, permissions):
        self._assert_owned(remote, permissions)

    def _observation_version(self):
        status = self.request('GET', '/api/status')
        if not isinstance(status, dict) or status.get('version') != self.VERSION:
            raise RemoteError('平台版本与已验证的 Colin 接入契约不一致', category='protocol_error')

    def find_unique_name(self, name):
        rows = [row for row in self.channels(name) if row.get('name') == name]
        if len(rows) > 1:
            raise RemoteError('稳定名称对应多个远端渠道，需人工核实', unknown=True)
        if not rows:
            return None
        self._assert_owned(rows[0], self.permissions())
        return rows[0]

    @staticmethod
    def _remark(remark):
        if not isinstance(remark, str) or len(remark) > 255:
            raise RemoteError('Colin 平台的渠道备注不能超过 255 个字符', category='configuration_error')
        return remark

    def create(self, *, name, key, models, remark='', group='default', channel_type=1, config=None, proxy='', keys=None):
        from .channel_config import create_payload

        permissions = self.permissions()
        if not permissions['can_write']:
            raise RemoteError('供应商账号没有渠道创建权限', category='permission_denied')
        self.validate_models(models, group)
        channel = create_payload(name=name, key=key, models=models, remark=self._remark(remark), group=group,
                                 channel_type=channel_type, config=config, proxy=proxy, keys=keys,
                                 **self._config_options(routing=permissions['can_edit_routing']))
        if channel['status'] == 1 and permissions.get('can_toggle') is not True:
            raise RemoteError('创建启用渠道还需要远端启停权限', category='permission_denied')
        body = {'mode': 'multi_to_single' if keys is not None else 'single', 'channel': channel}
        if keys is not None:
            body['multi_key_mode'] = 'random'
        data = self._create_request(body)
        if isinstance(data, dict) and type(data.get('id')) is int and data['id'] > 0:
            return str(data['id'])
        return None  # The durable worker resolves the same stable name, never re-POSTs.

    def _create_request(self, body):
        return self.request('POST', self.CREATE_PATH, body=body)

    @staticmethod
    def _json_setting(value):
        if value in (None, ''):
            return '{}'
        if not isinstance(value, str):
            raise RemoteError('Colin 平台设置字段必须是 JSON 字符串', category='protocol_error')
        try:
            decoded = json.loads(value)
        except ValueError:
            raise RemoteError('Colin 平台设置字段不是有效 JSON 对象', category='protocol_error') from None
        if not isinstance(decoded, dict):
            raise RemoteError('Colin 平台设置字段不是有效 JSON 对象', category='protocol_error')
        return value

    def edit(self, remote_id, *, changes, key=None, expected=None, multikey=False):
        permissions = self.permissions()
        if not permissions['can_write']:
            raise RemoteError('供应商账号没有渠道编辑权限', category='permission_denied')
        current = self.detail(remote_id)
        self._assert_owned(current, permissions)
        if any(current.get(field) == '***' for field in ('base_url', 'openai_organization', 'other',
                                                        'setting', 'settings', 'param_override', 'header_override')):
            raise RemoteError('站点隐藏了连接配置，无法安全保留原值；已停止编辑或更换密钥',
                              category='configuration_unverified')
        if expected and any(current.get(k) != v for k, v in expected.items()):
            raise RemoteError('发现远端人工修改，已停止覆盖；请同步后重新提交')
        if set(changes) - {'name', 'models', 'remark'}:
            raise RemoteError('当前本地工作流不允许修改这些远端字段', category='permission_denied')
        if type(current.get('type')) is not int or current['type'] not in self.supported_types():
            raise RemoteError('当前远端渠道类型尚未实现', category='configuration_error')
        if 'remark' in changes:
            self._remark(changes['remark'])
        if 'models' in changes:
            if not isinstance(changes['models'], str):
                raise RemoteError('模型配置必须为逗号分隔字符串', category='configuration_error')
            self.validate_models(changes['models'].split(','), current.get('group', 'default'))
        allowed = {'name', 'type', 'base_url', 'openai_organization', 'models', 'group',
                   'model_mapping', 'test_model', 'auto_ban', 'status_code_mapping', 'tag',
                   'remark', 'contact', 'setting', 'param_override', 'header_override', 'settings', 'other',
                   'priority', 'weight'}
        body = {k: current[k] for k in allowed if k in current}
        body['id'] = int(self._remote_id(remote_id))
        body['setting'] = self._json_setting(current.get('setting'))
        body['settings'] = self._json_setting(current.get('settings'))
        body.update(changes)
        if key is not None:
            body['key'] = key
            if multikey:
                body['key_mode'] = 'replace'
        # Preserve observed priority/weight while allowing no local change to
        # them. Existing masked key, creator and status are never sent back.
        self.request('PUT', '/api/channel/', body=body)

    def toggle(self, remote_id, enabled):
        permissions = self.permissions()
        if not permissions['can_toggle']:
            raise RemoteError('供应商账号没有渠道启停权限', category='permission_denied')
        remote_id = self._remote_id(remote_id)
        self._assert_owned(self.detail(remote_id), permissions)
        self.request('POST', '/api/channel/' + remote_id + '/status', body={'status': 1 if enabled else 2})

    def delete(self, remote_id, *, expected=None):
        permissions = self.permissions()
        if not permissions['can_write']:
            raise RemoteError('供应商账号没有渠道删除权限', category='permission_denied')
        remote_id = self._remote_id(remote_id)
        current = self.detail(remote_id)
        self._assert_owned(current, permissions)
        if expected is not None:
            validate_delete_identity(current, expected)
        self.request('DELETE', '/api/channel/' + remote_id)
