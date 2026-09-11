import { useApp } from '../core';
import type { Row } from '../core';
import Tooltip from './Tooltip';

export function GroupSettlementStatus({
  row,
  loading,
  error,
}: {
  row?: Row;
  loading?: boolean;
  error?: string;
}) {
  const { t } = useApp();
  const settled = row?.status === 'settled';
  const label: [string, string] = !row?.status
    ? ['—', '—']
    : settled
      ? ['已结算', 'Settled']
      : ['未结算', 'Not settled'];
  return (
    <Tooltip content={error || row?.reason || undefined}>
      <span className={`tag ${settled ? 'green' : ''}`}>
        {loading ? t('加载中…', 'Loading…') : error ? t('加载失败', 'Load failed') : t(...label)}
      </span>
    </Tooltip>
  );
}
