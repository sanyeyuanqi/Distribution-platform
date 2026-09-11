"""Fixed Google upload services, separate from their shared NewAPI wire type."""

GOOGLE_VARIANTS = ('ai_studio_gemini', 'vertex_gemini', 'vertex_claude')
CLAUDE9 = (
    'claude-haiku-4-5-20251001',
    'claude-opus-4-6',
    'claude-opus-4-7',
    'claude-opus-4-8',
    'claude-sonnet-5',
    'claude-fable-5',
    'claude-opus-5',
    'claude-sonnet-4-6',
    'claude-fable-5-1',
)

# Anthropic's Vertex ID table lists the other eight names without conversion:
# https://platform.claude.com/docs/en/build-with-claude/claude-on-vertex-ai
# Matching a published ID does not establish this account's model permission.
_VERTEX_CLAUDE_MAPPING = {
    'claude-haiku-4-5-20251001': 'claude-haiku-4-5@20251001',
}


def google_service_variant(schema, models=None):
    """Classify old definitions conservatively without changing their schema."""
    schema = schema if isinstance(schema, dict) else {}
    kind = schema.get('type')
    if kind in ('vertex_gemini', 'vertex_claude'):
        return kind
    if schema.get('remote_type') == 24:
        return 'ai_studio_gemini'
    if schema.get('remote_type') != 41:
        return ''
    if not isinstance(models, (list, tuple)) or not models:
        return 'vertex_legacy'
    if not all(isinstance(model, str) and model for model in models):
        return 'vertex_legacy'
    if kind in ('vertex_json', 'vertex_api_key') and all(model.startswith('gemini-') for model in models):
        return 'vertex_gemini'
    if kind == 'vertex_json' and all(model.startswith('claude-') for model in models):
        return 'vertex_claude'
    # Older NewAPI API-key requests always use publishers/google, including
    # when a Claude name was selected. Never classify that as usable Claude.
    return 'vertex_legacy'


def google_model_mapping(kind, models):
    """Return only necessary Vertex aliases for the effective selected models."""
    if kind != 'vertex_claude' or not isinstance(models, (list, tuple)):
        return {}
    return {model: _VERTEX_CLAUDE_MAPPING[model] for model in models
            if isinstance(model, str) and model in _VERTEX_CLAUDE_MAPPING}


def _is_claude_model(model):
    if not isinstance(model, str):
        return False
    model = model.strip().lower()
    # NewAPI's Vertex Init selects Anthropic for every "claude" prefix.
    return (model.startswith(('claude', 'anthropic/claude-', 'anthropic.claude-', 'publishers/anthropic/'))
            or model == 'publishers/anthropic'
            or '/publishers/anthropic/' in model)


def service_model_issues(schema, models):
    """Validate explicit Google services; leave historical Vertex formats unchanged."""
    schema = schema if isinstance(schema, dict) else {}
    kind = schema.get('type')
    ai_studio = schema.get('remote_type') == 24
    if not ai_studio and kind not in ('vertex_gemini', 'vertex_claude'):
        return []
    if not isinstance(models, (list, tuple)):
        return ['模型列表格式无效']
    if kind == 'vertex_claude':
        if any(not isinstance(model, str) or model not in CLAUDE9 for model in models):
            return ['Vertex AI · Claude 仅支持已配置的 9 个 Claude 模型']
    elif any(_is_claude_model(model) for model in models):
        service = 'AI Studio · Gemini' if ai_studio else 'Vertex AI · Gemini'
        return [service + ' 不能接收 Claude 模型，请选择 Vertex AI · Claude']
    return []
