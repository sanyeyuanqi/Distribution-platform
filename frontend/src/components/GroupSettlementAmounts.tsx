import { useApp } from '../core';
import type { Column, Row } from '../core';
import { usageAmountText } from '../usageAmounts';
import { settlementGroupPayable, summarizeSettlementPayables } from '../settlement-selection';
import ChannelUsageTotal from './ChannelUsageTotal';
import Tooltip from './Tooltip';
import './group-settlement-amounts.css';

export function GroupSettlementPayee({
  groups,
  loading = false,
  error = '',
}: {
  groups: Row[];
  loading?: boolean;
  error?: string;
}) {
  const { t } = useApp();
  const payees = [
    ...new Map(
      groups
        .filter((row) => row.settlement?.payee_id)
        .map((row) => [row.settlement.payee_id, row.settlement.payee_name || row.settlement.payee_id]),
    ).entries(),
  ];
  return (
    <div className="settlement-payee" aria-label={t('收款方', 'Payee')}>
      <span className="settlement-payee-label">{t('收款方', 'Payee')}</span>
      <Tooltip content={error || undefined}>
        <strong>
          {loading
            ? t('加载中…', 'Loading…')
            : error || !payees.length
              ? '—'
              : payees
                  .map(([id, name]) =>
                    payees.filter(([, otherName]) => otherName === name).length > 1
                      ? `${name} (${id})`
                      : name,
                  )
                  .join('、')}
        </strong>
      </Tooltip>
    </div>
  );
}

export function GroupPaymentTotal({
  groups,
  loading = false,
  error = '',
}: {
  groups: Row[];
  loading?: boolean;
  error?: string;
}) {
  const { t } = useApp();
  const total = summarizeSettlementPayables(
    groups.map((group) => ({
      id: group.id,
      remote_usage_total: group.remote_usage_total,
      settlement: group.settlement,
    })),
  );
  const unavailable = loading || !!error;
  return (
    <div className="settlement-payment-total" role="status" aria-live="polite">
      <span>{t('总计应支付', 'Total payable')}：</span>
      <Tooltip
        content={
          error ||
          t(
            '合计所选分组的本次结算金额。首次按总消费计算，再次结算只计算新增消耗。',
            'Sum of selected group settlement amounts. The first settlement uses total usage; later settlements use new usage only.',
          )
        }
      >
        <strong className="numeric">{unavailable ? '—' : (usageAmountText(total.amount) ?? '—')} USDT</strong>
      </Tooltip>
      {loading ? (
        <small>{t('计算中…', 'Calculating…')}</small>
      ) : error ? (
        <small>{t('加载失败', 'Load failed')}</small>
      ) : total.uncomputedCount > 0 ? (
        <small>
          {t(`${total.uncomputedCount} 个分组待计算`, `${total.uncomputedCount} groups awaiting calculation`)}
        </small>
      ) : null}
    </div>
  );
}

function Amount({ value, hint }: { value: unknown; hint: string }) {
  const text = usageAmountText(value);
  return (
    <Tooltip content={hint}>
      <span className={text === null ? 'muted' : 'numeric'}>{text ?? '—'}</span>
    </Tooltip>
  );
}

function SettlementRate({ amounts }: { amounts?: Row }) {
  const { t } = useApp();
  if (!amounts) return <span className="muted">—</span>;
  const percent = amounts.discount_percent;
  return (
    <Tooltip
      content={t(
        '当前渠道分类的结算汇率；应支付 = 汇率 × 本次结算消耗。确认后汇率随订单明细保存。',
        'Current channel-category rate; payable = rate × usage being settled. The rate is saved in the order detail on confirmation.',
      )}
    >
      <span>{percent == null ? t('未设置', 'Not set') : `${percent}%`}</span>
    </Tooltip>
  );
}

export function useGroupFinancialColumns({
  loading = false,
  error = '',
}: {
  loading?: boolean;
  error?: string;
} = {}): Column[] {
  const { t } = useApp();
  const unavailable = loading || !!error;
  const unavailableCell = (
    <Tooltip content={error || t('加载中…', 'Loading…')}>
      <span className="muted">—</span>
    </Tooltip>
  );
  return [
    {
      label: t('汇率', 'Rate'),
      render: (row) => (unavailable ? unavailableCell : <SettlementRate amounts={row.settlement} />),
    },
    {
      label: t('总消费（$）', 'Total usage ($)'),
      className: 'settlement-amount',
      render: (row) =>
        unavailable ? (
          unavailableCell
        ) : (
          <div>
            <ChannelUsageTotal channel={row} />
            {row.settlement?.previous_total && /[1-9]/.test(row.settlement.previous_total) && (
              <small className="block muted">
                {t('本次结算', 'This settlement')}：{usageAmountText(row.settlement.usage_amount) ?? '—'} USD
              </small>
            )}
          </div>
        ),
    },
    {
      label: t('应支付（USDT）', 'Payable (USDT)'),
      className: 'settlement-amount',
      render: (row) =>
        unavailable ? (
          unavailableCell
        ) : (
          <Amount
            value={settlementGroupPayable({
              id: row.id,
              remote_usage_total: row.remote_usage_total,
              settlement: row.settlement,
            })}
            hint={t(
              '应支付 = 汇率 × 本次结算消耗，例如 80% × 100 = 80 USDT。首次结算使用各站点总消费；再次结算只计算新增消耗。',
              'Payable = rate × usage being settled, e.g. 80% × 100 = 80 USDT. The first settlement uses total usage from all sites; later settlements use new usage only.',
            )}
          />
        ),
    },
  ];
}
