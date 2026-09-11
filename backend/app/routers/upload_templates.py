# ruff: noqa: B008
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import audit, require_roles
from ..catalog_policy import (
    catalog_category_filter,
    catalog_format_spec,
    is_catalog_category,
    simplified_template_config,
)
from ..channel_service import UploadInput, api_json, supported_format
from ..channel_services import service_details
from ..db import get_db, uid, utcnow
from ..models import Category, CredentialFormat, Site, SiteUploadTemplate
from ..routing_groups import normalize_routing_groups
from ..site_distribution import stop_unready_distribution
from ..site_verification import verification_state
from ..template_settings import (
    ChannelConfig,
    validate_proxy,
)
from ..upload_templates import (
    aws_url_issues,
    same_format_type,
    template_issues,
    template_variant,
)

TemplateRoutingGroups = Annotated[str, AfterValidator(normalize_routing_groups)]
ModelRPM = Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
ModelTPM = Annotated[int, Field(strict=True, ge=1, le=1_000_000_000)]
REQUIREMENT_FIELDS = {'model_rpm_requirements': 'RPM', 'model_tpm_requirements': 'TPM'}


def exact_requirement_model(value):
    if not value or value != value.strip() or len(value) > 200 or ',' in value or any(ord(char) < 32 for char in value):
        raise ValueError('需求模型名称须与已选模型精确一致，不能含首尾空白、逗号或控制字符')
    return value


RequirementModel = Annotated[str, Field(strict=True), AfterValidator(exact_requirement_model)]


