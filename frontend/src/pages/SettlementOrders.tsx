import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Check, FileText, LoaderCircle, Search } from 'lucide-react';
import {
  api,
  ApiError,
  DataState,
  datetime,
  items,
  Modal,
  nonce,
  Notice,
  Page,
  query,
  Refresh,
  ServerPager,
  Table,
  useApp,
  useData,
} from '../core';
import type { Row } from '../core';
import { usageAmountText } from '../usageAmounts';
import './SettlementOrders.css';

function Amount({ value, prefix = '' }: { value: unknown; prefix?: string }) {
  const amount = usageAmountText(value);
  return <span className="numeric">{amount == null ? '—' : `${prefix}${amount}`}</span>;
}

function OrderLines({ lines }: { lines: Row[] }) {
  const { t } = useApp();
  return (
    <Table
      rows={lines}
      pageSize={100}
      hidePagination
      empty={t('暂无结算明细', 'No settlement details')}
      scrollLabel={t('结算单明细', 'Settlement order details')}
      columns={[
        {
          label: 'ID',
          className: 'settlement-order-id',
          render: (row) => row.display_id ?? '—',
        },
        {
          label: t('分组标签', 'Group tag'),
          className: 'settlement-order-group',
          render: (row) => <strong>{row.group_tag || '—'}</strong>,
        },
        {
          label: t('分类', 'Category'),
          render: (row) =>
            t(
              row.service_name || row.category_name || '—',
              row.service_name_en || row.service_name || row.category_name || '—',
            ),
        },
        {
          label: t('当时汇率', 'Rate at settlement'),
          render: (row) => (row.discount_percent == null ? '—' : `${row.discount_percent}%`),
        },
        {
          label: t('结算消费（$）', 'Settled usage ($)'),
          className: 'settlement-order-amount',
          render: (row) => <Amount value={row.usage_amount} />,
        },
        {
          label: t('结算金额（USDT）', 'Settlement amount (USDT)'),
          className: 'settlement-order-amount',
          render: (row) => <Amount value={row.payment_amount} />,
        },
      ]}
    />
  );
}

function OrderTotals({ order }: { order: Row }) {
  const { t } = useApp();
  return (
    <div className="settlement-order-totals">
      <span>
        {t('结算消耗', 'Settled usage')}：<Amount value={order.usage_amount} /> USD
      </span>
      <strong>
        {t('总计应支付', 'Total payable')}：<Amount value={order.payment_amount} /> USDT
      </strong>
    </div>
  );
}

export function SettlementOrderModal({ id, onClose }: { id: string; onClose: () => void }) {
  const { t } = useApp();
  const { data, loading, error } = useData<Row>(`/settlement-orders/${encodeURIComponent(id)}`);
  return (
    <Modal
      wide
      className="settlement-order-modal"
      title={t('结算单详情', 'Settlement order')}
      onClose={onClose}
    >
      <div className="form-stack">
        <DataState loading={loading} error={error}>
          {data && (
            <>
              <dl className="settlement-order-summary">
                <div>
                  <dt>{t('结算单号', 'Order number')}</dt>
                  <dd>{data.number || '—'}</dd>
                </div>
                <div>
                  <dt>{t('收款方', 'Payee')}</dt>
                  <dd>{data.payee_name || '—'}</dd>
                </div>
                <div>
                  <dt>{t('创建时间', 'Created at')}</dt>
                  <dd>{datetime(data.created_at)}</dd>
                </div>
                <div>
                  <dt>{t('分组数', 'Groups')}</dt>
                  <dd>{data.line_count ?? data.lines?.length ?? '—'}</dd>
                </div>
                <div>
                  <dt>{t('结算消耗', 'Settled usage')}</dt>
                  <dd>
                    <Amount value={data.usage_amount} /> USD
                  </dd>
                </div>
                <div className="settlement-order-summary-payment">
                  <dt>{t('总计应支付', 'Total payable')}</dt>
                  <dd>
                    <Amount value={data.payment_amount} /> USDT
                  </dd>
                </div>
              </dl>
              <OrderLines lines={data.lines || []} />
            </>
          )}
        </DataState>
      </div>
    </Modal>
  );
}

type OrderSettlementProps = { groups: Row[]; onClose: () => void; onUpdated: () => void };

export function OrderSettlementDialog(props: OrderSettlementProps) {
  const { user } = useApp();
  const ids = [
    ...new Set(props.groups.map((group) => group.id).filter((id): id is string => typeof id === 'string')),
  ].sort();
  const selection = JSON.stringify(ids);
  return <OrderSettlementPreview key={`${user?.id}:${selection}`} {...props} selection={selection} />;
}

