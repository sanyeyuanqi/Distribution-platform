"""Explicit official releases reviewed against upstream management source code.

The manifest is bundled with the application. Neither network discovery nor a
server-supplied permission object can opt an unknown version into this contract.
"""
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


@dataclass(frozen=True)
class ReleaseContract:
    version: str
    published_at: str
    permission_model: str
    channel_types: frozenset[int]

    @property
    def uses_channel_permissions(self):
        return self.permission_model == 'channel_rbac'


_manifest = json.loads(Path(__file__).with_name('newapi_releases.json').read_text(encoding='utf-8'))
REVIEWED_AT = _manifest['reviewed_at']
WINDOW_START = _manifest['window_start']
WINDOW_END = _manifest['window_end']
RELEASES = MappingProxyType({
    entry['version']: ReleaseContract(
        version=entry['version'], published_at=entry['published_at'],
        permission_model=entry['permission_model'], channel_types=frozenset(entry['channel_types']),
    ) for entry in _manifest['releases']
})
if len(RELEASES) != len(_manifest['releases']) or any(
    release.permission_model not in {'role_admin', 'channel_rbac'} for release in RELEASES.values()
):
    raise RuntimeError('Invalid bundled NewAPI compatibility manifest')
del _manifest
