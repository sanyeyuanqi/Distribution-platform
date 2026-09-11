"""Frozen usage service identities and append-only service discount scopes."""
from datetime import timedelta

from .catalog_policy import CATALOG_FAMILIES
from .channel_services import SERVICE_NAMES


def service_definitions(categories):
    by_family = {category.family: category for category in categories}
    return [{'category_id': by_family[family].id, 'service_variant': variant,
             'label': label, 'label_en': label_en, 'family': family}
            for family in CATALOG_FAMILIES if family in by_family
            for variant, label, label_en in SERVICE_NAMES[family]]


def valid_service_variant(category, variant):
    return category is not None and any(variant == item[0] for item in SERVICE_NAMES.get(category.family, ()))


def service_label(category, variant):
    for value, label, _ in SERVICE_NAMES.get(category.family, ()):
        if value == variant:
            return label
    return category.name + '（大类默认）'


def frozen_service_variant(category, fmt):
    """Use explicit immutable provider formats; never infer old Vertex from models.

    A NULL result means the old category rate remains applicable. A known
    service in an unsplit category deliberately uses the empty string instead.
    """
    if not category:
        return None
    if category.family in ('Anthropic', 'OpenAI', 'OpenRouter', 'OpenCode'):
        return ''
    schema = getattr(fmt, 'schema_config', None)
    if not isinstance(schema, dict):
        return None
    kind, remote_type = schema.get('type'), schema.get('remote_type')
    if category.family == 'AWS':
        if remote_type == 33 and kind in ('aws_ak_sk', 'aws_api_key', 'aws_bedrock'):
            return 'bedrock'
        if remote_type == 14 and kind == 'aws_claude':
            return 'aws_claude'
    elif category.family == 'Azure':
        if remote_type == 3 and kind in ('api_key', 'azure_gpt'):
            return 'azure_gpt'
        if remote_type == 14 and kind == 'azure_claude':
            return 'azure_claude'
    elif category.family == 'Google':
        if remote_type == 24 and kind == 'api_key':
            return 'ai_studio_gemini'
        if remote_type == 41 and kind in ('vertex_gemini', 'vertex_claude'):
            return kind
    return None


def latest_scope_version(history, at, service_variant):
    return next((version for version in reversed(history)
                 if version.effective_at <= at
                 and getattr(version, 'service_variant', None) == service_variant), None)


def scoped_discount(history, at, service_variant=None):
    if service_variant is not None:
        override = latest_scope_version(history, at, service_variant)
        if override and not getattr(override, 'inherits_category', False):
            return override
    return latest_scope_version(history, at, None)


def discount_state(history, at, service_variant):
    override = latest_scope_version(history, at, service_variant) if service_variant is not None else None
    return {'current': scoped_discount(history, at, service_variant), 'override': override,
            'default_current': latest_scope_version(history, at, None),
            'inherited': override is None or bool(getattr(override, 'inherits_category', False))}


def scoped_transition(history, start, end, service_variant):
    """Unrelated service changes and shadowed defaults cannot split this usage."""
    boundaries = {version.effective_at for version in history
                  if start < version.effective_at < end
                  and getattr(version, 'service_variant', None) in (None, service_variant)}
    for boundary in boundaries:
        before = scoped_discount(history, boundary - timedelta(microseconds=1), service_variant)
        after = scoped_discount(history, boundary, service_variant)
        if getattr(before, 'id', None) != getattr(after, 'id', None):
            return True
    return False


def backfill_applies(version, service_variant, original):
    scope = getattr(version, 'service_variant', None)
    if getattr(version, 'inherits_category', False):
        return False
    if scope is not None:
        return scope == service_variant
    # A category backfill cannot erase an explicit service-specific 0% period.
    return original is None or getattr(original, 'service_variant', None) is None


def service_projection(definition, history, at, serialize_version):
    variant = definition['service_variant']
    state = discount_state(history, at, variant)
    def public(value):
        return {**{key: serialize_version(value[key]) if value[key] is not None else None
                   for key in ('current', 'override', 'default_current')}, 'inherited': value['inherited']}

    def identity(value):
        return (getattr(value['current'], 'id', None), value['inherited'])

    future, previous = [], identity(state)
    boundaries = sorted({version.effective_at for version in history
                         if version.effective_at > at
                         and getattr(version, 'service_variant', None) in (None, variant)})
    for boundary in boundaries:
        scheduled = discount_state(history, boundary, variant)
        if identity(scheduled) != previous:
            future.append({'effective_at': boundary, **public(scheduled)})
            previous = identity(scheduled)
    return {**definition, **public(state), 'future': future}
