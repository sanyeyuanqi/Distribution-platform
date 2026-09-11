"""Select the explicitly configured provider without guessing API variants."""

SUPPORTED_ADAPTERS = frozenset({'silicon-v1', 'tcp-red-v1', 'new-api-v1', 'spacex-hub-v1'})


def supports_adapter(adapter: str | None) -> bool:
    # SQLAlchemy applies the original default on INSERT; newly constructed Site
    # objects can still have None while their first verification is performed.
    return ('silicon-v1' if adapter is None else adapter) in SUPPORTED_ADAPTERS


def get_adapter(site, **kwargs):
    adapter = getattr(site, 'adapter', None)
    if adapter is None or adapter == 'silicon-v1':
        from .silicon import SiliconAdapter

        return SiliconAdapter(site, **kwargs)
    if adapter == 'tcp-red-v1':
        from .tcp_red import TcpRedAdapter

        return TcpRedAdapter(site, **kwargs)
    if adapter == 'new-api-v1':
        from .new_api import NewAPIAdapter

        return NewAPIAdapter(site, **kwargs)
    if adapter == 'spacex-hub-v1':
        from .spacex_hub import SpaceXHubAdapter

        return SpaceXHubAdapter(site, **kwargs)
    raise ValueError('站点适配器尚未实现')