def exact_mapping_model(value):
    if (not value or value != value.strip() or len(value) > 200 or len(value.encode('utf-8')) > 255
            or ',' in value or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('映射模型名称不能为空，不能含首尾空白、逗号或控制字符，且不能超过 200 字或 255 字节')
    return value


MappingModel = Annotated[str, Field(strict=True), AfterValidator(exact_mapping_model)]

router = APIRouter(prefix='/upload-templates', tags=['Upload templates'])


class TemplateCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    site_id: str
    category_id: str
    format_id: str
    name: str = Field(default='', max_length=120)
    enabled: bool = True
    models: list[str] = Field(default_factory=list, max_length=200)
    model_rpm_requirements: dict[RequirementModel, ModelRPM] = Field(default_factory=dict, max_length=200)
    model_tpm_requirements: dict[RequirementModel, ModelTPM] = Field(default_factory=dict, max_length=200)
    model_mapping: dict[MappingModel, MappingModel] = Field(default_factory=dict, max_length=200)
    routing_group: TemplateRoutingGroups = 'default'
    remark: str = Field(default='', max_length=4000)
    channel_config: ChannelConfig = Field(default_factory=ChannelConfig)
    proxy: str = Field(default='', max_length=4096)

    @field_validator('proxy')
    @classmethod
    def proxy_value(cls, value):
        return validate_proxy(value)

    @field_validator('models')
    @classmethod
    def validate_models(cls, values):
        return UploadInput.model_list(values)


class TemplatePatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format_id: str | None = None
    name: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None
    models: list[str] | None = Field(default=None, max_length=200)
    model_rpm_requirements: dict[RequirementModel, ModelRPM] | None = Field(default=None, max_length=200)
    model_tpm_requirements: dict[RequirementModel, ModelTPM] | None = Field(default=None, max_length=200)
    model_mapping: dict[MappingModel, MappingModel] | None = Field(default=None, max_length=200)
    routing_group: TemplateRoutingGroups | None = None
    remark: str | None = Field(default=None, max_length=4000)
    channel_config: ChannelConfig | None = None
    proxy: str | None = Field(default=None, max_length=4096)

    @field_validator('proxy')
    @classmethod
    def proxy_value(cls, value):
        return validate_proxy(value) if value is not None else None

    @field_validator('models')
    @classmethod
    def validate_models(cls, values):
        return UploadInput.model_list(values) if values is not None else None


def template_json(db, row):
    site, category, fmt = db.get(Site, row.site_id), db.get(Category, row.category_id), db.get(CredentialFormat, row.format_id)
    service = service_details(category, fmt, row.models) if category else {
        'variant': '', 'service_name': '', 'service_name_en': ''}
    issues = template_issues(row, site, category, fmt)
    if not row.enabled:
        issues.insert(0, '模板已停用')
    verification, error = verification_state(site)
    return api_json({**{k: getattr(row, k) for k in ('id', 'display_id', 'site_id', 'category_id', 'format_id', 'variant', 'name', 'enabled',
                    'models', 'model_rpm_requirements', 'model_tpm_requirements', 'model_mapping', 'routing_group', 'remark', 'version', 'created_at', 'updated_at')},
                    'site_name': site.name if site else '', 'category_name': category.name if category else '',
                    'category_family': category.family if category else '',
                    'service_variant': service['variant'], 'service_name': service['service_name'],
                    'service_name_en': service['service_name_en'],
                    'site_verification_status': verification, 'site_verification_error': error,
                    'channel_config': simplified_template_config(row.channel_config),
                    'format_name': fmt.name if fmt else '', 'issues': issues, 'ready': not issues})


def validate_template(db, row):
    if any(model not in row.models for model in (row.model_mapping or {})):
        raise HTTPException(422, '模型映射的原始模型只能使用当前已选模型')
    for field, unit in REQUIREMENT_FIELDS.items():
        if any(model not in row.models for model in (getattr(row, field) or {})):
            raise HTTPException(422, f'模型 {unit} 需求只能配置在当前已选模型中')
    category = db.scalar(select(Category).where(Category.id == row.category_id).with_for_update())
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.id == row.format_id)
                    .with_for_update().execution_options(populate_existing=True))
    # Template/catalog locks come first. Site enablement holds only the Site
    # lock and reads templates, so it never reverses this order.
    site = db.scalar(select(Site).where(Site.id == row.site_id).with_for_update()
                     .execution_options(populate_existing=True))
    if not site or site.archived or not category or not fmt or fmt.category_id != row.category_id:
        raise HTTPException(422, '站点、分类或凭据格式不存在或不匹配')
    if not is_catalog_category(category):
        raise HTTPException(422, '此分类的接入协议尚未实现')
    if not catalog_format_spec(category, fmt) and not (db.get(SiteUploadTemplate, row.id) and supported_format(category, fmt)):
        raise HTTPException(422, '凭据定义的解析方式与此分类的接入协议不匹配或尚未实现')
    row.channel_config = simplified_template_config(row.channel_config)
    row.proxy_encrypted = None
    row.variant = template_variant(category, fmt, row.models)
    url_issues = aws_url_issues(fmt, row.channel_config)
    if url_issues:
        raise HTTPException(422, '；'.join(url_issues))
    if row.enabled:
        others = db.scalars(select(SiteUploadTemplate).where(SiteUploadTemplate.category_id == row.category_id,
            SiteUploadTemplate.id != row.id, SiteUploadTemplate.enabled.is_(True), SiteUploadTemplate.format_id != row.format_id))
        for other in others:
            other_fmt = db.get(CredentialFormat, other.format_id)
            if template_variant(category, other_fmt, other.models) != row.variant:
                continue
            if row.variant in ('bedrock', 'azure_gpt', 'azure_claude', 'vertex_gemini', 'vertex_claude') and same_format_type(fmt, other_fmt):
                continue
            raise HTTPException(422, '同一分类、同一接入类型的启用模板必须使用同一个凭据格式')
        issues = template_issues(row, site, category, fmt, include_availability=False)
        if issues:
            raise HTTPException(422, '；'.join(issues))


def save(db):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '该站点已存在此分类和接入类型的模板，请编辑已有模板') from None


@router.get('')
def templates(site_id: str | None = None, category_id: str | None = None,
              user=Depends(require_roles('superadmin')), db: Session = Depends(get_db)):
    query = select(SiteUploadTemplate).join(Category, Category.id == SiteUploadTemplate.category_id).where(catalog_category_filter())
    if site_id:
        query = query.where(SiteUploadTemplate.site_id == site_id)
    if category_id:
        query = query.where(SiteUploadTemplate.category_id == category_id)
    rows = list(db.scalars(query.order_by(SiteUploadTemplate.display_id.asc())))
    return {'items': [template_json(db, row) for row in rows], 'total': len(rows)}


