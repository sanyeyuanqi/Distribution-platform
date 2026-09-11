import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, LoaderCircle } from 'lucide-react';
import {
  api,
  DataState,
  datetime,
  Empty,
  items,
  Modal,
  Notice,
  Refresh,
  shortId,
  Table,
  useApp,
  useData,
} from '../core';
import type { Row } from '../core';
import './discount-modal.css';

function timestamp(value: unknown): bigint | null {
  if (typeof value !== 'string' || !value) return null;
  const utc = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const milliseconds = Date.parse(utc);
  if (!Number.isFinite(milliseconds)) return null;
  const fraction = value.match(/\.(\d+)/)?.[1] || '';
  return BigInt(milliseconds) * 1000n + BigInt(fraction.padEnd(6, '0').slice(3, 6) || '0');
}

function compareVersions(left: Row, right: Row): number {
  for (const field of ['effective_at', 'created_at']) {
    const a = timestamp(left[field]) ?? -1n;
    const b = timestamp(right[field]) ?? -1n;
    if (a !== b) return a < b ? -1 : 1;
  }
  return String(left.id) < String(right.id) ? -1 : String(left.id) > String(right.id) ? 1 : 0;
}

function percentText(value: unknown, lang: string): string {
  const percent = Number(value);
  return value !== null && value !== undefined && Number.isFinite(percent)
    ? `${percent.toLocaleString(lang === 'en' ? 'en-US' : 'zh-CN', { maximumFractionDigits: 6 })}%`
    : '—';
}

function validPercent(value: string): boolean {
  return /^(?:\d+(?:\.\d{0,6})?|\.\d{1,6})$/.test(value) && Number(value) >= 0 && Number(value) <= 100;
}