function OrderSettlementPreview({
  selection,
  onClose,
  onUpdated,
}: OrderSettlementProps & { selection: string }) {
  const { t, notify } = useApp();
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<{ revision: number; quote: Row | null; error: string }>({
    revision: -1,
    quote: null,
    error: '',
  });
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState('');
  const [refreshRequired, setRefreshRequired] = useState(false);
  const [createdId, setCreatedId] = useState('');
  const busy = useRef(false);
  const mounted = useRef(true);
  const completed = useRef(false);
  const attempt = useRef<{ hash: string; key: string } | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    let alive = true;
    const channel_ids = JSON.parse(selection) as string[];
    if (!channel_ids.length) {
      setState({ revision, quote: null, error: t('请选择需要结算的分组。', 'Select groups to settle.') });
      return;
    }
    api<Row>('/settlement-orders/preview', 'POST', { channel_ids })
      .then((quote) => {
        if (!alive) return;
        if (typeof quote.snapshot_hash !== 'string' || !quote.snapshot_hash || !Array.isArray(quote.lines)) {
          throw new Error(
            t('结算预览不完整，请刷新重试。', 'The settlement preview is incomplete. Refresh to retry.'),
          );
        }
        if (attempt.current?.hash !== quote.snapshot_hash) {
          attempt.current = { hash: quote.snapshot_hash, key: nonce() };
        }
        setState({ revision, quote, error: '' });
      })
      .catch((error: Error) => {
        if (alive) setState({ revision, quote: null, error: error.message });
      });
    return () => {
      alive = false;
    };
  }, [selection, revision]);
  const current = state.revision === revision;
  const quote = current ? state.quote : null;
  const loading = !current;
  const error = current ? state.error : '';
  const refresh = () => {
    if (busy.current || completed.current) return;
    setSubmitError('');
    setRefreshRequired(false);
    setRevision((value) => value + 1);
  };
  const close = () => {
    if (!busy.current) onClose();
  };
  const confirm = async () => {
    if (
      busy.current ||
      completed.current ||
      loading ||
      !quote?.lines?.length ||
      refreshRequired ||
      !attempt.current
    )
      return;
    busy.current = true;
    setSubmitting(true);
    setSubmitError('');
    try {
      const order = await api<Row>('/settlement-orders', 'POST', {
        channel_ids: JSON.parse(selection),
        snapshot_hash: quote.snapshot_hash,
        idempotency_key: attempt.current.key,
      });
      if (typeof order.id !== 'string' || !order.id) {
        throw new Error(
          t(
            '未收到结算单号，请重试查询本次提交结果。',
            'No order ID was returned. Retry to retrieve this submission.',
          ),
        );
      }
      completed.current = true;
      if (mounted.current) {
        setCreatedId(order.id);
        notify(t('结算单已生成', 'Settlement order created'));
        onUpdated();
      }
    } catch (error) {
      if (mounted.current) {
        setSubmitError(
          error instanceof Error
            ? error.message
            : t('结算失败，请重试。', 'Settlement failed. Please retry.'),
        );
        if (error instanceof ApiError && error.status === 409) setRefreshRequired(true);
      }
    } finally {
      busy.current = false;
      if (mounted.current) setSubmitting(false);
    }
  };
  if (createdId) return <SettlementOrderModal id={createdId} onClose={onClose} />;
  return (
    <Modal
      wide
      className="settlement-order-modal"
      title={t('确认结算', 'Confirm settlement')}
      onClose={close}
      footer={
        <>
          {quote && <OrderTotals order={quote} />}
          <button
            className="button"
            disabled={loading || !!error || submitting || refreshRequired || !quote?.lines?.length}
            onClick={confirm}
          >
            {submitting ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}
            {submitting ? t('正在结算…', 'Settling…') : t('确认结算', 'Confirm settlement')}
          </button>
        </>
      }
    >
      <div className="form-stack">
        <div className="settlement-order-toolbar">
          <Refresh onClick={refresh} loading={loading || submitting} />
          <div className="settlement-order-payee">
            <span className="muted">{t('收款方', 'Payee')}</span>
            <strong>{loading ? t('加载中…', 'Loading…') : quote?.payee_name || '—'}</strong>
          </div>
        </div>
        <DataState loading={loading} error={error}>
          {quote && <OrderLines lines={quote.lines} />}
        </DataState>
        {quote && (
          <p className="muted">
            {t(
              '确认后生成结算单并记录本次结算，系统不会发起转账。',
              'Confirmation creates a settlement record. No funds are transferred by the system.',
            )}
          </p>
        )}
        {submitError && <Notice kind="error">{submitError}</Notice>}
        {refreshRequired && (
          <Notice>
            {t(
              '请点击刷新，核对最新明细后再次确认。',
              'Refresh and review the latest details before confirming again.',
            )}
          </Notice>
        )}
      </div>
    </Modal>
  );
}

export default function SettlementOrders() {
  const [params, setParams] = useSearchParams();
  const updateParams = useCallback(
    (next: URLSearchParams) => setParams(next, { replace: true }),
    [setParams],
  );
  return <SettlementOrderHistory params={params} setParams={updateParams} />;
}

export function UserSettlementHistoryModal({ payee, onClose }: { payee: Row; onClose: () => void }) {
  const [params, setParams] = useState(() => new URLSearchParams());
  return <SettlementOrderHistory params={params} setParams={setParams} dialog={{ payee, onClose }} />;
}

