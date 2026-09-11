"""Per-distribution observations, kept separate from immutable billing facts."""
from .db import utcnow
from .distribution_tombstones import locally_deleted

MONITOR_OPERATIONS = frozenset({'test', 'sync_usage'})
_NAMES = {'test': 'connectivity_test', 'sync_usage': 'usage_sync'}


def monitoring_snapshot(dist):
    values = (dist.remote_snapshot or {}).get('_monitoring')
    return dict(values) if isinstance(values, dict) else {}


def observation_json(dist, operation, *, conversion=None):
    name = _NAMES[operation]
    current = monitoring_snapshot(dist).get(name)
    initial = {'status': 'not_tested' if operation == 'test' else 'not_synced',
               'task_id': None, 'message': ''}
    initial.update({'model': None, 'tested_at': None, 'test_all': False, 'test_content': None,
                    'model_results': [], 'completed_count': 0, 'total_count': 0, 'current_model': None,
                    'total_key_count': 0, 'completed_key_count': 0, 'passed_key_count': 0, 'failed_key_count': 0,
                    'current_key_index': None} if operation == 'test'
                   else {'synced_at': None, 'remote_usage': None})
    result = {**initial, **current} if isinstance(current, dict) else initial
    if operation == 'sync_usage' and isinstance(result.get('remote_usage'), dict):
        from .adapters.channel_observation import normalize_usage_observation
        result['remote_usage'] = normalize_usage_observation(result['remote_usage'], conversion=conversion)
    return result


def test_progress_projection(snapshot, status, result):
    progress = result if 'model_results' in result else snapshot.get('test_progress', {})
    terminal = status in ('succeeded', 'failed', 'needs_review', 'cancelled')
    # Copy children only when projecting terminal states; never mutate durable
    # checkpoints, nor synthesize per-Key results from historical aggregates.
    rows = [{**row, 'key_results': [dict(key) for key in row.get('key_results', [])] if terminal
             else row.get('key_results', [])} for row in progress.get('model_results', [])]
    if terminal:
        for row in rows:
            for key in row['key_results']:
                if key['status'] == 'running':
                    key['status'] = status if status != 'succeeded' else 'needs_review'
                    key['message'] = {
                        'needs_review': '本 Key 测试结果尚未确认，不会自动重复测试',
                        'cancelled': '测试已停止，本 Key 未完成',
                        'failed': '本 Key 测试未完成',
                    }[key['status']]
                elif key['status'] == 'pending':
                    key['status'], key['message'] = 'not_tested', '未开始测试'
            if row['status'] == 'running':
                row['status'] = status if status != 'succeeded' else 'needs_review'
                if not row.get('failed_count'):
                    # Earlier successful Keys are not a response for the
                    # interrupted Key whose outcome is still unknown.
                    row['provider_message'] = None
                row['message'] = row.get('message') or {
                    'needs_review': '本模型测试结果尚未确认，不会自动重复测试',
                    'cancelled': '测试已停止，本模型未完成',
                    'failed': '本模型测试未完成',
                }.get(row['status'], '')
            elif row['status'] == 'pending':
                row['status'], row['message'] = 'not_tested', '未开始测试'
    return {**progress, 'model_results': rows, 'current_model': None if terminal else progress.get('current_model'),
            'current_key_index': None if terminal else progress.get('current_key_index')}


def test_item_observation(item, *, status=None):
    """Public progress only; never expose the frozen credential/source plan."""
    result = item.snapshot.get('operation_result') or {}
    status = status or item.status
    progress = test_progress_projection(item.snapshot, status, result)
    current = {'status': status, 'task_id': item.task_id, 'message': item.error or result.get('message', ''),
               'model': item.snapshot.get('test_model'), 'tested_at': result.get('tested_at'),
               'test_all': item.snapshot.get('test_all', False), 'test_content': item.snapshot.get('test_content'),
               'model_results': progress.get('model_results', []), 'completed_count': progress.get('completed_count', 0),
               'total_count': progress.get('total_count', 1 if item.snapshot.get('test_model') else 0),
               'current_model': progress.get('current_model'),
               'total_key_count': progress.get('total_key_count', 0),
               'completed_key_count': progress.get('completed_key_count', 0),
               'passed_key_count': progress.get('passed_key_count', 0),
               'failed_key_count': progress.get('failed_key_count', 0),
               'current_key_index': progress.get('current_key_index')}
    for field in ('success', 'latency_ms', 'source', 'tested_count', 'passed_count', 'failed_count'):
        source = result if field == 'success' else {**progress, **result}
        if field in source:
            current[field] = source[field]
    return current


def record_observation(dist, item, *, status=None, replace=False):
    if item.operation == 'test' and item.snapshot.get('test_scope') == 'channel':
        return  # Channel-wide results belong to the TaskItem, not to one source distribution.
    if item.operation not in MONITOR_OPERATIONS or (locally_deleted(dist) and item.operation != 'test'):
        return
    name = _NAMES[item.operation]
    current = observation_json(dist, item.operation)
    if current['task_id'] not in (None, item.task_id) and not replace:
        return
    result = item.snapshot.get('operation_result') or {}
    current.update(status=status or item.status, task_id=item.task_id,
                   message=item.error or result.get('message', ''))
    if item.operation == 'test':
        current = test_item_observation(item, status=current['status'])
        dist.test_status = current['status']
        if result.get('tested_at'):
            dist.tested_at = utcnow()
    elif 'remote_usage' in result:
        current.update(remote_usage=result['remote_usage'], synced_at=result.get('synced_at'))
        dist.usage_status = 'remote_snapshot'
    observations = {**monitoring_snapshot(dist), name: current}
    dist.remote_snapshot = {**(dist.remote_snapshot or {}), '_monitoring': observations}


def record_sync_observation(dist, task_id, remote_usage):
    """Refresh visible usage during a site sync without hiding a queued row job."""
    if locally_deleted(dist):
        return
    current = observation_json(dist, 'sync_usage')
    if current['status'] in ('pending', 'running'):
        return
    if current['task_id'] is None:
        current.update(status='succeeded', task_id=task_id, message='已同步远端原始消耗状态')
    current.update(synced_at=utcnow().isoformat() + 'Z', remote_usage=remote_usage)
    dist.remote_snapshot = {**(dist.remote_snapshot or {}), '_monitoring': {
        **monitoring_snapshot(dist), 'usage_sync': current}}
    dist.usage_status = 'remote_snapshot'