function DiscountCategoryCard({
  category,
  payeeId,
  current,
  future,
  snapshot,
  highlighted,
  disabled,
  onSaved,
}: {
  category: Row;
  payeeId: string;
  current: Row | null;
  future: Row[];
  snapshot: unknown;
  highlighted: boolean;
  disabled: boolean;
  onSaved: () => void;
}) {
  const { t, lang, notify } = useApp();
  const [percent, setPercent] = useState(current ? String(current.percent) : '');
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const saving = useRef(false);
  const dirty = useRef(false);
  const confirmed = useRef(current);
  useEffect(() => {
    if (!dirty.current && !saving.current) {
      confirmed.current = current;
      setPercent(current ? String(current.percent) : '');
    }
  }, [current?.id, current?.percent, category.override?.id, snapshot]);
  const scheduled = future[0];
  const valid = validPercent(percent);
  const fieldError =
    (percent || dirty.current) && !valid
      ? t(
          '请输入 0–100 的折扣，最多 6 位小数。',
          'Enter a discount from 0 to 100, with at most 6 decimal places.',
        )
      : '';
  const submit = async (value: string, inherit = false) => {
    if (disabled || saving.current || (!inherit && (!dirty.current || !validPercent(value)))) return;
    if (
      !inherit &&
      !category.inherited &&
      confirmed.current &&
      Number(value) === Number(confirmed.current.percent)
    ) {
      dirty.current = false;
      setPercent(String(confirmed.current.percent));
      setError('');
      return;
    }
    saving.current = true;
    setBusy(true);
    setSaved(false);
    setError('');
    try {
      const version = await api<Row>('/discounts', 'POST', {
        payee_id: payeeId,
        category_id: category.id,
        service_variant: category.service_variant,
        ...(inherit ? { inherits_category: true } : { percent: value }),
      });
      confirmed.current = inherit ? category.default_current || null : version;
      setPercent(confirmed.current ? String(confirmed.current.percent) : '');
      dirty.current = false;
      setSaved(true);
      onSaved();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : t('保存失败。', 'Save failed.');
      setError(
        t(
          `${message} 请重新聚焦输入框，再移开以重试。`,
          `${message} Focus and leave the input again to retry.`,
        ),
      );
      notify(
        t(`${category.name} 折扣保存失败：${message}`, `${category.name} discount save failed: ${message}`),
        true,
      );
    } finally {
      saving.current = false;
      setBusy(false);
    }
  };
  return (
    <div
      className={`discount-category-card${highlighted ? ' highlighted' : ''}`}
      data-category-id={category.id}
      data-service-variant={category.service_variant}
      role="group"
      aria-label={t(`${category.name} 分类折扣`, `${category.name} category discount`)}
    >
      <div className="discount-card-heading">
        <div className="discount-category-name">
          <strong>{category.name}</strong>
        </div>
        <span className="discount-save-state" role="status">
          {busy ? (
            <>
              <LoaderCircle size={13} className="spin" />
              {t('保存中…', 'Saving…')}
            </>
          ) : saved ? (
            <>
              <Check size={13} />
              {t('已保存', 'Saved')}
            </>
          ) : null}
        </span>
      </div>
      <label className="discount-percent-field">
        <span className="discount-input-wrap">
          <input
            type="number"
            min={0}
            max={100}
            step="0.000001"
            required
            aria-label={t(`${category.name} 结算折扣（%）`, `${category.name} Settlement discount (%)`)}
            aria-invalid={!!fieldError}
            value={percent === 'invalid' ? '' : percent}
            placeholder={t('未设置', 'Not configured')}
            disabled={disabled || busy}
            onChange={(event) => {
              dirty.current = true;
              setSaved(false);
              setPercent(event.currentTarget.validity.badInput ? 'invalid' : event.target.value);
              setError('');
            }}
            onBlur={(event) =>
              void submit(event.currentTarget.validity.badInput ? 'invalid' : event.currentTarget.value)
            }
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                event.currentTarget.blur();
              }
            }}
          />
          <span className="discount-input-suffix" aria-hidden="true">
            %
          </span>
        </span>
      </label>
      {category.override && !category.override.inherits_category && (
        <button
          type="button"
          className="discount-inherit-button"
          disabled={disabled || busy}
          onPointerDown={(event) => event.preventDefault()}
          onClick={() => void submit('', true)}
        >
          {t('恢复大类默认', 'Use category default')}
        </button>
      )}
      {scheduled && (
        <small className="discount-future">
          {t('将生效', 'Scheduled')}: {percentText(scheduled.current?.percent, lang)} ·{' '}
          {datetime(scheduled.effective_at)}
          {future.length > 1 && (
            <span>
              {t(`另有 ${future.length - 1} 个未来版本`, `${future.length - 1} more scheduled versions`)}
            </span>
          )}
        </small>
      )}
      {fieldError && (
        <small className="discount-row-error danger-text" role="alert">
          {fieldError}
        </small>
      )}
      {error && (
        <p className="discount-row-error danger-text" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export default function DiscountModal({
  payee,
  category,
  onClose,
  onSaved,
}: {
  payee: Row;
  category?: Row | null;
  onClose: () => void;
  onSaved?: () => void;
}) {
  const { t, lang } = useApp();
  const categories = useData('/categories');
  const history = useData(`/discounts?payee_id=${payee.id}`);
  const editor = useRef<HTMLDivElement>(null);
  const closeHandler = useRef(onClose);
  closeHandler.current = onClose;
  const versions = items(history.data).slice().sort(compareVersions);
  const services: Row[] = Array.isArray(history.data?.services) ? history.data.services : [];
  const loaded = !!categories.data && !!history.data;
  const unavailable = !!categories.error || !!history.error || !loaded;
  const close = useCallback(() => {
    const focused = document.activeElement;
    if (focused instanceof HTMLInputElement && editor.current?.contains(focused)) focused.blur();
    closeHandler.current();
  }, []);
  const refresh = () => {
    categories.refresh();
    history.refresh();
  };
  const onVersionSaved = () => {
    history.refresh();
    onSaved?.();
  };
  useEffect(() => {
    const nowStamp = timestamp(history.data?.as_of);
    if (nowStamp === null) return;
    const next = services
      .flatMap((row) => (row.future || []).map((event: Row) => timestamp(event.effective_at)))
      .filter((value: bigint | null): value is bigint => value !== null && value > nowStamp)
      .sort((a: bigint, b: bigint) => (a < b ? -1 : a > b ? 1 : 0))[0];
    if (next === undefined || next === null) return;
    const delay = Math.min(Number((next - nowStamp) / 1000n) + 1, 2147483647);
    const timer = window.setTimeout(() => history.refresh(), Math.max(delay, 50));
    return () => window.clearTimeout(timer);
  }, [history.data]);
  return (
    <Modal
      wide
      className="discount-modal"
      title={t('分类结算折扣', 'Category settlement discount')}
      subtitle={payee.nickname || payee.username}
      onClose={close}
    >
      <div className="discount-editor" ref={editor}>
        <div className="discount-editor-toolbar">
          <div>
            <h3>
              {t('全部渠道分类', 'All channel categories')}
              {loaded && <span className="discount-category-count">{services.length}</span>}
            </h3>
            <p>
              {t(
                '修改后移开焦点，自动保存并立即生效。',
                'Leave an edited input to save and apply the discount.',
              )}
            </p>
          </div>
          <Refresh onClick={refresh} loading={categories.loading || history.loading} />
        </div>
        {loaded && (categories.error || history.error) && (
          <Notice kind="error">{categories.error || history.error}</Notice>
        )}
        <DataState
          loading={!loaded && (categories.loading || history.loading)}
          error={!loaded ? categories.error || history.error : ''}
        >
          {loaded && (
            <div className="discount-category-grid">
              {services.map((service) => {
                const item: Row = {
                  ...service,
                  id: service.category_id,
                  name: lang === 'en' ? service.label_en || service.label : service.label,
                };
                return (
                  <DiscountCategoryCard
                    key={`${payee.id}:${item.id}:${item.service_variant}`}
                    category={item}
                    payeeId={payee.id}
                    current={service.current || null}
                    future={service.future || []}
                    snapshot={history.data}
                    highlighted={category?.category_id === item.id}
                    disabled={unavailable}
                    onSaved={onVersionSaved}
                  />
                );
              })}
              {!services.length && <Empty text={t('暂无渠道分类', 'No channel categories')} />}
            </div>
          )}
        </DataState>
        {loaded && (
          <details className="discount-history">
            <summary>
              {t('折扣修改记录', 'Discount history')}{' '}
              <span className="discount-history-count">{versions.length}</span>
            </summary>
            <Table
              rows={versions.slice().reverse()}
              columns={[
                {
                  label: t('版本 ID', 'Version ID'),
                  render: (row) => <span className="mono">{shortId(row.id)}</span>,
                },
                {
                  label: t('分类', 'Category'),
                  render: (row) => {
                    const match = services.find(
                      (item) =>
                        item.category_id === row.category_id && item.service_variant === row.service_variant,
                    );
                    if (match) return lang === 'en' ? match.label_en || match.label : match.label;
                    const name =
                      items(categories.data).find((item) => item.id === row.category_id)?.name ||
                      shortId(row.category_id);
                    return row.service_variant == null
                      ? `${name} · ${t('大类默认', 'Category default')}`
                      : name;
                  },
                },
                {
                  label: t('折扣', 'Discount'),
                  render: (row) => (
                    <strong>
                      {row.inherits_category
                        ? t('恢复大类默认', 'Use category default')
                        : percentText(row.percent, lang)}
                    </strong>
                  ),
                },
                { label: t('生效时间', 'Effective'), render: (row) => datetime(row.effective_at) },
                { label: t('创建时间', 'Created'), render: (row) => datetime(row.created_at) },
              ]}
            />
          </details>
        )}
      </div>
    </Modal>
  );
}
