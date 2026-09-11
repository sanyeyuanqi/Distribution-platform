import { useApp } from '../core';
import type { Row } from '../core';
import Tooltip from './Tooltip';

export default function SettlementGroupState({ state }: { state?: Row }) {
  const { t } = useApp();
  const labels: Record<string, [string, string]> = {
    automatic_disabled: ['已自动禁用', 'Automatically disabled'],
    enabled: ['全部启用', 'All enabled'],
    manual_disabled: ['全部手动禁用', 'All manually disabled'],
    mixed: ['状态混合', 'Mixed states'],
    unavailable: ['待同步', 'Awaiting sync'],
    no_channels: ['无远端渠道', 'No remote channels'],
  };
  const label = labels[state?.status] || ['—', '—'];
  return (
    <Tooltip content={state?.reason || undefined}>
      <span
        className={`tag ${state?.status === 'enabled' ? 'green' : ['automatic_disabled', 'manual_disabled'].includes(state?.status) ? 'blue' : ''}`}
      >
        {t(...label)}
      </span>
    </Tooltip>
  );
}
