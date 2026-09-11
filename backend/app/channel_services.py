"""Business-service selectors and authorized usage, independent of wire/auth types."""
from decimal import Decimal, localcontext
from types import SimpleNamespace

from sqlalchemy import and_, case, func, select

from .catalog_policy import CATALOG_FAMILIES
from .models import Category, CredentialFormat, Site
from .models_billing import UsageFact
from .models_channels import Channel, Distribution
from .remote_usage_totals import manual_usage_evidence, remote_usage_total
from .upload_templates import template_variant

# These describe the local business service, not a provider's authorization or
# the receiving site's routing group. Old credential variants keep their IDs.
SERVICE_NAMES = {
    'AWS': (
        ('bedrock', 'AWS Bedrock', 'AWS Bedrock'),
        ('aws_claude', 'AWS Claude 代理', 'Claude on AWS'),
    ),
    'Anthropic': (('', 'Anthropic 官方', 'Anthropic API'),),
    'OpenAI': (('', 'OpenAI', 'OpenAI'),),
    'Azure': (
        ('azure_gpt', 'Azure OpenAI', 'Azure OpenAI'),
        ('azure_claude', 'Azure Claude', 'Azure Claude'),
    ),
    'Google': (
        ('ai_studio_gemini', 'Google AI Studio', 'Google AI Studio'),
        ('vertex_gemini', 'Vertex AI Gemini', 'Vertex AI Gemini'),
        ('vertex_claude', 'Vertex AI Claude', 'Vertex AI Claude'),
    ),
    'OpenRouter': (('', 'OpenRouter', 'OpenRouter'),),
    'OpenCode': (('', 'OpenCode', 'OpenCode'),),
}


def service_details(category, fmt, models=None):
    variant = template_variant(category, fmt, models)
    for value, name, name_en in SERVICE_NAMES.get(category.family, ()):
        if variant == value:
            return {'variant': value, 'service_name': name, 'service_name_en': name_en}
    if category.family == 'Google' and variant == 'vertex_legacy':
        return {'variant': variant, 'service_name': 'Vertex AI（历史接入）',
                'service_name_en': 'Vertex AI (legacy)'}
    if category.family in ('AWS', 'Azure', 'Google'):
        return {'variant': 'legacy', 'service_name': category.family + '（历史接入）',
                'service_name_en': category.family + ' (legacy)'}
    return {'variant': '', 'service_name': category.name, 'service_name_en': category.name}


def filter_service_variant(db, query, variant):
    """Apply the same model-aware legacy classification before pagination/export."""
    if variant is None:
        return query
    rows = db.execute(query.with_only_columns(Channel.id, Channel.models, Category, CredentialFormat)
                      .join(Category, Category.id == Channel.category_id)
                      .join(CredentialFormat, CredentialFormat.id == Channel.format_id))
    selected = [channel_id for channel_id, models, category, fmt in rows
                if service_details(category, fmt, models)['variant'] == variant]
    return query.where(Channel.id.in_(selected))


def _add_amount(amounts, unit, value):
    current = Decimal(amounts.get(unit, '0'))
    # Database SUM can exceed a single Money column's precision. Preserve all
    # integer/fraction digits when combining channel totals, even beyond 28.
    with localcontext() as context:
        context.prec = max(28, max(current.adjusted(), value.adjusted())
                           - min(current.as_tuple().exponent, value.as_tuple().exponent) + 3)
        amounts[unit] = format(current + value, 'f')


def channel_category_options(db, owners, archived=False):
    categories = list(db.scalars(select(Category)))
    by_id = {row.id: row for row in categories}
    result = {}

    def option(category_id, variant, name, name_en):
        return result.setdefault((category_id, variant), {
            'category_id': category_id, 'variant': variant, 'name': name, 'name_en': name_en,
            'channel_count': 0, 'verified_usage_by_unit': {}, 'data_status': 'empty',
            'remote_usage_total': {'amount': '0', 'unit': 'USD', 'covered': 0, 'total': 0},
        })

    for family in CATALOG_FAMILIES:
        for category in categories:
            if category.family == family:
                for variant, name, name_en in SERVICE_NAMES[family]:
                    option(category.id, variant, name, name_en)

    # Select only routing metadata; never load a channel's encrypted credential.
    channel_query = (select(Channel.id, Channel.models, Channel.category_id, CredentialFormat)
                     .join(CredentialFormat, CredentialFormat.id == Channel.format_id)
                     .where(Channel.owner_id.in_(owners)))
    if archived != 'all':
        channel_query = channel_query.where(Channel.archived.is_(archived))
    rows = db.execute(channel_query.order_by(Channel.created_at, Channel.id))
    channel_options = {}
    for channel_id, models, category_id, fmt in rows:
        service = service_details(by_id[category_id], fmt, models)
        item = option(category_id, service['variant'], service['service_name'], service['service_name_en'])
        item['channel_count'] += 1
        channel_options[channel_id] = (category_id, service['variant'])

    # Read only current remote metadata across the entire authorized scope,
    # independent of channel pagination. Never load channel credentials or site tokens.
    distributions = [SimpleNamespace(**dict(row._mapping)) for row in db.execute(select(
        Distribution.id, Distribution.channel_id, Distribution.site_id, Distribution.remote_id,
        Distribution.status, Distribution.last_sync_at, Distribution.remote_snapshot)
        .where(Distribution.channel_id.in_(list(channel_options))))]
    sites = {row.id: SimpleNamespace(**dict(row._mapping)) for row in db.execute(select(
        Site.id, Site.adapter, Site.base_url, Site.verified_at, Site.capabilities)
        .where(Site.id.in_({dist.site_id for dist in distributions})))}
    evidence = manual_usage_evidence(db, distributions)
    by_service = {}
    for dist in distributions:
        by_service.setdefault(channel_options[dist.channel_id], []).append(dist)
    for key, item in result.items():
        if item['channel_count']:
            item['remote_usage_total'] = remote_usage_total(by_service.get(key, []), sites, evidence=evidence)

    qualified = and_(UsageFact.verified.is_(True), UsageFact.amount.is_not(None))
    facts = db.execute(select(UsageFact.channel_id, UsageFact.unit,
                       func.sum(case((qualified, UsageFact.amount), else_=None)),
                       func.count(UsageFact.id), func.sum(case((qualified, 1), else_=0)))
                       .join(Channel, Channel.id == UsageFact.channel_id)
                       .where(UsageFact.channel_id.in_(list(channel_options)), UsageFact.owner_id == Channel.owner_id,
                              Channel.owner_id.in_(owners))
                       .group_by(UsageFact.channel_id, UsageFact.unit))
    covered, incomplete = set(), set()
    for channel_id, unit, amount, count, verified_count in facts:
        key = channel_options[channel_id]
        if amount is not None:
            _add_amount(result[key]['verified_usage_by_unit'], unit, amount)
            covered.add(channel_id)
        if verified_count < count:
            incomplete.add(key)
    for channel_id, key in channel_options.items():
        if channel_id not in covered:
            incomplete.add(key)
    for key, item in result.items():
        if item['channel_count']:
            item['data_status'] = ('missing' if not item['verified_usage_by_unit'] else
                                   'partial' if key in incomplete else 'verified')
    return list(result.values())
