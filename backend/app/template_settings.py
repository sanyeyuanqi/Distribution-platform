"""Validated non-secret channel defaults; proxies have a separate encrypted column."""
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AccountInfo(BaseModel):
    model_config = ConfigDict(extra='forbid')
    balance_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False, strict=True)
    rpm: int | None = Field(default=None, ge=0, strict=True)
    tpm: int | None = Field(default=None, ge=0, strict=True)
    prepaid: bool | None = Field(default=None, strict=True)
    kd: bool | None = Field(default=None, strict=True)


def validate_proxy(value):
    value = value.strip()
    if not value:
        return ''
    try:
        parsed = urlsplit(value)
        if (len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value)
                or parsed.scheme not in ('http', 'https', 'socks5') or not parsed.hostname
                or not parsed.port or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError
    except ValueError:
        raise ValueError('代理需要合法协议、主机及端口，且不能包含路径、查询或片段') from None
    return value


def proxy_hint(value):
    if not value:
        return ''
    parsed = urlsplit(value)
    host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
    return f'{parsed.scheme}://{"***@" if parsed.username or parsed.password else ""}{host}:{parsed.port}'


class ChannelConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_url: str = Field(default='', max_length=1000)
    organization: str = Field(default='', max_length=200)
    other: str = Field(default='', max_length=4000)
    azure_responses_version: str = Field(default='', max_length=100)
    status: Literal[1, 2] = 2
    priority: int = Field(default=0, ge=0, le=2147483647, strict=True)
    weight: int = Field(default=1, ge=0, le=2147483647, strict=True)
    auto_ban: Literal[0, 1] = 1
    model_mapping: dict[str, str] = Field(default_factory=dict, max_length=200)
    status_code_mapping: dict[str, str] = Field(default_factory=dict, max_length=100)
    default_upload_mode: Literal['single', 'batch'] = 'batch'
    rpm_enabled: bool = Field(default=False, strict=True)
    rpm_limit: int | None = Field(default=None, ge=1, le=1000000, strict=True)
    account_info: AccountInfo = Field(default_factory=AccountInfo)

    @field_validator('status', 'auto_ban', mode='before')
    @classmethod
    def integer_flags(cls, value):
        if type(value) is not int:
            raise ValueError('状态和自动禁用选项必须使用整数值')
        return value

    @field_validator('base_url')
    @classmethod
    def url(cls, value):
        value = value.strip().rstrip('/')
        if value:
            parsed = urlsplit(value)
            if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
                raise ValueError('接口地址需要不含账号密码或查询参数的 HTTP(S) 地址')
        return value

    @field_validator('organization')
    @classmethod
    def organization_value(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError('组织标识不能包含控制字符')
        return value.strip()

    @field_validator('model_mapping')
    @classmethod
    def model_map(cls, value):
        if any(not k.strip() or not v.strip() or len(k) > 200 or len(v) > 200
               or any(c in k + v for c in ('\n', '\r', ',')) for k, v in value.items()):
            raise ValueError('模型映射必须使用非空模型名称，且不能包含逗号或换行')
        return {k.strip(): v.strip() for k, v in value.items()}

    @field_validator('status_code_mapping')
    @classmethod
    def status_map(cls, value):
        if any(not k.isdigit() or not v.isdigit() or not 100 <= int(k) <= 599
               or not 100 <= int(v) <= 599 for k, v in value.items()):
            raise ValueError('状态码映射必须使用 100 到 599 的状态码字符串')
        return value

    @model_validator(mode='after')
    def rpm_settings(self):
        if self.rpm_enabled and self.rpm_limit is None:
            raise ValueError('开启 RPM 保护时必须填写 RPM 上限')
        return self


def config_defaults(value=None):
    return ChannelConfig.model_validate(value or {}).model_dump(mode='json')
