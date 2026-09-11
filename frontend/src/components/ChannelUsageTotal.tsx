import { useApp } from '../core';
import type { Row } from '../core';
import { usageAmountText } from '../usageAmounts';
import Tooltip from './Tooltip';

export default function ChannelUsageTotal({
  channel,
  singleDistribution = false,
}: {
  channel: Row;
  singleDistribution?: boolean;
}) {
  const { t } = useApp();
  const total = channel.remote_usage_total;
  const text = total?.unit === 'USD' ? usageAmountText(total.amount) : null;
  if (text === null)
    return (
      <Tooltip
        content={t('暂无可换算为美元的远端消耗数据', 'No remote usage amount in USD is available yet')}
      >
        <span className="muted">—</span>
      </Tooltip>
    );
  const partial = total.covered < total.total;
  return (
    <Tooltip
      content={
        singleDistribution
          ? t(
              '此站点渠道最近同步的累计消耗金额，按站点额度比例换算为美元。',
              'Latest cumulative usage from this remote channel, converted to USD using the site’s quota rate.',
            )
          : t(
              '汇总各平台侧渠道最近同步的累计消耗金额，按各站点额度比例换算为美元；未同步或无法换算的渠道不计入。',
              'Latest cumulative usage from remote channels, converted to USD using each site’s quota rate. Channels without usable amounts are excluded.',
            )
      }
    >
      <div>
        <span className="block numeric">${text} USD</span>
        {partial && (
          <small className="block muted">
            {t(`已同步 ${total.covered}/${total.total}`, `Synced ${total.covered}/${total.total}`)}
          </small>
        )}
      </div>
    </Tooltip>
  );
}
