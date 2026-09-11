"""Silicon seller interface contract, separate from NewAPI administrator APIs."""
import json
import ssl
from collections.abc import Callable
from decimal import Decimal

import httpx

from ..network import safe_request
from ..routing_groups import routing_group_names
from ..security import decrypt
from .channel_observation import extract_usage, test_result


class RemoteError(Exception):
    def __init__(self, message: str, *, unknown=False, retryable=False, category='remote_error', status_code=None,
                 reason=None, observed_version=None, endpoint=None, method=None):
        super().__init__(message)
        self.unknown = unknown
        self.retryable = retryable
        self.category = category
        self.status_code = status_code
        self.reason = reason
        self.observed_version = observed_version
        self.endpoint = endpoint
        self.method = method


def _tls_certificate_reason(error):
    """Classify verified TLS failures by type/code, never exception text."""
    seen = set()
    while error is not None and id(error) not in seen and len(seen) < 16:
        seen.add(id(error))
        if isinstance(error, ssl.SSLCertVerificationError):
            code = getattr(error, 'verify_code', None)
            if type(code) is int:
                if code == 10:
                    return 'tls_certificate_expired'
                if code == 9:
                    return 'tls_certificate_not_yet_valid'
            return 'tls_certificate_invalid'
        error = error.__cause__
    return None


def validate_delete_identity(remote, expected):
    if (not isinstance(expected, dict) or not expected.get('id') or not expected.get('name')
            or type(expected.get('type')) is not int
            or not isinstance(remote, dict) or str(remote.get('id')) != expected['id']
            or remote.get('name') != expected['name'] or type(remote.get('type')) is not int
            or remote['type'] != expected['type']):
        raise RemoteError('远端渠道身份与删除确认不一致，已停止删除；请同步后重新确认',
                          unknown=True, category='identity_mismatch')