function SettlementOrderHistory({
  params,
  setParams,
  dialog,
}: {
  params: URLSearchParams;
  setParams: (next: URLSearchParams) => void;
  dialog?: { payee: Row; onClose: () => void };
}) {
  const { t, user } = useApp();
  const search = params.get('search') || '';
  const accountId = dialog ? '' : params.get('account_id') || '';
  const payeeId = dialog ? dialog.payee.id : params.get('payee_id') || '';
  const rawOffset = Number(params.get('offset') || '0');
  const offset = Number.isSafeInteger(rawOffset) && rawOffset >= 0 ? rawOffset : 0;
  const limit = 50;
  const scope = JSON.stringify([user?.id, accountId, payeeId]);
  const [searchDraft, setSearchDraft] = useState(search);
  const [selected, setSelected] = useState<{ scope: string; id: string } | null>(null);
  const closeSelected = useCallback(() => setSelected(null), []);
  const { data, error, loading, refresh } = useData<Row>(
    `/settlement-orders?${query({ account_id: accountId, payee_id: payeeId, search, offset, limit })}`,
  );
  const updateParams = (values: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(values)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setParams(next);
  };
  useEffect(() => {
    setSearchDraft(search);
  }, [search, scope]);
  useEffect(() => {
    if (!loading && !error && data && offset > 0 && offset >= data.total) {
      const next = new URLSearchParams(params);
      const last = Math.max(0, Math.floor((data.total - 1) / limit) * limit);
      if (last) next.set('offset', String(last));
      else next.delete('offset');
      setParams(next);
    }
  }, [data, error, loading, offset, params, setParams]);
  const content = (
    <section className="panel settlement-orders-panel">
      <div className="panel-toolbar">
        <form
          className="settlement-order-search"
          role="search"
          onSubmit={(event) => {
            event.preventDefault();
            const nextSearch = searchDraft.trim();
            setSearchDraft(nextSearch);
            if (nextSearch === search && offset === 0) refresh();
            else updateParams({ search: nextSearch, offset: '' });
          }}
        >
          <label className="search-input">
            <input
              aria-label={t('搜索结算单', 'Search settlement orders')}
              value={searchDraft}
              placeholder={t('搜索单号或收款方…', 'Search order number or payee…')}
              onChange={(event) => setSearchDraft(event.target.value)}
            />
          </label>
          <button type="submit" className="button secondary">
            <Search size={16} />
            {t('搜索', 'Search')}
          </button>
        </form>
        {!dialog && (accountId || payeeId) && (
          <button
            className="text-button"
            onClick={() => updateParams({ account_id: '', payee_id: '', offset: '' })}
          >
            {t('清除账号筛选', 'Clear account filter')}
          </button>
        )}
        {dialog && <Refresh onClick={refresh} loading={loading} />}
      </div>
      <DataState loading={loading} error={error}>
        {!error && (
          <>
            <Table
              rows={items(data)}
              pageSize={limit}
              hidePagination
              empty={t('暂无结算单', 'No settlement orders')}
              columns={[
                {
                  label: t('订单号', 'Order number'),
                  render: (row) => <strong>{row.number || '—'}</strong>,
                },
                { label: t('收款方', 'Payee'), render: (row) => row.payee_name || '—' },
                {
                  label: t('消耗', 'Usage'),
                  className: 'settlement-order-amount',
                  render: (row) => <Amount value={row.usage_amount} prefix="$" />,
                },
                {
                  label: t('结算金额（USDT）', 'Settlement amount (USDT)'),
                  className: 'settlement-order-amount',
                  render: (row) => <Amount value={row.payment_amount} />,
                },
                { label: t('分组数', 'Groups'), render: (row) => row.line_count ?? '—' },
                { label: t('创建时间', 'Created at'), render: (row) => datetime(row.created_at) },
                {
                  label: t('操作', 'Actions'),
                  className: 'table-actions-cell',
                  render: (row) => (
                    <div className="row-actions">
                      <button onClick={() => setSelected({ scope, id: row.id })}>
                        <FileText size={14} />
                        {t('查看', 'View')}
                      </button>
                    </div>
                  ),
                },
              ]}
            />
            <ServerPager
              offset={offset}
              limit={limit}
              total={data?.total ?? 0}
              loading={loading}
              onChange={(value) => updateParams({ offset: value ? String(value) : '' })}
            />
          </>
        )}
      </DataState>
    </section>
  );
  return (
    <>
      {dialog ? (
        <Modal
          wide
          className="settlement-order-modal settlement-history-modal"
          title={t('结算记录', 'Settlement history')}
          subtitle={
            dialog.payee.nickname && dialog.payee.nickname !== dialog.payee.username
              ? `${dialog.payee.nickname} · @${dialog.payee.username}`
              : dialog.payee.username
          }
          onClose={dialog.onClose}
        >
          {content}
        </Modal>
      ) : (
        <Page
          title={t('结算历史', 'Settlement history')}
          subtitle={t(
            '查看每次结算的金额与分组明细。',
            'Review amounts and group details for each settlement.',
          )}
          actions={<Refresh onClick={refresh} loading={loading} />}
        >
          {content}
        </Page>
      )}
      {selected?.scope === scope && <SettlementOrderModal id={selected.id} onClose={closeSelected} />}
    </>
  );
}
