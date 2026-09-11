"""Remote identities span all registrations of the same deployment."""
import hashlib
import json

from sqlalchemy import and_, select
from sqlalchemy.orm import aliased

from .models import Site


def same_deployment_site_ids(site_id):
    """Include archived registrations without changing their historical UUIDs."""
    source = aliased(Site)
    return select(Site.id).join(source, and_(
        Site.base_url == source.base_url,
        Site.seller_user_id == source.seller_user_id,
        Site.adapter == source.adapter,
    )).where(source.id == site_id)


def remote_identity_lock_key(site, namespace, target):
    # JSON avoids ambiguous separators, and connection identity remains stable
    # when a registration is archived or another Site UUID is added.
    identity = json.dumps([namespace, site.base_url, str(site.seller_user_id), site.adapter, str(target)],
                          ensure_ascii=False, separators=(',', ':'))
    return int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], 'big', signed=True)
