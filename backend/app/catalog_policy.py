"""Fixed business categories, separate from the full upstream protocol registry."""
from sqlalchemy import case

from .models import Category

CATALOG_FAMILIES = ('AWS', 'Anthropic', 'OpenAI', 'Azure', 'Google', 'OpenRouter', 'OpenCode')
CATALOG_PROTOCOL_FAMILIES = {
    **{family: (family,) for family in CATALOG_FAMILIES},
    'Google': ('Google', 'VertexAI'),
    'OpenCode': ('Anthropic',),
}
CATALOG_FORMAT_CODES = {
    'AWS': ('newapi-33-aws-ak-sk-v1', 'newapi-33-aws-api-key-v1',
            'newapi-33-aws-bedrock-v1', 'newapi-14-aws-claude-v1'),
    'Anthropic': ('newapi-14-api-key-v1',),
    'OpenAI': ('api_key-v1',),
    'Azure': ('newapi-3-azure-gpt-v1', 'newapi-14-azure-claude-v1'),
    'Google': ('newapi-24-api-key-v1', 'newapi-41-vertex-gemini-v1', 'newapi-41-vertex-claude-v1'),
    'OpenRouter': ('newapi-20-api-key-v1',),
    'OpenCode': ('newapi-14-api-key-v1',),
}

# Exact provider IDs from the public OpenRouter and OpenCode model catalogues.
# These are wire-name conversions for the supported upload names, not a rule for
# inventing aliases for every Claude release or changing the selected model set.
_CATALOG_MODEL_MAPPINGS = {
    'OpenRouter': {
        'claude-haiku-4-5-20251001': 'anthropic/claude-haiku-4.5',
        'claude-opus-4-6': 'anthropic/claude-opus-4.6',
        'claude-opus-4-7': 'anthropic/claude-opus-4.7',
        'claude-opus-4-8': 'anthropic/claude-opus-4.8',
        'claude-sonnet-5': 'anthropic/claude-sonnet-5',
        'claude-fable-5': 'anthropic/claude-fable-5',
        'claude-opus-5': 'anthropic/claude-opus-5',
        'claude-sonnet-4-6': 'anthropic/claude-sonnet-4.6',
        'claude-fable-5-1': 'anthropic/claude-fable-5.1',
    },
    'OpenCode': {
        'claude-haiku-4-5-20251001': 'claude-haiku-4-5',
    },
}


def is_catalog_category(category):
    return bool(category and category.family in CATALOG_FAMILIES)


def catalog_category_filter():
    return Category.family.in_(CATALOG_FAMILIES)


def catalog_category_order():
    return case({family: index for index, family in enumerate(CATALOG_FAMILIES)},
                value=Category.family, else_=len(CATALOG_FAMILIES))


def catalog_supports_protocol(family, protocol_family):
    return protocol_family in CATALOG_PROTOCOL_FAMILIES.get(family, ())


def get_catalog_format_specs():
    from .newapi_formats import get_format_specs
    wire_specs = {spec['code']: spec for spec in get_format_specs()}
    help_by_family = {
        'Anthropic': ('只需填写 API Key（sk-ant-...），走 Anthropic 官方地址，无需 Base URL。每行一个 Key。', 'sk-ant-...'),
        'OpenAI': ('只需填写 API Key（sk-...），走 OpenAI 官方地址，无需 Base URL。每行一个 Key。', 'sk-...'),
        'OpenRouter': ('走 OpenRouter 上的 Claude 模型。只需填写 OpenRouter API Key（sk-or-v1-...），系统自动走官方地址 https://openrouter.ai/api，无需 Base URL。', 'sk-or-v1-...'),
        'OpenCode': ('走 OpenCode 上的 Claude 模型。填写 API Key，系统自动走官方地址 https://opencode.ai/zen，无需 Base URL。每行一个 Key。', '输入 OpenCode API Key，每行一个'),
    }
    specs = []
    for family in CATALOG_FAMILIES:
        for code in CATALOG_FORMAT_CODES[family]:
            spec = {**wire_specs[code], 'family': family}
            if family in help_by_family:
                spec['help'], spec['placeholder'] = help_by_family[family]
            elif family == 'Google' and spec['remote_type'] == 24:
                spec['help'] = '只需填写 Gemini API Key（AIza...），走 Google AI Studio 官方地址，无需 Base URL。每行一个 Key。'
                spec['placeholder'] = 'AIza...'
            specs.append(spec)
    return specs


def implemented_catalog_format(category, fmt):
    if not is_catalog_category(category) or not fmt or fmt.category_id != category.id:
        return None
    from .newapi_formats import format_spec
    spec = format_spec(fmt)
    if spec and catalog_supports_protocol(category.family, spec['family']):
        return spec
    return None


def catalog_format_spec(category, fmt):
    """Only source-owned definitions may be selected for new uploads."""
    if not is_catalog_category(category) or not fmt or fmt.category_id != category.id:
        return None
    from .newapi_formats import protocol_schema
    try:
        schema = protocol_schema(fmt.schema_config)
    except ValueError:
        return None
    return next((spec for spec in get_catalog_format_specs() if spec['family'] == category.family
                 and spec['code'] == fmt.code and spec['version'] == fmt.version
                 and spec['schema_config'] == schema), None)


def simplified_template_config(config):
    status = (config or {}).get('status', 2) if isinstance(config, dict) else 2
    return {'status': status if type(status) is int and status in (1, 2) else 2}


def catalog_default_base_url(category):
    return 'https://opencode.ai/zen' if category and category.family == 'OpenCode' else ''


def catalog_model_mapping(category, models, explicit_mapping):
    """Add fixed wire aliases for the effective models; preserve explicit overrides.

    Call while creating a new effective configuration, after intersecting the
    template and Key model scopes. Existing frozen task snapshots remain intact.
    """
    fixed = _CATALOG_MODEL_MAPPINGS.get(category.family if category else None, {})
    automatic = {model: fixed[model] for model in models if model in fixed}
    return {**automatic, **(explicit_mapping or {})}


def catalog_configuration_issues(category, config=None):
    if category and category.family == 'OpenCode' and not (config or {}).get('base_url'):
        return ['OpenCode 模板必须配置 API 地址，例如 https://opencode.ai/zen']
    return []
