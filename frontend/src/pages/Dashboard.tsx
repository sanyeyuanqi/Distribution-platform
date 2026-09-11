import UsageTrend from './UsageTrend';
import { Link } from 'react-router-dom';
import {
  ArrowRight,
  Boxes,
  CircleDollarSign,
  Globe2,
  KeyRound,
  Plus,
  ShieldCheck,
  Users,
} from 'lucide-react';
import {
  DataState,
  datetime,
  Empty,
  items,
  money,
  Notice,
  Page,
  Refresh,
  shortId,
  Status,
  Table,
  useApp,
  useData,
} from '../core';
import { canAccessSection } from '../permissions';
import { usageAmountText } from '../usageAmounts';

export default function Dashboard() {
  const { t, user } = useApp();
  const { data, error, loading, refresh } = useData('/dashboard', 30000);
  const canReadAnnouncements = canAccessSection(user?.role, 'announcements');
  const canReadTasks = canAccessSection(user?.role, 'tasks');
  const announcements = useData(canReadAnnouncements ? '/announcements' : null);
  const d = data || {};
  const remoteUsage = d.remote_usage_total;
  const remoteUsageAmount = remoteUsage?.unit === 'USD' ? usageAmountText(remoteUsage.amount) : null;
  const usageCategories = (d.categories || []).flatMap((c: any) =>
    Object.entries(c.totals_by_unit || {}).map(([unit, amount]) => ({ ...c, unit, amount })),
  );
  const cards = [
    {
      label: t('本地渠道', 'Local channels'),
      value: d.local_channels,
      icon: KeyRound,
      color: 'purple',
      detail: t('去重后的密钥资源', 'Distinct key resources'),
    },
    {
      label: t('平台侧渠道', 'Remote channels'),
      value: d.remote_channels,
      icon: Globe2,
      color: 'blue',
      detail: t('各站点分发实例', 'Distributed site instances'),
    },
    {
      label: t('渠道消耗总计', 'Total channel usage'),
      value: remoteUsageAmount === null ? '—' : `$${remoteUsageAmount}`,
      icon: CircleDollarSign,
      color: 'green',
      detail:
        remoteUsageAmount === null
          ? t('等待渠道消耗同步', 'Waiting for channel usage to sync')
          : remoteUsage.covered < remoteUsage.total
            ? t(
                `USD · 已同步 ${remoteUsage.covered}/${remoteUsage.total} 个渠道`,
                `USD · ${remoteUsage.covered}/${remoteUsage.total} channels synced`,
              )
            : user?.role === 'superadmin'
              ? t('所有渠道累计消耗 · USD', 'All channels · Lifetime usage in USD')
              : t('可见渠道累计消耗 · USD', 'Visible channels · Lifetime usage in USD'),
    },
    {
      label: t(
        user?.role === 'user' ? '上传分组' : '工作空间成员',
        user?.role === 'user' ? 'Upload groups' : 'Workspace members',
      ),
      value: user?.role === 'user' ? d.groups : d.users,
      icon: user?.role === 'user' ? Boxes : Users,
      color: 'amber',
      detail:
        user?.role === 'user'
          ? t('按每次上传归组', 'Grouped per upload')
          : t('当前授权可见范围', 'Current authorized scope'),
    },
  ];
  return (
    <Page
      title={t('工作空间概览', 'Workspace overview')}
      subtitle={t(
        '所有渠道，一目了然。查看分发、消耗与结算的最新状态。',
        'Your channels at a glance. Track distribution, usage and settlements.',
      )}
      actions={
        <>
          <Refresh onClick={refresh} loading={loading} />
          {user?.role === 'superadmin' ? (
            <Link className="button" to="/sites">
              <Plus size={17} />
              {t('添加站点', 'Add site')}
            </Link>
          ) : (
            <Link className="button" to="/upload">
              <Plus size={17} />
              {t('上传密钥', 'Upload keys')}
            </Link>
          )}
        </>
      }
    >
      <div className="overview-toolbar dashboard-status">
        <span className="muted update-label">
          <span className="live-dot" />
          {t('后台自动同步 · 页面更新', 'Automatic background sync · Page updated')} {datetime(d.updated_at)}
        </span>
      </div>
      <DataState loading={loading && !data} error={error}>
        <div className="metric-grid">
          {cards.map((c) => (
            <div className="metric-card" key={c.label}>
              <div className="metric-top">
                <span>{c.label}</span>
                <div className={`metric-icon ${c.color}`}>
                  <c.icon size={20} />
                </div>
              </div>
              <strong>
                {c.value === undefined ? '—' : typeof c.value === 'number' ? money(c.value, 0) : c.value}
              </strong>
              <div className="metric-detail">{c.detail}</div>
            </div>
          ))}
        </div>
        {d.coverage && (
          <div className="coverage-banner">
            <div>
              <ShieldCheck size={18} />
              <strong>{t('消耗数据覆盖', 'Usage data coverage')}</strong>
              <span>
                {d.coverage.covered ?? 0} / {d.coverage.total ?? 0} {t('平台侧渠道', 'remote channels')}
              </span>
            </div>
            <p>
              {t(
                '未提供统计接口的站点标记为未支持，缺失消耗不会作为 0 参与结算。',
                'Sites without statistics are marked unsupported. Missing usage is never settled as zero.',
              )}
            </p>
          </div>
        )}
        <section className="panel trend-panel">
          <div className="panel-heading">
            <div>
              <h2>{t('分类消耗', 'Usage by category')}</h2>
              <p>{t('已核实消耗 · 各单位独立展示', 'Verified usage · Separate totals for each unit')}</p>
            </div>
            <span className="tag">{t('累计', 'Lifetime')}</span>
          </div>
          {usageCategories.length ? (
            <div className="category-bars">
              {usageCategories.map((c: any, i: number) => {
                const amount = Number(c.amount ?? c.total ?? c.total_base ?? 0);
                const max = Math.max(
                  ...usageCategories
                    .filter((x: any) => x.unit === c.unit)
                    .map((x: any) => Number(x.amount ?? x.total ?? x.total_base ?? 0)),
                  1,
                );
                return (
                  <div className="category-bar-row" key={`${c.category_id}-${c.unit}`}>
                    <div>
                      <span className={`category-mark c${i % 4}`}>
                        {(c.category_name || c.name || '?').slice(0, 1)}
                      </span>
                      <strong>{c.category_name || c.name}</strong>
                      <b>
                        {money(amount)} <small>{c.unit || 'USD'}</small>
                      </b>
                    </div>
                    <div className="bar-track">
                      <i style={{ width: `${(amount / max) * 100}%` }} />
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <Empty text={t('等待第一笔已核实消耗', 'Awaiting verified usage')} />
          )}
          <div className="panel-link">
            <Link to="/channels">
              {t('查看渠道明细', 'View channel details')}
              <ArrowRight size={15} />
            </Link>
          </div>
        </section>
        <UsageTrend trend={d.trend || []} partial={d.coverage?.covered < d.coverage?.total} />
        {(canReadTasks || canReadAnnouncements) && (
          <div className="dashboard-columns lower">
            {canReadTasks && (
              <section className="panel">
                <div className="panel-heading">
                  <div>
                    <h2>{t('最近任务', 'Recent tasks')}</h2>
                    <p>{t('分发与同步任务的执行进展', 'Distribution and synchronization progress')}</p>
                  </div>
                  <Link className="text-button" to="/settings/tasks">
                    {t('查看全部', 'View all')}
                    <ArrowRight size={15} />
                  </Link>
                </div>
                <Table
                  rows={d.recent_tasks || []}
                  columns={[
                    {
                      label: t('任务编号', 'Task'),
                      render: (r) => <span className="mono">#{shortId(r.id)}</span>,
                    },
                    { label: t('类型', 'Type'), key: 'kind' },
                    { label: t('状态', 'Status'), render: (r) => <Status value={r.status} /> },
                    { label: t('创建时间', 'Created'), render: (r) => datetime(r.created_at) },
                  ]}
                  pageSize={5}
                />
              </section>
            )}
            {canReadAnnouncements && (
              <section className="panel">
                <div className="panel-heading">
                  <div>
                    <h2>{t('工作空间公告', 'Workspace news')}</h2>
                    <p>{t('面向当前角色的最新消息', 'Updates for your role')}</p>
                  </div>
                  <Link to="/announcements" className="icon-button">
                    <ArrowRight size={17} />
                  </Link>
                </div>
                {items(announcements.data)
                  .filter((a) => a.status === 'published')
                  .slice(0, 3)
                  .map((a) => (
                    <Link className="announcement-teaser" to="/announcements" key={a.id}>
                      <span className="tag purple">{t('公告', 'News')}</span>
                      <strong>{t(a.title_zh, a.title_en || a.title_zh)}</strong>
                      <small>{datetime(a.published_at || a.created_at)}</small>
                    </Link>
                  ))}
                {!items(announcements.data).filter((a) => a.status === 'published').length && (
                  <Empty text={t('暂无已发布公告', 'No published announcements')} />
                )}
              </section>
            )}
          </div>
        )}
        {Object.entries(d.usage_by_unit || {}).filter(([u]) => u !== 'USD').length > 0 && (
          <Notice>
            {t('其他计价单位', 'Other pricing units')}:{' '}
            {Object.entries(d.usage_by_unit)
              .filter(([u]) => u !== 'USD')
              .map(([u, v]) => `${money(v)} ${u}`)
              .join(' · ')}
          </Notice>
        )}
      </DataState>
    </Page>
  );
}