@router.post('', status_code=201)
def create(body: TemplateCreate, user=Depends(require_roles('superadmin')), db: Session = Depends(get_db)):
    values = body.model_dump(mode='json', exclude={'proxy'})
    values['channel_config'] = simplified_template_config(values['channel_config'])
    row = SiteUploadTemplate(id=uid(), **values, version=1,
                             proxy_encrypted=None)
    validate_template(db, row)
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '该站点已存在此分类和接入类型的模板，请编辑已有模板') from None
    stop_unready_distribution(db, db.get(Site, row.site_id), user)
    audit(db, user, 'upload_template.create', 'upload_template', row.id,
          {'site_id': row.site_id, 'category_id': row.category_id, 'enabled': row.enabled,
           'model_rpm_requirement_count': len(row.model_rpm_requirements),
           'model_tpm_requirement_count': len(row.model_tpm_requirements),
           'model_mapping_count': len(row.model_mapping)})
    save(db)
    return template_json(db, row)


@router.get('/{template_id}')
def detail(template_id: str, user=Depends(require_roles('superadmin')), db: Session = Depends(get_db)):
    row = db.get(SiteUploadTemplate, template_id)
    if not row:
        raise HTTPException(404, '模板不存在')
    return template_json(db, row)


@router.patch('/{template_id}')
def patch(template_id: str, body: TemplatePatch, user=Depends(require_roles('superadmin')), db: Session = Depends(get_db)):
    row = lock_template(db, template_id)
    values = body.model_dump(exclude_unset=True)
    if any(value is None for value in values.values()):
        raise HTTPException(422, '模板字段不能为空值')
    values.pop('proxy', None)
    if row.proxy_encrypted:
        values['proxy_encrypted'] = None
    if 'channel_config' in values:
        values['channel_config'] = simplified_template_config(values['channel_config'])
    if 'models' in values:
        for field in (*REQUIREMENT_FIELDS, 'model_mapping'):
            if field not in values:
                values[field] = {model: demand for model, demand in (getattr(row, field) or {}).items()
                                 if model in values['models']}
    previous_config = row.channel_config
    changed = any(getattr(row, name) != value for name, value in values.items())
    for name, value in values.items():
        setattr(row, name, value)
    with db.no_autoflush:
        validate_template(db, row)
    if row.channel_config != previous_config:
        changed = True
        values['channel_config'] = row.channel_config
    if changed:
        row.version += 1
        row.updated_at = utcnow()
    db.flush()
    stop_unready_distribution(db, db.get(Site, row.site_id), user)
    audit(db, user, 'upload_template.update', 'upload_template', row.id,
          {'fields': sorted(values), 'version': row.version, 'enabled': row.enabled})
    save(db)
    return template_json(db, row)


@router.delete('/{template_id}')
def delete(template_id: str, user=Depends(require_roles('superadmin')), db: Session = Depends(get_db)):
    row = lock_template(db, template_id)
    site = db.scalar(select(Site).where(Site.id == row.site_id).with_for_update()
                     .execution_options(populate_existing=True))
    audit(db, user, 'upload_template.delete', 'upload_template', row.id, {'site_id': row.site_id, 'category_id': row.category_id})
    db.delete(row)
    db.flush()
    if site:
        stop_unready_distribution(db, site, user)
    db.commit()
    return {'deleted': True, 'id': template_id}


def lock_template(db, template_id):
    selected = db.get(SiteUploadTemplate, template_id)
    if not selected:
        raise HTTPException(404, '模板不存在')
    # Match upload/catalog lock order so template edits cannot deadlock with
    # a first upload inserting template-referencing distribution records.
    db.scalar(select(Category).where(Category.id == selected.category_id).with_for_update())
    row = db.scalar(select(SiteUploadTemplate).where(SiteUploadTemplate.id == template_id).with_for_update()
                    .execution_options(populate_existing=True))
    if not row:
        raise HTTPException(404, '模板不存在')
    return row
