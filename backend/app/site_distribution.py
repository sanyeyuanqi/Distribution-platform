"""Local distribution readiness uses enabled templates, never a fallback group."""
from sqlalchemy import select

from .adapters import supports_adapter
from .models import Category, CredentialFormat, SiteUploadTemplate
from .site_verification import verification_state


def distribution_readiness(db, site):
    from .upload_templates import template_issues

    issues = []
    if not site or site.archived:
        issues.append('站点不存在或已归档')
    if site and not supports_adapter(site.adapter):
        issues.append('站点适配器尚未实现')
    state, error = verification_state(site)
    if state != 'verified':
        issues.append('站点验证失败：' + error['message'] if error else '站点尚未验证，请先验证站点')
    elif site.health != 'healthy':
        issues.append('站点连接状态异常，请重新验证站点')
    if state == 'verified':
        cap = site.capabilities or {}
        if cap.get('create') != 'supported' or cap.get('can_write') is not True:
            reason = cap.get('create_block_reason') if site.adapter == 'spacex-hub-v1' else None
            issues.append(reason if isinstance(reason, str) and reason else '站点没有创建渠道权限')
    rows = []
    if db is not None and site and site.id:
        rows = db.execute(select(SiteUploadTemplate, Category, CredentialFormat)
            .outerjoin(Category, Category.id == SiteUploadTemplate.category_id)
            .outerjoin(CredentialFormat, CredentialFormat.id == SiteUploadTemplate.format_id)
            .where(SiteUploadTemplate.site_id == site.id, SiteUploadTemplate.enabled.is_(True))
            .order_by(SiteUploadTemplate.created_at, SiteUploadTemplate.id)
            .execution_options(populate_existing=True)).all()
    for template, category, fmt in rows:
        try:
            problems = template_issues(template, site, category, fmt, include_availability=False)
        except (ValueError, TypeError, AttributeError):
            # Corrupt legacy configuration must block readiness without echoing
            # its raw values or preventing the site-management page from loading.
            problems = ['模板配置无效，请检查模型、分组与凭据格式']
        problems = [problem for problem in problems if problem not in issues]
        if problems:
            label = template.name or (category.family if category else '') or '未命名模板'
            issues.append(f'模板「{label}」：' + '；'.join(dict.fromkeys(problems)))
    return {'distribution_ready': not issues, 'distribution_issues': list(dict.fromkeys(issues)),
            'enabled_template_count': len(rows)}


def stop_unready_distribution(db, site, actor):
    """Call after flushing changes while holding the site's row lock."""
    state = distribution_readiness(db, site)
    if site.enabled and not state['distribution_ready']:
        from .auth import audit
        site.enabled = False
        audit(db, actor, 'site.distribution.stop', 'site', site.id,
              {'reason': 'template_or_site_unavailable', 'enabled_template_count': state['enabled_template_count']})
    return state
