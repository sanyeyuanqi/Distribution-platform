import { useEffect, useState } from 'react';
import { CircleDollarSign, Search } from 'lucide-react';
import {
  DataState,
  datetime,
  items,
  Modal,
  query,
  Refresh,
  ServerPager,
  Table,
  useApp,
  useData,
} from '../core';
import type { Row } from '../core';
import { GroupPaymentTotal, GroupSettlementPayee, useGroupFinancialColumns } from './GroupSettlementAmounts';
import SettlementGroupState from './SettlementGroupState';
import { GroupSettlementStatus } from './GroupSettlementStatus';
import { OrderSettlementDialog } from '../pages/SettlementOrders';

export default function AccountSettlementModal({
  account,
  onClose,
  onUpdated,
}: {
  account: Row;
  onClose: () => void;
  onUpdated: () => void;
}) {
  const { t } = useApp();
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const [reviewGroups, setReviewGroups] = useState<Row[] | null>(null);
  const { data, loading, error, refresh } = useData(
    `/settlements/groups?${query({ account_id: account.id, search, offset, limit: 50 })}`,
  );
  const financialColumns = useGroupFinancialColumns({ loading, error });
  const rows = items(data);
  const settlementState = (row: Row) => row.settlement;
  const eligible = rows.filter((r) => settlementState(r)?.can_manage && settlementState(r)?.can_settle);
  const selectedGroups = eligible.filter((row) => selected.includes(row.id));
  const refreshAll = () => {
    refresh();
    onUpdated();
  };
  useEffect(() => {
    setSelected([]);
  }, [data, search, offset]);
  useEffect(() => {
    if (!loading && !error && data && offset > 0 && offset >= data.total) {
      setOffset(Math.max(0, Math.floor((data.total - 1) / 50) * 50));
    }
  }, [data, loading, error, offset]);
  if (reviewGroups)
    return (
      <OrderSettlementDialog
        groups={reviewGroups.map(
          (group) => rows.find((row) => row.id === group.id) || { ...group, remote_usage_total: undefined },
        )}
        onClose={() => {
          setReviewGroups(null);
          refreshAll();
        }}
        onUpdated={refreshAll}
      />
    );
  return (
    <Modal
      wide
      className="settlement-groups-modal"
      title={t('用户结算', 'Account settlement')}
      subtitle={`${account.nickname || account.username} · @${account.username}`}
      onClose={onClose}
      footer={
        <>
          <div className="settlement-footer-summary">
            <span className="muted">
              {t(
                `已选 ${selectedGroups.length} 个未结算分组`,
                `${selectedGroups.length} unbilled groups selected`,
              )}
            </span>
            <GroupPaymentTotal groups={selectedGroups} loading={loading} error={error} />
          </div>
          <button
            className="button"
            disabled={loading || !!error || !selectedGroups.length}
            onClick={() => setReviewGroups(selectedGroups)}
          >
            <CircleDollarSign size={16} />
            {t('结算所选分组', 'Settle selected groups')}
          </button>
        </>
      }
    >
      <div className="form-stack">
        <div className="panel-toolbar channel-list-toolbar">
          <label className="search-input">
            <Search size={16} />
            <input
              aria-label={t('搜索结算分组', 'Search settlement groups')}
              value={search}
              placeholder={t('搜索分组、编号或上传用户…', 'Search group, ID or uploader…')}
              onChange={(e) => {
                setSearch(e.target.value);
                setOffset(0);
                setSelected([]);
              }}
            />
          </label>
          <button
            className="button secondary"
            disabled={loading || !!error || !eligible.length}
            onClick={() => setSelected(eligible.map((r) => r.id))}
          >
            {t('勾选本页未结算', 'Select unbilled groups on this page')}
          </button>
          <Refresh onClick={refresh} loading={loading} />
          <GroupSettlementPayee groups={rows} loading={loading} error={error} />
        </div>
        <DataState loading={loading} error={error}>
          <Table
            rows={rows}
            hidePagination
            pageSize={50}
            empty={t('该账号暂无分组', 'This account has no groups')}
            rowClassName={(r) =>
              !loading && !error && settlementState(r)?.status === 'settled'
                ? 'settlement-group-settled'
                : undefined
            }
            columns={[
              {
                label: (
                  <input
                    type="checkbox"
                    aria-label={t('勾选本页可结算分组', 'Select eligible groups on this page')}
                    disabled={loading || !eligible.length}
                    checked={eligible.length > 0 && eligible.every((r) => selected.includes(r.id))}
                    onChange={(e) => setSelected(e.target.checked ? eligible.map((r) => r.id) : [])}
                  />
                ),
                render: (r) => (
                  <input
                    type="checkbox"
                    aria-label={t(`勾选结算分组 ${r.display_id}`, `Select settlement group ${r.display_id}`)}
                    disabled={
                      loading || !!error || !settlementState(r)?.can_manage || !settlementState(r)?.can_settle
                    }
                    checked={selected.includes(r.id)}
                    onChange={(e) =>
                      setSelected((ids) =>
                        e.target.checked ? [...ids, r.id] : ids.filter((id) => id !== r.id),
                      )
                    }
                  />
                ),
              },
              {
                label: t('分组标签', 'Group tag'),
                className: 'settlement-group-tag',
                render: (r) => (
                  <strong>
                    #{r.display_id} {r.group_tag}
                  </strong>
                ),
              },
              {
                label: t('分组类型', 'Group type'),
                render: (r) =>
                  t(
                    r.service_name || r.category_name || '—',
                    r.service_name_en || r.service_name || r.category_name || '—',
                  ),
              },
              {
                label: t('分组状态', 'Group status'),
                render: (r) => <SettlementGroupState state={r.settlement?.group_state} />,
              },
              ...financialColumns,
              {
                label: t('结算状态', 'Settlement status'),
                render: (r) => (
                  <GroupSettlementStatus row={settlementState(r)} loading={loading} error={error} />
                ),
              },
              { label: t('创建时间', 'Created'), render: (r) => datetime(r.created_at) },
              {
                label: t('操作', 'Actions'),
                render: (r) => (
                  <button className="text-button" onClick={() => setReviewGroups([r])}>
                    {t('查看', 'View')}
                  </button>
                ),
              },
            ]}
          />
          <ServerPager
            offset={offset}
            limit={50}
            total={data?.total || 0}
            loading={loading}
            onChange={(next) => {
              setOffset(next);
              setSelected([]);
            }}
          />
        </DataState>
      </div>
    </Modal>
  );
}
