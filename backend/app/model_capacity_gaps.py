"""Platform planning totals from template demand and declared local capacity."""
from sqlalchemy import func, select

from .channel_services import service_details
from .db import utcnow
from .models import Category, CredentialFormat, Site, SiteUploadTemplate
from .models_channels import Channel, Distribution, UnclaimedChannel


def _object(value):
    return value if isinstance(value, dict) else {}


def _models(value):
    if not isinstance(value, list):
        return set()
    return {model for model in value if isinstance(model, str) and model and model == model.strip()}


def _nonnegative_integer(value):
    # bool is an int subclass; it is never a capacity declaration.
    return value if type(value) is int and value >= 0 else None


def _capacity(settings, snapshots, metric):
    account = _object(settings).get('account_info')
    if isinstance(account, dict) and metric in account:
        # Explicit null/invalid declarations cannot revive an older value.
        return _nonnegative_integer(account[metric])
    if account is not None and not isinstance(account, dict):
        return None
    legacy = []
    for snapshot in snapshots:
        config = _object(_object(snapshot).get('effective_channel_config'))
        legacy.append(_nonnegative_integer(_object(config.get('account_info')).get(metric)))
    # Frozen copies describe the same capacity, never independent capacity.
    if not legacy or legacy[0] is None or any(value != legacy[0] for value in legacy):
        return None
    return legacy[0]


def platform_model_gaps(db):
    categories = {row.id: row for row in db.scalars(select(Category))}
    formats = {row.id: row for row in db.scalars(select(CredentialFormat))}
    result = {}

    def row_for(category_id, format_id, source_models, model):
        category, fmt = categories.get(category_id), formats.get(format_id)
        if not category or not fmt or fmt.category_id != category_id:
            return None
        service = service_details(category, fmt, source_models)
        return result.setdefault((category_id, service['variant'], model), {
            'category_id': category_id, 'category_name': category.name, 'variant': service['variant'],
            'type_label': service['service_name'], 'type_label_en': service['service_name_en'], 'model': model,
            'required_rpm': None, 'required_tpm': None, 'supplied_rpm': 0, 'supplied_tpm': 0,
            'unknown_rpm_channels': 0, 'unknown_tpm_channels': 0, 'channel_count': 0,
        })

    # Planning includes saved drafts and disabled switches; only archived sites
    # leave the platform plan. Select metadata, never encrypted credentials.
    templates = db.execute(select(SiteUploadTemplate.category_id, SiteUploadTemplate.format_id,
        SiteUploadTemplate.models, SiteUploadTemplate.model_rpm_requirements, SiteUploadTemplate.model_tpm_requirements)
        .join(Site, Site.id == SiteUploadTemplate.site_id).where(Site.archived.is_(False)))
    for category_id, format_id, models, rpms, tpms in templates:
        for model in _models(models):
            row = row_for(category_id, format_id, models, model)
            if row is None:
                continue
            for metric, demand, maximum in (('rpm', rpms, 1_000_000), ('tpm', tpms, 1_000_000_000)):
                value = _nonnegative_integer(_object(demand).get(model))
                if value is not None and 1 <= value <= maximum:
                    field = 'required_' + metric
                    row[field] = (row[field] or 0) + value

    distributions = db.execute(select(Channel.id, Channel.category_id, Channel.format_id, Channel.models,
        Channel.upload_settings, Distribution.models.label('distributed_models'), Distribution.template_snapshot,
        Distribution.remote_snapshot['_local_deletion'].label('local_deletion'))
        .join(Distribution, Distribution.channel_id == Channel.id).join(Site, Site.id == Distribution.site_id)
        .where(Channel.archived.is_(False), Site.archived.is_(False), Distribution.remote_id.is_not(None),
               Distribution.remote_id != '', Distribution.status.in_(('enabled', 'disabled', '1', '2'))))
    channels = {}
    for channel_id, category_id, format_id, models, settings, distributed_models, snapshot, deletion in distributions:
        if isinstance(deletion, dict) and deletion.get('remote_confirmed') is False:
            continue
        entry = channels.setdefault(channel_id, {'category_id': category_id, 'format_id': format_id,
            'models': models, 'settings': settings, 'distributed_models': set(), 'snapshots': []})
        entry['distributed_models'].update(_models(distributed_models))
        entry['snapshots'].append(snapshot)
    for entry in channels.values():
        amounts = {metric: _capacity(entry['settings'], entry['snapshots'], metric) for metric in ('rpm', 'tpm')}
        for model in entry['distributed_models']:
            row = row_for(entry['category_id'], entry['format_id'], entry['models'], model)
            if row is None:
                continue
            row['channel_count'] += 1
            for metric, amount in amounts.items():
                if amount is None:
                    row['unknown_' + metric + '_channels'] += 1
                else:
                    row['supplied_' + metric] += amount

    for row in result.values():
        row['data_status'] = 'partial' if row['unknown_rpm_channels'] or row['unknown_tpm_channels'] else 'declared'
        for metric in ('rpm', 'tpm'):
            required, supplied = row['required_' + metric], row['supplied_' + metric]
            gap = max(required - supplied, 0) if required is not None else None
            row['gap_' + metric] = str(gap) if gap is not None else None
            row[metric + '_gap_estimated'] = bool(gap and row['unknown_' + metric + '_channels'])
            row['required_' + metric] = str(required) if required is not None else None
            row['supplied_' + metric] = str(supplied)
    items = sorted(result.values(), key=lambda row: (row['category_name'], row['type_label'], row['model']))
    unclaimed = db.scalar(select(func.count()).select_from(UnclaimedChannel)
        .join(Site, Site.id == UnclaimedChannel.site_id)
        .where(Site.archived.is_(False), UnclaimedChannel.adopted_channel_id.is_(None)))
    return {'items': items, 'total': len(items), 'scope': 'platform', 'capacity_basis': 'declared_channel',
            'updated_at': utcnow().isoformat() + 'Z', 'unclaimed_channel_count': unclaimed,
            'notes': ['供给为本地渠道申报容量，未核实实际吞吐；同一渠道的多个模型共享该容量。',
                      '同一渠道的站点副本、分区和 Key 数不会重复计入；不同本地渠道仍可能共用上游配额。',
                      '未认领渠道不计入容量；未填写容量的渠道单独标记，缺口按已知申报量计算。']}
