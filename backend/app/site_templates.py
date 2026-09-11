"""Create local, disabled receiving templates when a site is first registered."""
from sqlalchemy import select

from .auth import audit
from .catalog_policy import (
    CATALOG_FAMILIES,
    CATALOG_FORMAT_CODES,
    catalog_category_filter,
    catalog_category_order,
    catalog_format_spec,
)
from .channel_services import service_details
from .models import Category, CredentialFormat, SiteUploadTemplate


def create_site_templates(db, site, actor):
    """Use the installed catalog; never copy another site's receiving settings."""
    existing = set(db.execute(select(SiteUploadTemplate.category_id, SiteUploadTemplate.variant)
                              .where(SiteUploadTemplate.site_id == site.id)).all())
    rows = list(db.execute(select(Category, CredentialFormat)
                          .join(CredentialFormat, CredentialFormat.category_id == Category.id)
                          .where(catalog_category_filter(), Category.active.is_(True), CredentialFormat.enabled.is_(True))
                          .order_by(catalog_category_order(), Category.created_at, Category.id)))

    def preference(item):
        category, fmt = item
        # The combined Bedrock parser accepts both AK/SK and API keys.
        codes = CATALOG_FORMAT_CODES[category.family]
        return (CATALOG_FAMILIES.index(category.family), category.created_at, category.id,
                0 if fmt.code == 'newapi-33-aws-bedrock-v1' else 1,
                codes.index(fmt.code) if fmt.code in codes else len(codes))

    created = []
    for category, fmt in sorted(rows, key=preference):
        if not catalog_format_spec(category, fmt):
            continue
        service = service_details(category, fmt)
        destination = (category.id, service['variant'])
        if destination in existing:
            continue
        template = SiteUploadTemplate(site_id=site.id, category_id=category.id, format_id=fmt.id,
            variant=service['variant'], name=f"{service['service_name']} · {site.name}"[:120],
            enabled=False, models=[], routing_group=site.routing_group or 'default',
            channel_config={'status': 2}, version=1)
        db.add(template)
        db.flush()
        audit(db, actor, 'upload_template.create', 'upload_template', template.id,
              {'site_id': site.id, 'category_id': category.id, 'enabled': False, 'source': 'site.create'})
        existing.add(destination)
        created.append(template)
    return created
