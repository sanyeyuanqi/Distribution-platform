"""New API management contracts pinned to reviewed releases or exact builds.

Reviewed releases are grouped by their actual authorization and status routes.
The bundled manifest pins every supported version, release date and source hash.
An independently reviewed, origin-bound build may hide its release version;
it keeps a distinct identity and never inherits unverified relay capabilities.
This adapter never uses relay keys, credential-reveal routes, or guessed future
contracts.
"""
import json
import re

from ..newapi_formats import get_format_specs
from .newapi_builds import (
    BUILD_REVIEWED_AT,
    REMOTE_GROUP_TOO_LONG_MESSAGE,
    build_metadata,
    get_contract,
    group_length_issue,
    identify_build,
)
from .newapi_compatibility import RELEASES, REVIEWED_AT
from .silicon import RemoteError, SiliconAdapter
from .tcp_red import TcpRedAdapter


class NewAPIAdapter(TcpRedAdapter):
    STABLE_VERSION = 'v0.13.2'
    RC_VERSIONS = frozenset(version for version in RELEASES if version.startswith('v1.0.0-rc.'))
    VERSIONS = frozenset(RELEASES)
    RBAC_VERSIONS = frozenset(version for version, contract in RELEASES.items() if contract.uses_channel_permissions)
    LEGACY_VERSIONS = VERSIONS - RBAC_VERSIONS
    SUPPORTS_RPM = False
    CONNECTION_GROUP_SCOPE = None
    CREATE_PATH = '/api/channel/'
    MASKED_CONFIG_FIELDS = frozenset()

    def _version(self):
        status = SiliconAdapter.request(self, 'GET', '/api/status')
        if not isinstance(status, dict) or not isinstance(status.get('version'), str):
            raise RemoteError('NewAPI 未返回有效版本信息', category='protocol_error', reason='invalid_version_response',
                              endpoint='/api/status', method='GET')
        version = status['version']
        if version == '':
            version = identify_build(self.site.base_url, status, self.transport)
        elif version not in self.VERSIONS:
            raise RemoteError('NewAPI 版本未包含在已核对的兼容清单中，请核对版本后更新接入支持', category='protocol_error',
                              reason='unsupported_version', observed_version=version, endpoint='/api/status', method='GET')
        if getattr(self, 'frozen_build_id', version) != version:
            raise RemoteError('站点构建与冻结任务不匹配，请重新验证并准备任务', category='protocol_error',
                              reason='build_changed', endpoint='/api/status', method='GET')
        return version

    def request(self, method, path, *, body=None, params=None):
        try:
            if method not in ('GET', 'HEAD'):
                version = self._version()
                if version != getattr(self, '_current_version', None):
                    raise RemoteError('操作期间平台版本发生变化，请重新验证站点', category='protocol_error')
                if method in ('POST', 'PUT') and path == '/api/channel/' and isinstance(body, dict):
                    channel = body.get('channel') if method == 'POST' else body
                    if isinstance(channel, dict) and (issue := group_length_issue(version, channel.get('group'))):
                        raise RemoteError(issue, category='configuration_error', reason='group_too_long')
                if (build_metadata(version) and method == 'PUT' and path == '/api/channel/'
                        and isinstance(body, dict) and body.get('type') == 41 and 'key' in body
                        and json.loads(body.get('settings') or '{}').get('vertex_key_type') == 'api_key'
                        and any(separator in body['key'] for separator in ('\n', '\r'))):
                    raise RemoteError('此构建的 Vertex API Key 仅支持单密钥远端渠道', category='configuration_error')
            return SiliconAdapter.request(self, method, path, body=body, params=params)
        except RemoteError as exc:
            exc.endpoint, exc.method = path, method
            raise

    def _business_error(self, payload, *, method, path):
        message = payload.get('message')
        if (build_metadata(getattr(self, '_current_version', None))
                and method in ('POST', 'PUT') and path == '/api/channel/'
                and isinstance(message, str)
                and re.fullmatch(r"Error 1406 \(22001\): Data too long for column 'group' at row [1-9][0-9]*", message)):
            return RemoteError(REMOTE_GROUP_TOO_LONG_MESSAGE, category='configuration_error', reason='group_too_long')
        return super()._business_error(payload, method=method, path=path)

    def supported_types(self):
        version = getattr(self, '_current_version', None)
        if version is None:
            version = self._version()
        return frozenset(s['remote_type'] for s in get_format_specs()) & get_contract(version).channel_types

    def _observation_version(self):
        if self._version() != getattr(self, '_current_version', None):
            raise RemoteError('操作期间平台版本发生变化，请重新验证站点', category='protocol_error')

    def _config_options(self, *, routing=True):
        from .bedrock_capabilities import bedrock_api_key_sdk_mode
        return {**super()._config_options(routing=routing),
                'bedrock_api_key_sdk': bedrock_api_key_sdk_mode('new-api-v1', getattr(self, '_current_version', None))
                    and getattr(self, 'frozen_bedrock_api_key_sdk', True)}

    def _create_request(self, body):
        if not build_metadata(self._current_version):
            return super()._create_request(body)
        channel = body['channel']
        if channel['type'] == 41 and json.loads(channel['settings']).get('vertex_key_type') == 'api_key':
            if '\n' in channel['key'] or '\r' in channel['key']:
                raise RemoteError('此构建的 Vertex API Key 仅支持单密钥远端渠道', category='configuration_error')
            body = {'mode': 'single', 'channel': channel}
        # The browser follows the no-slash route's 307 automatically. Send the
        # canonical endpoint directly; never redirect or replay credential POSTs.
        return self.request('POST', self.CREATE_PATH, body=body)

    def permissions(self):
        self._current_version = self._version()
        identity = self.request('GET', '/api/user/self')
        if (not isinstance(identity, dict) or type(identity.get('id')) is not int
                or str(identity['id']) != str(self.site.seller_user_id)):
            raise RemoteError('管理用户编号与令牌身份不匹配', category='identity_mismatch')
        if identity.get('status') != 1 or type(identity.get('role')) is not int or identity['role'] not in (10, 100):
            raise RemoteError('NewAPI 管理接口需要已启用的管理员账号', category='permission_denied')
        if not get_contract(self._current_version).uses_channel_permissions:
            # Only reviewed AdminAuth release/build contracts authorize operations by
            # roles 10/100. Missing capabilities never downgrades an RBAC release.
            flags = dict.fromkeys(('read', 'write', 'operate', 'sensitive_write'), True)
        else:
            permissions = identity.get('permissions')
            admin = permissions.get('admin_permissions') if isinstance(permissions, dict) else None
            channel = admin.get('channel') if isinstance(admin, dict) else None
            if not isinstance(channel, dict) or channel.get('read') is not True:
                raise RemoteError('NewAPI 未授予明确的渠道读取权限', category='permission_denied')
            flags = {name: channel.get(name) is True for name in ('read', 'write', 'operate', 'sensitive_write')}
        return {**flags, 'identity_id': str(identity['id']),
                'can_write': flags['write'] and flags['sensitive_write'],
                'can_toggle': flags['operate'], 'can_edit_routing': flags['write']}

    def metadata(self):
        models = self.request('GET', '/api/channel/models')
        groups = self.request('GET', '/api/group/')
        if (not isinstance(models, list)
                or any(not isinstance(m, dict) or not isinstance(m.get('id'), str) or not m['id'] for m in models)
                or not isinstance(groups, list) or any(not isinstance(g, str) or not g for g in groups)):
            raise RemoteError('NewAPI 模型或分组响应不符合接入契约', category='protocol_error')
        return {'models': models, 'groups': groups}

    def validate_models(self, models, group='default'):
        # Official AddChannel permits custom names; its catalog is a suggestion.
        self._validate_model_syntax(models)
        if issue := group_length_issue(getattr(self, '_current_version', None), group):
            raise RemoteError(issue, category='configuration_error', reason='group_too_long')
        meta = self.metadata()
        if (not isinstance(group, str) or any(not g or g not in meta['groups']
                                              for g in group.split(','))):
            raise RemoteError('远端不允许指定渠道分组', category='configuration_error')

    def _assert_owned(self, remote, permissions):
        # Official Channel has no created_by field. Its management API applies
        # administrator permissions; tenant isolation is enforced locally.
        return

    @staticmethod
    def _remark(remark):
        if not isinstance(remark, str) or len(remark) > 255:
            raise RemoteError('NewAPI 渠道备注不能超过 255 个字符', category='configuration_error')
        return remark

    def verify(self):
        permissions = self.permissions()
        self._page(page_size=1)
        meta = self.metadata()
        types = self.supported_types()
        cap = {'read': 'supported', 'formats': [s['code'] for s in get_format_specs() if s['remote_type'] in types],
               'channel_types': sorted(types), 'channel_config': 'supported',
               'models': list(dict.fromkeys(m['id'] for m in meta['models'])),
               'groups': list(dict.fromkeys(meta['groups'])), 'custom_models': True,
               'can_write': permissions['can_write'], 'can_toggle': permissions['can_toggle'],
               'can_edit_routing': permissions['can_edit_routing'],
               'remote_permissions': {name: permissions[name] for name in ('read', 'write', 'operate', 'sensitive_write')},
               'remark_max_length': 255, 'model_max_bytes': 255, 'proxy': 'supported', 'account_info': 'local_only',
               'rpm': 'unsupported', 'tpm': 'unsupported', 'stats': 'unsupported', 'tags': 'unverified',
               'test': 'supported' if permissions['can_toggle'] else 'permission_denied', 'usage': 'supported'}
        for action in ('create', 'edit', 'delete'):
            cap[action] = 'supported' if permissions['can_write'] else 'permission_denied'
        cap['toggle'] = 'supported' if permissions['can_toggle'] else 'permission_denied'
        cap['multi_key'] = 'supported'
        from .vertex_capabilities import vertex_claude_api_key_capability
        cap['vertex_claude_api_key'] = vertex_claude_api_key_capability('new-api-v1', self._current_version)
        cap['version_contract'] = get_contract(self._current_version).permission_model
        cap['compatibility_reviewed_at'] = REVIEWED_AT
        if build := build_metadata(self._current_version):
            cap['verified_build'] = build
            cap['compatibility_reviewed_at'] = BUILD_REVIEWED_AT
        cap['usage_conversion'] = self.usage_conversion()
        return {'identity': {'id': permissions['identity_id']}, 'capabilities': cap,
                'verified_version': self._current_version}

    def toggle(self, remote_id, enabled):
        permissions = self.permissions()
        if not permissions['can_toggle']:
            raise RemoteError('NewAPI 账号没有渠道启停权限', category='permission_denied')
        remote_id = self._remote_id(remote_id)
        self.detail(remote_id)
        if not get_contract(self._current_version).uses_channel_permissions:
            self.request('PUT', '/api/channel/', body={'id': int(remote_id), 'status': 1 if enabled else 2})
        else:
            self.request('POST', '/api/channel/' + remote_id + '/status', body={'status': 1 if enabled else 2})
