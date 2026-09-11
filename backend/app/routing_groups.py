"""Template group selections, serialized with NewAPI's comma-separated contract."""

MAX_ROUTING_GROUPS = 32
MAX_ROUTING_GROUP_LENGTH = 160


def routing_group_names(value: str) -> list[str]:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError('请选择目标渠道分组，分组名称合计不能超过 160 个字符')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('渠道分组不能包含换行或控制字符')
    parts = [part.strip() for part in value.split(',')]
    if any(not part for part in parts):
        raise ValueError('目标渠道分组不能为空，也不能包含空分组')
    groups = list(dict.fromkeys(parts))
    if len(groups) > MAX_ROUTING_GROUPS:
        raise ValueError(f'目标渠道分组最多选择 {MAX_ROUTING_GROUPS} 个')
    if len(','.join(groups)) > MAX_ROUTING_GROUP_LENGTH:
        raise ValueError(f'分组名称合计不能超过 {MAX_ROUTING_GROUP_LENGTH} 个字符（含分隔符）')
    return groups


def normalize_routing_groups(value: str) -> str:
    return ','.join(routing_group_names(value))
