import { Status, useApp } from '../core';
import type { Row } from '../core';
import Tooltip from './Tooltip';

export function ChannelUploadStatus({ row, pending = false }: { row: Row; pending?: boolean }) {
  const { t } = useApp();
  const status = pending ? 'pending' : row.upload_status;
  const result =
    status === 'succeeded'
      ? 'upload_succeeded'
      : ['pending', 'running'].includes(status)
        ? 'upload_running'
        : ['failed', 'cancelled'].includes(status)
          ? 'failed'
          : null;
  const message = pending
    ? t('上传尚未完成', 'The upload has not finished.')
    : result === 'upload_succeeded'
      ? undefined
      : row.upload_message ||
        (result === 'upload_running'
          ? t('正在上传，完成后自动更新结果', 'Uploading. The result will update automatically.')
          : t('上传结果尚未确认', 'The upload result has not been confirmed.'));
  return (
    <Tooltip content={message}>
      <span>{result ? <Status value={result} /> : <span className="muted">—</span>}</span>
    </Tooltip>
  );
}

export function ChannelRemoteStatus({ row }: { row: Row }) {
  const { t } = useApp();
  return (
    <>
      <Status
        value={
          ['enabled', 'disabled'].includes(row.remote_status)
            ? row.remote_status
            : row.remote_status === 'deleted'
              ? 'remote_deleted'
              : 'remote_status_unknown'
        }
      />
      {row.remote_status === 'disabled' && ['manual', 'automatic'].includes(row.remote_disable_reason) && (
        <small className="block muted">
          {row.remote_disable_reason === 'automatic'
            ? t('自动停用', 'Automatically disabled')
            : t('手动停用', 'Manually disabled')}
        </small>
      )}
    </>
  );
}
