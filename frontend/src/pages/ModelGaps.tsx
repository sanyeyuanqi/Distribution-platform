import { useEffect, useRef } from 'react';
import { ChevronDown } from 'lucide-react';
import { DataState, datetime, items, Page, Refresh, Table, useApp, useData } from '../core';
import type { Row } from '../core';
import './model-gaps.css';

function integerValue(value: unknown): string | null {
  if (typeof value === 'number') return Number.isSafeInteger(value) && value >= 0 ? String(value) : null;
  return typeof value === 'string' && /^\d+$/.test(value) ? value.replace(/^0+(?=\d)/, '') : null;
}

function integerText(value: unknown): string {
  return integerValue(value)?.replace(/\B(?=(\d{3})+(?!\d))/g, ',') ?? '—';
}

function compareGap(left: Row, right: Row, unit: 'rpm' | 'tpm'): number {
  const a = left[`required_${unit}`] == null ? null : integerValue(left[`gap_${unit}`]);
  const b = right[`required_${unit}`] == null ? null : integerValue(right[`gap_${unit}`]);
  if (a === null || b === null) return a === b ? 0 : a === null ? 1 : -1;
  return BigInt(a) === BigInt(b) ? 0 : BigInt(a) > BigInt(b) ? -1 : 1;
}

function GapAmount({ row, unit }: { row: Row; unit: 'RPM' | 'TPM' }) {
  const { t } = useApp();
  const suffix = unit.toLowerCase();
  const required = row[`required_${suffix}`];
  const gap = row[`gap_${suffix}`];
  const unknown = row[`unknown_${suffix}_channels`];
  const unset = required === null || required === undefined;
  return (
    <details className="model-gap-amount">
      <summary aria-label={`${row.model} ${unit} ${t('计算明细', 'breakdown')}`}>
        <span className="model-gap-value">{integerText(gap)}</span>
        {unset && <small className="muted">{t('未设置', 'Not set')}</small>}
        <ChevronDown size={13} aria-hidden="true" />
      </summary>
      <div className="model-gap-breakdown">
        <dl>
          <div>
            <dt>{t('需求合计', 'Total demand')}</dt>
            <dd>
              {integerText(required)} {unit}
            </dd>
          </div>
          <div>
            <dt>{t('已申报总量', 'Declared total')}</dt>
            <dd>
              {integerText(row[`supplied_${suffix}`])} {unit}
            </dd>
          </div>
        </dl>
        {unset ? (
          <p>{t('此模型尚未设置此项需求。', 'No demand is configured for this unit.')}</p>
        ) : (
          <p>
            {t(
              '缺口为需求合计减去已申报总量，最低为 0。',
              'The gap is total demand minus the declared total, with a minimum of 0.',
            )}
          </p>
        )}
        {integerValue(unknown) !== null && BigInt(integerValue(unknown)!) > 0n && (
          <p>
            {t(
              `部分渠道未填写配额，按已知总量计算。${integerText(unknown)} 个渠道未填写 ${unit}。`,
              `Some channels have no quota declared; calculations use known totals. ${integerText(unknown)} channels have no ${unit} declaration.`,
            )}
          </p>
        )}
      </div>
    </details>
  );
}

function ModelGapsView() {
  const { user, t } = useApp();
  const { data, error, loading, refresh } = useData('/model-gaps');
  const loadingRef = useRef(loading);
  loadingRef.current = loading;
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | undefined;
    const refreshIfIdle = () => {
      if (!loadingRef.current) refresh();
    };
    const schedule = () => {
      if (timer) clearInterval(timer);
      if (document.visibilityState === 'visible') timer = setInterval(refreshIfIdle, 60_000);
    };
    const visible = () => {
      schedule();
      if (document.visibilityState === 'visible') refreshIfIdle();
    };
    schedule();
    document.addEventListener('visibilitychange', visible);
    return () => {
      if (timer) clearInterval(timer);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [refresh]);
  const typeName = (row: Row) =>
    t(
      row.type_label || row.category_name || '—',
      row.type_label_en || row.type_label || row.category_name || '—',
    );
  const rows = items(data)
    .filter((row) => row.required_rpm != null || row.required_tpm != null)
    .sort(
      (a, b) =>
        compareGap(a, b, 'rpm') ||
        compareGap(a, b, 'tpm') ||
        typeName(a).localeCompare(typeName(b)) ||
        String(a.model || '').localeCompare(String(b.model || '')),
    );
  return (
    <Page
      title={t('模型缺口', 'Model gaps')}
      subtitle={t(
        '全平台模型 RPM / TPM 需求与已申报总量。',
        'Platform-wide model RPM / TPM demand and declared totals.',
      )}
      actions={<Refresh onClick={refresh} loading={loading} />}
    >
      <section className="panel model-gaps-panel">
        <div className="model-gaps-updated">
          <span>{t('每分钟自动刷新', 'Refreshes every minute')}</span>
          {data?.updated_at && (
            <span>
              {t('更新于', 'Updated')} {datetime(data.updated_at)}
            </span>
          )}
        </div>
        <DataState loading={(loading && !data) || (!data && !error)} error={error}>
          {rows.length ? (
            <Table
              rows={rows}
              columns={[
                {
                  label: t('类型', 'Type'),
                  className: 'model-gaps-type',
                  render: typeName,
                },
                {
                  label: t('模型名', 'Model'),
                  className: 'model-gaps-model',
                  render: (row) => <span className="mono">{row.model}</span>,
                },
                {
                  label: rows.some((row) => row.rpm_gap_estimated === true)
                    ? t('缺口 RPM（估算）', 'RPM gap (Estimated)')
                    : t('缺口 RPM', 'RPM gap'),
                  className: 'model-gaps-number',
                  render: (row) => <GapAmount row={row} unit="RPM" />,
                },
                {
                  label: rows.some((row) => row.tpm_gap_estimated === true)
                    ? t('缺口 TPM（估算）', 'TPM gap (Estimated)')
                    : t('缺口 TPM', 'TPM gap'),
                  className: 'model-gaps-number',
                  render: (row) => <GapAmount row={row} unit="TPM" />,
                },
              ]}
            />
          ) : (
            !error && (
              <div className="model-gaps-empty">
                <h3>{t('暂无模型需求数据', 'No model demand data yet')}</h3>
                <p>
                  {user?.role === 'superadmin'
                    ? t(
                        '请在分发模板中填写各模型的 RPM / TPM 需求。',
                        'Set model RPM / TPM demand in distribution templates.',
                      )
                    : t(
                        '请联系超级管理员在分发模板中设置模型需求。',
                        'Ask a superadministrator to set model demand in distribution templates.',
                      )}
                </p>
              </div>
            )
          )}
        </DataState>
      </section>
    </Page>
  );
}

export default function ModelGaps() {
  const { user } = useApp();
  return <ModelGapsView key={user?.id || 'anonymous'} />;
}
