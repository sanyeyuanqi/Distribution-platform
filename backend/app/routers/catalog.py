"""Fixed credential definitions for template and upload selectors."""
# ruff: noqa: B008
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..catalog_policy import (
    catalog_category_filter,
    catalog_category_order,
    catalog_default_base_url,
    catalog_format_spec,
)
from ..channel_services import service_details
from ..db import get_db
from ..models import Category, CredentialFormat, User
from ..newapi_formats import format_default_models

router = APIRouter(tags=['Catalog'])


def format_json(category, row):
    spec = catalog_format_spec(category, row)
    service = service_details(category, row)
    return {'id': row.id, 'category_id': row.category_id, 'code': spec['code'], 'name': spec['name'],
            'version': spec['version'], 'enabled': row.enabled, 'schema_config': spec['schema_config'],
            'default_models': format_default_models(spec['schema_config']),
            'service_variant': service['variant'], 'service_name': service['service_name'],
            'service_name_en': service['service_name_en'],
            'implemented': True, 'issues': [],
            **{key: spec[key] for key in ('help', 'placeholder', 'json', 'remote_type')},
            'input_mode': spec.get('input_mode', 'json' if spec['json'] else 'text')}


def category_json(db, row):
    formats = db.scalars(select(CredentialFormat).where(CredentialFormat.category_id == row.id))
    return {'id': row.id, 'name': row.family, 'family': row.family, 'active': row.active,
            'default_base_url': catalog_default_base_url(row),
            'formats': [format_json(row, fmt) for fmt in formats if catalog_format_spec(row, fmt)]}


@router.get('/categories')
def categories(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Category).where(catalog_category_filter()).order_by(catalog_category_order())).all()
    return {'items': [category_json(db, row) for row in rows], 'total': len(rows)}


@router.get('/formats')
def formats(category_id: str | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = select(CredentialFormat, Category).join(Category, Category.id == CredentialFormat.category_id).where(catalog_category_filter())
    if category_id:
        query = query.where(CredentialFormat.category_id == category_id)
    items = [format_json(category, row) for row, category in db.execute(query) if catalog_format_spec(category, row)]
    return {'items': items, 'total': len(items)}
