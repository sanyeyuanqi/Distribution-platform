"""Independent upload outcomes and observations of a remote channel."""
from .distribution_tombstones import locally_deleted
from .remote_channel_status import remote_channel_state

UPLOAD_MESSAGES = {
    'pending': '等待上传',
    'running': '正在上传',
    'succeeded': '上传成功',
    'failed': '上传失败，请检查创建任务',
    'needs_review': '上传结果尚未确认，请先核实',
    'cancelled': '上传任务已取消',
    'unknown': '缺少可确认的上传记录',
}


def _linked_snapshot(dist):
    snapshot = getattr(dist, 'remote_snapshot', None)
    return (bool(getattr(dist, 'remote_id', None)) and isinstance(snapshot, dict)
            and type(snapshot.get('id')) in (int, str)
            and str(snapshot['id']) == str(dist.remote_id))


def observed_remote_state(dist):
    result = {'remote_status': 'unavailable', 'remote_disable_reason': None}
    if locally_deleted(dist) or not getattr(dist, 'remote_id', None):
        return result
    # Only the acknowledged remote-delete path writes this local state without
    # a local-deletion marker. A missing list entry cannot prove deletion.
    if dist.status == 'deleted':
        result['remote_status'] = 'deleted'
    elif dist.status != 'missing' and _linked_snapshot(dist):
        state = remote_channel_state(dist.remote_snapshot.get('status'))
        result.update(remote_status=state['status'], remote_disable_reason=state['disable_reason'])
    return result


def upload_state(dist, items):
    """Items are newest-first; later non-create operations never replace upload evidence."""
    latest = next((item for item in items if item.operation == 'create'), None)
    status, message, is_reupload = 'unknown', UPLOAD_MESSAGES['unknown'], False
    if latest is not None:
        is_reupload = latest.task_kind == 'reupload'
        status = {'queued': 'pending', 'retry': 'pending', 'unknown': 'needs_review',
                  'superseded': 'cancelled'}.get(latest.status, latest.status)
        if status not in UPLOAD_MESSAGES:
            status = 'unknown'
        message = UPLOAD_MESSAGES[status]
        if status in ('pending', 'running', 'failed', 'needs_review', 'cancelled') and latest.error:
            # TaskItem.error already holds the task's safe diagnostic. Never use
            # dist.error here: a later toggle/edit failure can overwrite it.
            message = latest.error
    elif (dist.status in ('enabled', 'disabled', 'unavailable') and _linked_snapshot(dist)
          and not locally_deleted(dist)):
        status, message = 'succeeded', '已确认远端渠道存在（历史记录）'
    return {'upload_status': status, 'upload_message': message, 'upload_is_reupload': is_reupload}