class SiliconAdapter:
    VERSION = 'v1.0.0-rc.25-fix-36'
    # Historical versions identify legacy payloads and old stored capabilities.
    # Current seller compatibility is checked through the interfaces, not this list.
    COMPACT_VERSIONS = frozenset({VERSION, 'v1.0.0-rc.25-fix-38'})
    LEGACY_VERSION = 'v1.0.0-rc.25-fix-22-multiseller-2'
    VERSIONS = COMPACT_VERSIONS | {LEGACY_VERSION}
    COMPACT_CONTRACT = 'silicon-seller-compact-v1'
    LEGACY_CONTRACT = 'silicon-seller-legacy-v1'
    PERMISSION_FIELDS = ('can_write', 'can_toggle', 'can_edit_routing')
    # The documented type=1 is an example, not a seller-only type restriction.
    # Restrict capabilities to the business catalog reviewed for this contract.
    CHANNEL_TYPES = frozenset({1, 3, 14, 20, 24, 33, 41})
    SUPPORTS_PROXY = False
    SUPPORTS_RPM = False
    OBJECT_SETTINGS = True
    SELLER_API = True
    CONNECTION_GROUP_SCOPE = None
    SELLER_FIELDS = frozenset({'id', 'name', 'type', 'key', 'base_url', 'openai_organization',
                              'other', 'models', 'model_mapping', 'test_model', 'group', 'remark',
                              'status', 'priority', 'weight', 'proxy', 'settings'})
    SELLER_SETTINGS = frozenset({'azure_responses_version', 'vertex_key_type', 'aws_key_type',
                                'openrouter_enterprise'})

    def __init__(self, site, transport: Callable | None = None, before_write: Callable | None = None):
        self.site = site
        self.transport = transport or safe_request
        self.before_write = before_write

    def request(self, method, path, *, body=None, params=None):
        try:
            return self._request_envelope(method, path, body=body, params=params).get('data')
        except RemoteError as exc:
            exc.endpoint, exc.method = path, method
            raise

    def _request_envelope(self, method, path, *, body=None, params=None, side_effect=False,
                          allow_business_failure=False):
        # New API's GET channel/test endpoint invokes upstream inference and
        # updates test state. Treat that GET exactly like an unsafe write.
        write = side_effect or method not in ('GET', 'HEAD')
        if write and self.before_write:
            self.before_write()
        try:
            response = self.transport(method, self.site.base_url.rstrip('/') + path,
                                      headers={'Authorization': 'Bearer ' + decrypt(self.site.token_encrypted),
                                               'New-Api-User': str(self.site.seller_user_id),
                                               'Content-Type': 'application/json'},
                                      json=body, params=params, timeout=60 if side_effect else 20)
        except (httpx.TransportError, OSError) as exc:
            raise RemoteError('远端网络异常；写入结果需要核实' if write else '远端读取网络异常',
                              unknown=write, retryable=not write, category='connection_error',
                              reason=_tls_certificate_reason(exc)) from None
        except ValueError:
            raise RemoteError('远端地址或响应不符合安全要求', category='configuration_error') from None
        if not 200 <= response.status_code < 300:
            # Never include remote errors: they can echo tokens, keys or proxy passwords.
            category = {401: 'authentication_error', 403: 'permission_denied', 429: 'rate_limited'}.get(
                response.status_code, 'server_error' if response.status_code >= 500 else 'protocol_error')
            raise RemoteError(f'远端 HTTP {response.status_code}', unknown=write and response.status_code >= 500,
                              retryable=not write and (response.status_code == 429 or response.status_code >= 500),
                              category=category, status_code=response.status_code)
        try:
            payload = response.json(parse_float=Decimal) if path == '/api/status' else response.json()
        except (ValueError, TypeError):
            raise RemoteError('远端返回了无效 JSON', unknown=write, category='protocol_error') from None
        if isinstance(payload, dict) and payload.get('success') is False and not allow_business_failure:
            raise self._business_error(payload, method=method, path=path)
        if (not isinstance(payload, dict) or type(payload.get('success')) is not bool
                or (payload['success'] is False and not allow_business_failure)):
            raise RemoteError('远端响应缺少明确的成功标记', unknown=write, category='protocol_error')
        if method == 'GET' and path == '/api/status' and payload.get('success') is True:
            self._usage_status = payload.get('data')
        return payload

    def _business_error(self, payload, *, method, path):
        # Subclasses may recognize an exact reviewed error signature, but never
        # propagate arbitrary upstream messages or SQL into persisted errors.
        return RemoteError('远端业务请求未成功', category='business_error')

    def check_connection(self):
        """Read configured endpoints without authorizing any distribution capability."""
        from ..site_verification import safe_diagnostic

        endpoints = []

        def read(path, expected_type, *, params=None):
            data = self.request('GET', path, params=params)
            if not isinstance(data, expected_type):
                invalid(path)
            endpoints.append(path)
            return data

        def invalid(path):
            raise RemoteError(f'连接检查失败：GET {path} 未返回有效的数据结构',
                              category='protocol_error', endpoint=path, method='GET')

        identity = read('/api/user/self', dict)
        if (type(identity.get('id')) is not int or identity['id'] <= 0
                or str(identity['id']) != str(self.site.seller_user_id)):
            raise RemoteError('连接检查失败：令牌身份与配置的用户编号不匹配', category='identity_mismatch',
                              endpoint='/api/user/self', method='GET')
        status = read('/api/status', dict)
        path = '/api/seller/channel/' if self.SELLER_API else '/api/channel/'
        listing = read(path, dict, params={'p': 1, 'page_size': 1})
        if (not isinstance(listing.get('items'), list)
                or type(listing.get('total')) is not int or listing['total'] < 0):
            invalid(path)
        if self.SELLER_API:
            path = '/api/seller/channel/meta'
            metadata = read(path, dict)
            if not isinstance(metadata.get('models'), list) or not isinstance(metadata.get('groups'), list):
                invalid(path)
        else:
            read('/api/channel/models', list)
            params = {'scope': self.CONNECTION_GROUP_SCOPE} if self.CONNECTION_GROUP_SCOPE else None
            read('/api/group/', list, params=params)
        version = safe_diagnostic('remote_error', observed_version=status.get('version')).get('observed_version')
        return {'version': version, 'endpoints': endpoints}

    def _seller_version(self):
        from ..site_verification import safe_diagnostic

        status = self.request('GET', '/api/status')
        if not isinstance(status, dict):
            self._contract_error('/api/status', 'data')
        # Missing/hidden version tags do not determine interface compatibility.
        return safe_diagnostic('remote_error', observed_version=status.get('version')).get('observed_version', '')

    @staticmethod
    def _contract_error(endpoint, field):
        # Only callers' fixed field names/paths appear here, never remote values.
        raise RemoteError(f'Silicon 接口不兼容：GET {endpoint} 的 {field} 字段缺失或类型不符',
                          category='protocol_error', reason='incompatible_interface',
                          endpoint=endpoint, method='GET')

    def _check_listing(self, data):
        path = '/api/seller/channel/'
        if not isinstance(data, dict):
            self._contract_error(path, 'data')
        if not isinstance(data.get('items'), list):
            self._contract_error(path, 'items')
        if type(data.get('total')) is not int or data['total'] < 0:
            self._contract_error(path, 'total')
        if any(not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0
               for row in data['items']):
            self._contract_error(path, 'items.id')
        for field, kind in (('name', str), ('type', int), ('status', int), ('models', str), ('group', str)):
            if any(type(row.get(field)) is not kind for row in data['items']):
                self._contract_error(path, 'items.' + field)
        return data

    def _recheck_seller_contract(self):
        profile = getattr(self, '_seller_contract_profile', None)
        permissions = getattr(self, '_seller_permissions', None)
        self.permissions()
        if profile != self._seller_contract_profile:
            raise RemoteError('Silicon 卖家接口协议发生变化，请重新验证站点后重试', category='protocol_error',
                              reason='interface_changed')
        if permissions != self._seller_permissions:
            raise RemoteError('Silicon 卖家接口权限发生变化，请刷新后重试', category='permission_denied',
                              reason='permissions_changed')

    def _seller_write(self, method, path, *, body=None):
        # Recheck the actual interfaces before the durable write-attempt flag.
        self._recheck_seller_contract()
        channel = body.get('channel', body) if isinstance(body, dict) else None
        if isinstance(channel, dict) and 'models' in channel:
            self._validate_models_with_meta(channel['models'].split(','), channel.get('group', 'default'),
                                            self._seller_metadata)
        return self.request(method, path, body=body)

    def _compact_seller_dto(self):
        if not self.SELLER_API:
            return False
        profile = getattr(self, '_seller_contract_profile', None)
        if profile is not None:
            return profile == self.COMPACT_CONTRACT
        cap = getattr(self.site, 'capabilities', None) or {}
        if cap.get('protocol_contract') == self.COMPACT_CONTRACT:
            return True
        version = getattr(self, '_current_seller_version', None)
        if version is None:
            version = cap.get('verified_version')
        return version in SiliconAdapter.COMPACT_VERSIONS

    def _seller_config(self, config):
        if config is not None and not isinstance(config, dict):
            raise RemoteError('渠道模板配置必须是对象', category='configuration_error')
        if self._compact_seller_dto():
            config = config or {}
            if config.get('auto_ban', 1) != 1 or config.get('status_code_mapping', {}):
                raise RemoteError('此卖家版本不支持自动禁用或状态码映射设置', category='configuration_error')

    def _seller_payload(self, channel, proxy=''):
        if not self._compact_seller_dto():
            return channel
        result = {k: v for k, v in channel.items() if k in self.SELLER_FIELDS}
        result['settings'] = {k: v for k, v in channel['settings'].items() if k in self.SELLER_SETTINGS}
        result['proxy'] = proxy
        return result

    def permissions(self):
        identity = self.request('GET', '/api/user/self')
        if not isinstance(identity, dict) or str(identity.get('id')) != str(self.site.seller_user_id):
            raise RemoteError('卖家用户编号与令牌身份不匹配', category='identity_mismatch')
        version = self._seller_version()
        data = self.request('GET', '/api/seller/channel/', params={'p': 1, 'page_size': 1})
        self._check_listing(data)
        for field in self.PERMISSION_FIELDS:
            if type(data.get(field)) is not bool:
                self._contract_error('/api/seller/channel/', field)
        meta = self.metadata()
        self._current_seller_version = version
        self._seller_contract_profile = self.LEGACY_CONTRACT if version == self.LEGACY_VERSION else self.COMPACT_CONTRACT
        self._seller_permissions = {field: data[field] for field in self.PERMISSION_FIELDS}
        self._seller_metadata = meta
        self._seller_identity = identity['id']
        return data

    def metadata(self):
        data = self.request('GET', '/api/seller/channel/meta')
        if not isinstance(data, dict):
            self._contract_error('/api/seller/channel/meta', 'data')
        if (not isinstance(data.get('models'), list)
                or any(not isinstance(m, dict) or not isinstance(m.get('id'), str) or not m['id']
                       for m in data['models'])):
            self._contract_error('/api/seller/channel/meta', 'models.id')
        if (not isinstance(data.get('groups'), list)
                or any(not isinstance(g, str) or not g for g in data['groups'])):
            self._contract_error('/api/seller/channel/meta', 'groups')
        return data

    def verify(self):
        from ..newapi_formats import get_format_specs

        permissions = self.permissions()
        meta = self._seller_metadata
        cap = {'read': 'supported', 'formats': [s['code'] for s in get_format_specs()
                                               if s['remote_type'] in self.supported_types()],
               'channel_types': sorted(self.supported_types()),
               'channel_config': 'supported', 'account_info': 'local_only', 'custom_models': False,
               'models': [m['id'] for m in meta.get('models', []) if isinstance(m, dict) and isinstance(m.get('id'), str)],
               'groups': [g for g in meta.get('groups', []) if isinstance(g, str)]}
        for remote in ('can_write', 'can_toggle', 'can_edit_routing'):
            cap[remote] = permissions.get(remote) is True
        for action in ('create', 'edit', 'delete'):
            cap[action] = 'supported' if cap['can_write'] else 'permission_denied'
        cap['toggle'] = 'supported' if cap['can_toggle'] else 'permission_denied'
        cap.update({x: 'unsupported' for x in ('stats', 'proxy', 'rpm', 'tpm')})
        cap['test'] = ('supported' if cap['can_write'] else 'permission_denied') if self._compact_seller_dto() else 'unsupported'
        cap['usage'] = 'supported' if self._compact_seller_dto() else 'unsupported'
        cap['multi_key'] = 'supported' if self._compact_seller_dto() else 'unsupported'
        from .vertex_capabilities import vertex_claude_api_key_capability
        cap['vertex_claude_api_key'] = vertex_claude_api_key_capability('silicon-v1', self._current_seller_version)
        if self._compact_seller_dto():
            cap.update(proxy='supported', proxy_encoding='top_level',
                       custom_models=True, model_max_bytes=255,
                       unsupported_config_fields=['auto_ban', 'status_code_mapping'])
        cap['tags'] = 'unverified'
        cap['protocol_contract'] = self._seller_contract_profile
        cap['compatibility_check'] = 'interfaces'
        cap['usage_conversion'] = self.usage_conversion()
        cap.update(version_contract='silicon_seller', settings_encoding='object', remark_max_length=255)
        return {'identity': {'id': self._seller_identity}, 'capabilities': cap,
                'verified_version': self._current_seller_version}

    def channels(self, keyword=None):
        if not getattr(self, '_seller_contract_profile', None):
            self.permissions()
        records = []
        for page in range(1, 1001):
            params = {'p': page, 'page_size': 100}
            if keyword is not None:
                params['keyword'] = keyword
            data = self.request('GET', '/api/seller/channel/', params=params)
            self._check_listing(data)
            rows = data['items']
            records.extend(rows)
            if not rows or len(rows) < 100 or len(records) >= int(data.get('total', 10**9)):
                return records
        raise RemoteError('远端列表超出分页上限，需要人工检查')

    def find_unique_name(self, name):
        rows = [row for row in self.channels(name) if row.get('name') == name]
        if len(rows) > 1:
            raise RemoteError('唯一名称存在多个远端渠道，需人工核实', unknown=True)
        return rows[0] if rows else None

    def detail(self, remote_id):
        if self.SELLER_API and not getattr(self, '_seller_contract_profile', None):
            self.permissions()
        data = self.request('GET', '/api/seller/channel/' + str(remote_id))
        if not isinstance(data, dict) or str(data.get('id')) != str(remote_id):
            raise RemoteError('远端详情响应结构不符合接入契约')
        return data

    @staticmethod
    def _observation_id(remote_id):
        value = str(remote_id)
        if not value.isascii() or not value.isdigit() or int(value) <= 0:
            raise RemoteError('远端渠道编号无效', category='configuration_error')
        return value

    def _observation_owned(self, remote, permissions):
        # Seller details are scoped to the authenticated seller on the server.
        return

    def _observation_version(self):
        self._recheck_seller_contract()

    def _observation_target(self, remote_id, permissions, expected):
        remote = self.detail(remote_id)
        self._observation_owned(remote, permissions)
        if (type(remote.get('id')) is not int or str(remote['id']) != remote_id
                or (expected is not None and (
                    not isinstance(expected, dict) or str(expected.get('id')) != remote_id
                    or not expected.get('name') or remote.get('name') != expected['name']
                    or type(expected.get('type')) is not int or type(remote.get('type')) is not int
                    or remote['type'] != expected['type']))):
            raise RemoteError('远端渠道身份已改变，请同步后重试', category='identity_mismatch')
        return remote

    def test_channel(self, remote_id, model, *, expected=None):
        self._validate_model_syntax([model])
        remote_id = self._observation_id(remote_id)
        permissions = self.permissions()
        if self.SELLER_API and not self._compact_seller_dto():
            raise RemoteError('此卖家版本尚未核实指定模型测试接口', category='unsupported')
        permission = 'can_write' if self.SELLER_API else 'can_toggle'
        if permissions.get(permission) is not True:
            raise RemoteError('当前账号没有渠道测试权限', category='permission_denied')
        remote = self._observation_target(remote_id, permissions, expected)
        models = remote.get('models')
        if not isinstance(models, str) or model not in [value.strip() for value in models.split(',')]:
            raise RemoteError('所选模型不在此远端渠道的模型范围内，请同步后重试', category='configuration_error')
        self._observation_version()
        prefix = '/api/seller/channel/test/' if self.SELLER_API else '/api/channel/test/'
        payload = self._request_envelope('GET', prefix + remote_id, params={'model': model},
                                         side_effect=True, allow_business_failure=True)
        return test_result(payload)

    def channel_status(self, remote_id, *, expected):
        """Read the channel state and verify identity without changing the remote."""
        remote_id = self._observation_id(remote_id)
        permissions = self.permissions()
        self._observation_version()
        remote = self._observation_target(remote_id, permissions, expected)
        return remote.get('status')

    def usage(self, remote_id, *, expected=None):
        remote_id = self._observation_id(remote_id)
        permissions = self.permissions()
        if self.SELLER_API and not self._compact_seller_dto():
            raise RemoteError('此卖家版本尚未核实渠道消耗字段', category='unsupported')
        self._observation_version()
        remote = self._observation_target(remote_id, permissions, expected)
        return extract_usage(remote, conversion=self.usage_conversion())

    def usage_conversion(self, *, refresh=False):
        from .channel_observation import quota_conversion

        if refresh:
            self.permissions()
            self._observation_version()
        version = getattr(self, '_current_version', None) or getattr(self, '_current_seller_version', None) or self.VERSION
        return quota_conversion(getattr(self, '_usage_status', None),
            adapter_kind=getattr(self.site, 'adapter', None) or 'silicon-v1', verified_version=version,
            protocol_contract=getattr(self, '_seller_contract_profile', None))

    @staticmethod
    def _validate_model_syntax(models):
        if (not isinstance(models, list) or not models or len(models) > 200
                or any(not isinstance(model, str) or not model.strip() or model != model.strip()
                       or len(model.encode('utf-8')) > 255 or any(c in model for c in (',', '\n', '\r'))
                       for model in models)):
            raise RemoteError('模型名称不能为空、包含分隔符或超过 255 字节', category='configuration_error')

    def validate_models(self, models, group='default'):
        self._validate_model_syntax(models)
        meta = self.metadata()
        self._validate_models_with_meta(models, group, meta)

    def _validate_models_with_meta(self, models, group, meta):
        self._validate_model_syntax(models)
        allowed = {m['id'] for m in meta.get('models', []) if isinstance(m, dict) and 'id' in m}
        # The compact seller form permits custom models; metadata is a catalog.
        # The legacy seller contract still enforces its model allowlist.
        if not self._compact_seller_dto() and any(m not in allowed for m in models):
            raise RemoteError('远端允许的模型已改变，请核对配置')
        try:
            requested_groups = routing_group_names(group)
        except ValueError:
            raise RemoteError('远端渠道分组格式无效', category='configuration_error') from None
        if any(part not in meta.get('groups', []) for part in requested_groups):
            raise RemoteError('远端不允许指定路由组')

    def supported_types(self):
        return self.CHANNEL_TYPES

    def _config_options(self, *, routing=True):
        return {'supported_types': self.supported_types(), 'routing': routing,
                'supports_proxy': self.SUPPORTS_PROXY or self._compact_seller_dto(), 'supports_rpm': self.SUPPORTS_RPM,
                'object_settings': self.OBJECT_SETTINGS}

    def validate_created_config(self, remote, config, proxy, channel_type):
        from .channel_config import validate_readback

        # Permission capabilities were frozen in the template dispatch decision.
        cap = getattr(self.site, 'capabilities', None) or {}
        routing = cap.get('can_edit_routing') is True
        self._seller_config(config)
        if self._compact_seller_dto():
            # Compare only the seller DTO's writable fields. Its top-level
            # proxy is the equivalent of the administrator DTO's setting.proxy.
            if not isinstance(remote.get('proxy', ''), str):
                raise RemoteError('远端代理配置结构不一致，需人工核实', unknown=True,
                                  category='configuration_mismatch')
            remote = {**remote, 'auto_ban': 1, 'status_code_mapping': None,
                      'setting': json.dumps({'proxy': remote.get('proxy', '')})}
        validate_readback(remote, config, proxy, channel_type, **self._config_options(routing=routing))

    def create(self, *, name, key, models, remark='', group='default', channel_type=1, config=None, proxy='', keys=None):
        from .channel_config import create_payload

        permissions = self.permissions()
        if permissions.get('can_write') is not True:
            raise RemoteError('远端卖家没有创建权限')
        self.validate_models(models, group)
        self._seller_config(config)
        if keys is not None and not self._compact_seller_dto():
            raise RemoteError('此卖家版本尚未验证多密钥容器创建', category='unsupported')
        channel = create_payload(name=name, key=key, models=models, remark=remark, group=group,
                                 channel_type=channel_type, config=config, proxy=proxy, keys=keys,
                                 **self._config_options(routing=permissions.get('can_edit_routing') is True))
        if channel['status'] == 1 and permissions.get('can_toggle') is not True:
            raise RemoteError('创建启用渠道还需要远端启停权限', category='permission_denied')
        normalized_proxy = json.loads(channel.get('setting') or '{}').get('proxy', '')
        channel = self._seller_payload(channel, normalized_proxy)
        body = {'mode': 'multi_to_single' if keys is not None else 'single', 'channel': channel}
        if keys is not None:
            body['multi_key_mode'] = 'random'
        data = self._seller_write('POST', '/api/seller/channel/', body=body)
        return str(data['id']) if isinstance(data, dict) and data.get('id') is not None else None

    def edit(self, remote_id, *, changes, key=None, expected=None, multikey=False):
        permissions = self.permissions()
        if permissions.get('can_write') is not True:
            raise RemoteError('远端卖家没有编辑权限')
        current = self.detail(remote_id)
        if expected and any(current.get(k) != v for k, v in expected.items()):
            raise RemoteError('发现远端人工修改，已停止覆盖；请同步后重新提交')
        if set(changes) - {'name', 'models', 'remark'}:
            raise RemoteError('当前本地工作流不允许修改这些远端字段', category='permission_denied')
        # Explicit writable allowlist, never pass response-only fields or masked keys.
        allowed = {'id', 'name', 'type', 'base_url', 'openai_organization', 'models', 'group',
                   'model_mapping', 'test_model', 'auto_ban', 'status_code_mapping', 'tag',
                   'remark', 'setting', 'param_override', 'header_override', 'settings', 'other'}
        body = {k: v for k, v in current.items() if k in allowed}
        if permissions.get('can_edit_routing') is True:
            body.update({k: current[k] for k in ('priority', 'weight') if k in current})
        body.update(changes)
        body['id'] = int(remote_id)
        # Newer seller responses serialize storage settings as JSON text, while
        # the seller write DTO still takes an object. Never replay a masked key.
        extra = body.get('settings')
        try:
            if extra is None:
                extra = {}
            elif isinstance(extra, str):
                extra = json.loads(extra or '{}')
            if not isinstance(extra, dict):
                raise TypeError
        except (ValueError, TypeError):
            raise RemoteError('远端 settings 不是有效 JSON 对象，停止修改', category='protocol_error') from None
        body['settings'] = extra
        if not isinstance(body.get('setting', '{}'), str):
            raise RemoteError('远端 setting 类型未知，停止修改')
        if 'models' in changes:
            self.validate_models(changes['models'].split(','), body.get('group', 'default'))
        if key is not None:
            body['key'] = key
        remote_proxy = current.get('proxy', '')
        if self._compact_seller_dto() and not isinstance(remote_proxy, str):
            raise RemoteError('远端代理配置结构未知，停止修改', category='protocol_error')
        body = self._seller_payload(body, remote_proxy)
        if multikey:
            if not self._compact_seller_dto():
                raise RemoteError('此卖家版本尚未验证整组密钥替换', category='unsupported')
            body['key_mode'] = 'replace'
        self._seller_write('PUT', '/api/seller/channel/', body=body)

    def toggle(self, remote_id, enabled):
        if self.permissions().get('can_toggle') is not True:
            raise RemoteError('远端卖家没有启停权限')
        self._seller_write('POST', '/api/seller/channel/' + str(remote_id) + '/status', body={'status': 1 if enabled else 2})

    def delete(self, remote_id, *, expected=None):
        if self.permissions().get('can_write') is not True:
            raise RemoteError('远端卖家没有删除权限')
        if expected is not None:
            validate_delete_identity(self.detail(remote_id), expected)
        self._seller_write('DELETE', '/api/seller/channel/' + str(remote_id))


def public_remote(data):
    """Exclude remote key, connection data and free-form metadata from persisted snapshots."""
    result = {k: data[k] for k in ('id', 'name', 'type', 'status', 'models', 'group') if k in data}
    if data.get('base_url') == '***' and data.get('_unverified_config_fields') == ['base_url']:
        result['_unverified_config_fields'] = ['base_url']
    return result
