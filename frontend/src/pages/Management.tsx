import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  ArrowRight,
  Check,
  CheckCircle2,
  CircleDollarSign,
  Edit3,
  ExternalLink,
  FileText,
  Globe2,
  Layers3,
  Plus,
  Power,
  Search,
  Settings2,
  Trash2,
  X,
} from 'lucide-react';
import {
  api,
  Confirm,
  DataState,
  datetime,
  Empty,
  Field,
  items,
  JsonDetails,
  Modal,
  Notice,
  Page,
  query,
  RecordForm,
  Refresh,
  shortId,
  Status,
  Table,
  useAction,
  useApp,
  useData,
} from '../core';
import type { FormField, Row } from '../core';
import { siteConnectionErrorMessage, siteConnectionVersionInfo } from '../site-verification';
import type { Site, SiteConnectionCheck } from '../site-verification';
import CreateUserDialog from '../components/CreateUserDialog';
import ChannelUsageTotal from '../components/ChannelUsageTotal';
import Tooltip from '../components/Tooltip';
import DiscountModal from '../components/DiscountModal';
import AccountSettlementModal from '../components/AccountSettlementModal';
import { UserSettlementHistoryModal } from './SettlementOrders';
import SystemSettingsHeader from './SystemSettings';
import TablePagination from '../components/TablePagination';
import useDebouncedValue from '../useDebouncedValue';
import { usageAmountText } from '../usageAmounts';
import './users-management.css';

export function UsersPage() {
  const { user, t, notify } = useApp();
  const [search, setSearch] = useState('');
  const settledSearch = useDebouncedValue(search.trim());
  const pageKey = JSON.stringify([user?.id, settledSearch]);
  const [page, setPage] = useState({ key: '', offset: 0 });
  const offset = page.key === pageKey ? page.offset : 0;
  const limit = 50;
  const { data, error, loading, refresh } = useData(
    `/users?${query({ view: 'management', search: settledSearch, offset, limit })}`,
    30000,
  );
  const [params, setParams] = useSearchParams();
  const accountId = params.get('account_id') || '';
  const accountInPage = items(data).find((row) => row.id === accountId);
  const accountLookup = useData(
    accountId && !accountInPage
      ? `/users?${query({ view: 'management', user_id: accountId, limit: 1 })}`
      : null,
  );
  const account = accountInPage || items(accountLookup.data).find((row) => row.id === accountId);
  const [edit, setEdit] = useState<Row | null>(null);
  const [creating, setCreating] = useState(false);
  const [status, setStatus] = useState<Row | null>(null);
  const [discountPayee, setDiscountPayee] = useState<Row | null>(null);
  const [historyPayee, setHistoryPayee] = useState<Row | null>(null);
  const canSetDiscount = (row: Row) =>
    (user?.role === 'superadmin' && row.role === 'admin') ||
    (row.role === 'user' && row.parent_id === user?.id);
  const selectAccount = (id: string) => {
    const next = new URLSearchParams(params);
    if (id) next.set('account_id', id);
    else next.delete('account_id');
    setParams(next);
  };
  const rows = items(data);
  useEffect(() => {
    if (!loading && !error && data && offset > 0 && offset >= data.total) {
      setPage({ key: pageKey, offset: Math.max(0, Math.floor((data.total - 1) / limit) * limit) });
    }
  }, [data, error, loading, offset, pageKey]);
  return (
    <Page
      title={t(
        user?.role === 'superadmin' ? '用户管理' : '子账号管理',
        user?.role === 'superadmin' ? 'User management' : 'Subaccounts',
      )}
      subtitle={t(
        '集中管理账号、渠道与结算，查看累计消耗和结算统计。',
        'Manage accounts, channels and settlements, with lifetime usage and settlement totals.',
      )}
      actions={
        <>
          <Refresh onClick={refresh} loading={loading} />
          {user?.role === 'superadmin' && (
            <button className="button" onClick={() => setCreating(true)}>
              <Plus size={17} />
              {t('新建用户', 'Create user')}
            </button>
          )}
        </>
      }
    >
      <section className="panel">
        <div className="panel-toolbar">
          <label className="search-input">
            <Search size={16} aria-hidden="true" />
            <input
              aria-label={t('搜索账号', 'Search accounts')}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t('搜索 ID、昵称或用户名…', 'Search ID, display name or username…')}
            />
          </label>
          <span className="muted">
            {t('用户名创建后不可修改', 'Usernames cannot be changed after creation')}
          </span>
        </div>
        <DataState loading={loading} error={error}>
          <Table
            rows={rows}
            pageSize={limit}
            hidePagination
            columns={[
              {
                label: 'ID',
                render: (r) => <span className="numeric">{r.display_id ?? '—'}</span>,
              },
              {
                label: t('用户名', 'Username'),
                render: (r) => <strong>{r.username}</strong>,
              },
              {
                label: t('昵称', 'Display name'),
                render: (r) => r.nickname || '—',
              },
              {
                label: t('所属管理员', 'Parent administrator'),
                render: (r) =>
                  r.parent_id ? <span>{r.parent_username || '—'}</span> : <span className="muted">—</span>,
              },
              {
                label: t('账号类型', 'Account type'),
                render: (r) => (
                  <span className={`tag role-tag ${r.role === 'admin' ? 'blue' : 'purple'}`}>
                    {r.role === 'admin' ? t('管理员', 'Administrator') : t('普通用户', 'Member')}
                  </span>
                ),
              },
              {
                label: t('本地 / 平台渠道', 'Local / Remote'),
                render: (r) => (
                  <span className="numeric">
                    {r.local_channels ?? r.channel_count ?? '—'} / {r.remote_channels ?? '—'}
                  </span>
                ),
              },
              {
                label: t('累计消耗', 'Lifetime usage'),
                render: (r) => (error ? <span className="muted">—</span> : <ChannelUsageTotal channel={r} />),
              },
              {
                label: t('结算订单次数', 'Settlement orders'),
                render: (r) => (
                  <span className="numeric">{error ? '—' : (r.settlement_order_count ?? '—')}</span>
                ),
              },
              {
                label: t('结算总金额', 'Total settled amount'),
                render: (r) => (
                  <span className="numeric">
                    {error ? '—' : (usageAmountText(r.settlement_order_amount) ?? '—')}{' '}
                    {r.settlement_order_unit || 'USDT'}
                  </span>
                ),
              },
              {
                label: t('状态', 'Status'),
                render: (r) => <Status value={r.archived ? 'archived' : r.active ? 'active' : 'disabled'} />,
              },
              {
                label: t('操作', 'Actions'),
                className: 'table-actions-cell',
                render: (r) => (
                  <div className="row-actions users-management-actions">
                    <div className="users-management-action-row">
                      <button disabled={r.archived} onClick={() => setEdit(r)}>
                        <Edit3 size={14} />
                        {t('编辑', 'Edit')}
                      </button>
                      <button disabled={r.archived} onClick={() => setStatus(r)}>
                        <Power size={14} />
                        {r.active ? t('停用', 'Disable') : t('启用', 'Enable')}
                      </button>
                      <button disabled={loading || !!error} onClick={() => selectAccount(r.id)}>
                        <CircleDollarSign size={14} />
                        {t('结算', 'Settle')}
                      </button>
                    </div>
                    <div className="users-management-action-row">
                      <Tooltip
                        content={
                          canSetDiscount(r)
                            ? t('按渠道分类设置结算折扣', 'Set settlement discounts by channel category')
                            : t(
                                '该用户的分类折扣由其所属管理员设置',
                                'This user’s category discounts are managed by their parent administrator',
                              )
                        }
                      >
                        <button
                          disabled={loading || !!error || !canSetDiscount(r)}
                          onClick={() => setDiscountPayee(r)}
                        >
                          <Settings2 size={14} />
                          {t('设置汇率', 'Set rates')}
                        </button>
                      </Tooltip>
                      <button disabled={loading || !!error} onClick={() => setHistoryPayee(r)}>
                        <FileText size={14} />
                        {t('结算记录', 'Settlement history')}
                      </button>
                    </div>
                  </div>
                ),
              },
            ]}
          />
          {!error && (
            <TablePagination
              offset={offset}
              limit={limit}
              total={data?.total ?? 0}
              loading={loading || search.trim() !== settledSearch}
              onChange={(value) => setPage({ key: pageKey, offset: value })}
            />
          )}
        </DataState>
      </section>
      {creating && user?.role === 'superadmin' && (
        <CreateUserDialog onClose={() => setCreating(false)} onCreated={refresh} />
      )}
      {discountPayee && (
        <DiscountModal payee={discountPayee} onClose={() => setDiscountPayee(null)} onSaved={refresh} />
      )}
      {historyPayee && (
        <UserSettlementHistoryModal
          key={`${user?.id}:${historyPayee.id}`}
          payee={historyPayee}
          onClose={() => setHistoryPayee(null)}
        />
      )}
      {account && (
        <AccountSettlementModal
          key={account.id}
          account={account}
          onClose={() => selectAccount('')}
          onUpdated={refresh}
        />
      )}
      {accountId && !account && (
        <DataState loading={accountLookup.loading} error={accountLookup.error}>
          {accountLookup.data && !accountLookup.error && (
            <Notice kind="error">
              {t(
                '账号不存在或不在授权范围内。',
                'The account does not exist or is outside your access scope.',
              )}{' '}
              <button className="text-button" onClick={() => selectAccount('')}>
                {t('返回账号列表', 'Back to accounts')}
              </button>
            </Notice>
          )}
        </DataState>
      )}
      {edit?.id && (
        <RecordForm
          title={t('编辑账号', 'Edit account')}
          initial={edit}
          onClose={() => setEdit(null)}
          fields={[
            { name: 'username', label: t('用户名', 'Username'), required: true, readonly: true },
            { name: 'nickname', label: t('昵称', 'Display name'), required: true },
            {
              name: 'password',
              label: t('新密码', 'New password'),
              type: 'password',
              minLength: 6,
              maxLength: 256,
              hint: t(
                '密码至少 6 位，留空不修改。重置密码会使当前会话失效。',
                'At least 6 characters; leave blank to keep it. Resetting revokes current sessions.',
              ),
            },
          ]}
          onSave={async (v) => {
            const payload = { nickname: v.nickname, ...(v.password ? { password: v.password } : {}) };
            await api(`/users/${edit.id}`, 'PATCH', payload);
            refresh();
            notify(t('账号已保存', 'Account saved'));
          }}
        />
      )}
      {status && (
        <Confirm
          title={status.active ? t('停用账号', 'Disable account') : t('启用账号', 'Enable account')}
          onClose={() => setStatus(null)}
          onConfirm={async () => {
            await api(`/users/${status.id}`, 'PATCH', { active: !status.active });
            refresh();
          }}
        >
          <p>
            <strong>{status.username}</strong>
          </p>
          <Notice kind="warning">
            {status.active
              ? t(
                  '停用将终止该账号会话并停止未开始的写任务；下属账号及远端渠道不随之停用。',
                  'Disabling revokes this account’s sessions and stops pending writes. Its children and remote channels remain unchanged.',
                )
              : t(
                  '启用后，该账号可以重新登录和提交任务。',
                  'The account can sign in and submit tasks again.',
                )}
          </Notice>
        </Confirm>
      )}
    </Page>
  );
}

function SiteDelete({ site, onClose, onDone }: { site: Row; onClose: () => void; onDone: () => void }) {
  const { t } = useApp();
  const [name, setName] = useState('');
  const { run, busy } = useAction();
  return (
    <Modal title={t('删除站点', 'Delete site')} onClose={onClose}>
      <div className="form-stack">
        <Notice kind="warning">
          {t(
            '删除会归档本地站点并保留关联历史，不会自动删除远端渠道。',
            'Deletion archives the local site and preserves history. Remote channels are not automatically deleted.',
          )}
        </Notice>
        <p>
          {t('请手动输入以下站点名称：', 'Type this site name manually:')}{' '}
          <strong className="site-confirm-name" onCopy={(e) => e.preventDefault()}>
            {site.name}
          </strong>
        </p>
        <Field
          label={t('站点名称确认', 'Confirm site name')}
          hint={t('此输入框禁止复制、粘贴和拖放', 'Copy, paste and drag-and-drop are disabled')}
        >
          <input
            autoComplete="off"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onPaste={(e) => e.preventDefault()}
            onCopy={(e) => e.preventDefault()}
            onCut={(e) => e.preventDefault()}
            onDrop={(e) => e.preventDefault()}
          />
        </Field>
        <div className="form-actions">
          <button className="button secondary" onClick={onClose}>
            {t('取消', 'Cancel')}
          </button>
          <button
            className="button danger"
            disabled={name !== site.name || busy}
            onClick={() =>
              run(async () => {
                await api(`/sites/${site.id}`, 'DELETE', { confirmation: name });
                onDone();
                onClose();
              })
            }
          >
            <Trash2 size={16} />
            {t('确认删除', 'Delete site')}
          </button>
        </div>
      </div>
    </Modal>
  );
}
function siteTimestamp(value: unknown) {
  if (!value) return '—';
  const text = String(value);
  const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(text) ? text : `${text}Z`);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  })
    .format(date)
    .replaceAll('/', '-');
}

export function Sites() {
  const { t, notify } = useApp();
  const { data, error, loading, refresh } = useData('/sites');
  const [edit, setEdit] = useState<Row | null>(null),
    [remove, setRemove] = useState<Row | null>(null);
  const [distributionFailure, setDistributionFailure] = useState<{
    site: Site;
    issues: string[];
  } | null>(null);
  const { run, busy } = useAction();
  const showDistributionFailure = (site: Site) => {
    const issues = (site.distribution_issues || []).filter(
      (issue: unknown): issue is string => typeof issue === 'string' && !!issue.trim(),
    );
    setDistributionFailure({
      site,
      issues: issues.length
        ? issues
        : [
            site.verification_error?.message ||
              t(
                '分发配置尚未通过检查，请验证站点并检查分类模板。',
                'Distribution checks have not passed. Verify the site and check its category templates.',
              ),
          ],
    });
  };
  const fields: FormField[] = [
    { name: 'name', label: t('站点名称', 'Site name'), required: true },
    { name: 'prefix', label: t('命名前缀', 'Naming prefix'), required: true },
    {
      name: 'base_url',
      label: t('站点地址', 'Base URL'),
      required: true,
      type: 'url',
      hint: t(
        'HTTPS 地址；内网目标需要在服务端明确允许',
        'HTTPS URL; private destinations require an explicit server allowlist',
      ),
    },
    {
      name: 'adapter',
      label: t('接入适配器', 'Adapter'),
      type: 'select',
      value: 'silicon-v1',
      required: true,
      options: [
        { value: 'silicon-v1', label: 'Silicon Seller API' },
        { value: 'tcp-red-v1', label: 'TCP Red / Colin Supplier API' },
        { value: 'new-api-v1', label: 'NewAPI 标准渠道接口' },
        { value: 'spacex-hub-v1', label: 'SpaceX 供应商 Hub' },
      ],
      hint:
        edit?.adapter === 'spacex-hub-v1'
          ? t(
              'SpaceX 主账号负责管理渠道；上传需用户子账号系统令牌。每条远端渠道仅一个 Key，批量上传将逐条分发，创建后不可原地更换。',
              'SpaceX primary accounts manage channels; uploads require a user sub-account system token. Each Key is sent to a separate remote channel and cannot be replaced after creation.',
            )
          : t('SpaceX 上传需用户子账号系统令牌。', 'SpaceX uploads require a user sub-account system token.'),
    },
    {
      name: 'seller_user_id',
      label: 'New-Api-User',
      required: true,
      hint:
        edit?.adapter === 'spacex-hub-v1'
          ? t(
              '填写系统令牌所属的数字用户 ID；上传时须为用户子账号。',
              'Numeric user ID belonging to the system token; uploads require a user sub-account.',
            )
          : t('必须与卖家令牌匹配的数字用户 ID', 'Numeric seller user ID matching the token'),
    },
    {
      name: 'token',
      label:
        edit?.adapter === 'spacex-hub-v1'
          ? t('用户子账号系统令牌', 'User sub-account system token')
          : t('卖家访问令牌', 'Seller access token'),
      type: 'password',
      required: !edit?.id,
      hint: t(
        '仅写入，不在后续查询中返回；编辑时留空保留',
        'Write-only; leave blank on edit to keep current token',
      ),
    },
    {
      name: 'enabled',
      label: t('启用站点分发', 'Enable site distribution'),
      type: 'checkbox',
      value: false,
      hint: t(
        '连接与创建权限通过验证后可启用，模板可稍后配置。已启用模板仍需通过配置检查。',
        'Requires a verified connection and channel creation permission. Templates can be configured later; enabled templates must pass configuration checks.',
      ),
    },
    {
      name: 'collect_enabled',
      label: t('启用定时采集', 'Enable scheduled collection'),
      type: 'checkbox',
      value: true,
    },
  ];
  return (
    <Page
      title={t('站点管理', 'Site management')}
      subtitle={t(
        '集中管理卖家连接、分发设置与采集状态。令牌始终由服务端使用。',
        'Manage seller connections, distribution settings and collection status. Tokens remain on the server.',
      )}
      actions={
        <>
          <Refresh onClick={refresh} loading={loading} />
          <button className="button" onClick={() => setEdit({})}>
            <Plus size={17} />
            {t('添加站点', 'Add site')}
          </button>
        </>
      }
    >
      <section className="panel">
        <DataState loading={loading} error={error}>
          <Table
            rows={items(data)}
            scrollLabel={t('站点列表', 'Sites')}
            columns={[
              {
                label: 'ID',
                className: 'site-id-cell',
                render: (r) => <span>{r.display_id ?? '—'}</span>,
              },
              {
                label: t('站点', 'Site'),
                render: (r) => (
                  <div className="identity-cell">
                    <span className="site-avatar">
                      <Globe2 size={20} />
                    </span>
                    <strong>{r.name}</strong>
                  </div>
                ),
              },
              {
                label: t('地址', 'Address'),
                render: (r) => <span>{r.base_url}</span>,
              },
              {
                label: t('站点版本', 'Site version'),
                className: 'site-version-cell',
                render: (r) => {
                  const version = siteConnectionVersionInfo(r as Site, t);
                  return (
                    <Tooltip
                      content={
                        version?.tooltip ||
                        t(
                          '最近接口验证尚未提供版本信息',
                          'The latest connection check has not provided version information',
                        )
                      }
                    >
                      <span className={`site-version-value${version ? '' : ' muted'}`}>
                        {version?.summary ||
                          (r.connection_check?.ok === true ||
                          (!r.connection_check && r.verified_at && !r.verification_error)
                            ? t('未提供版本号', 'Version not provided')
                            : t('待验证', 'Unverified'))}
                      </span>
                    </Tooltip>
                  );
                },
              },
              {
                label: t('连接状态', 'Connection'),
                className: 'site-connection-cell',
                render: (r) => {
                  const check = (r as Site).connection_check;
                  const message = check
                    ? check.ok === true
                      ? ''
                      : siteConnectionErrorMessage(check.error, t)
                    : r.verification_error?.message;
                  const connected = check
                    ? check.ok === true
                    : !r.verification_error && r.health === 'healthy';
                  const adapterNotice =
                    connected && typeof r.adapter_notice === 'string' ? r.adapter_notice.trim() : '';
                  return (
                    <div>
                      <Status
                        value={
                          check
                            ? check.ok === true
                              ? 'healthy'
                              : 'failed'
                            : r.verification_error
                              ? 'failed'
                              : r.health
                        }
                      />
                      {message && <small className="block wrap-cell danger-text">{message}</small>}
                      {adapterNotice && <small className="block wrap-cell muted">{adapterNotice}</small>}
                    </div>
                  );
                },
              },
              {
                label: t('分发', 'Distribution'),
                render: (r) => <Status value={r.enabled ? 'enabled' : 'disabled'} />,
              },
              {
                label: t('最近验证', 'Last verified'),
                className: 'site-timestamp-cell',
                render: (r) => (
                  <small>{siteTimestamp(r.connection_check?.checked_at ?? r.verified_at)}</small>
                ),
              },
              {
                label: t('最近同步', 'Last synced'),
                className: 'site-timestamp-cell',
                render: (r) => <small>{siteTimestamp(r.last_sync_at)}</small>,
              },
              {
                label: t('操作', 'Actions'),
                className: 'table-actions-cell',
                render: (r) => (
                  <div className="row-actions">
                    <Tooltip
                      content={t(
                        '仅检查接口连接和认证，不检查模板适配，也不修改分发开关或其他配置。',
                        'Checks API connectivity and authentication only. Template compatibility, distribution and other settings are unchanged.',
                      )}
                    >
                      <button
                        disabled={busy || r.archived}
                        onClick={() =>
                          run(async () => {
                            try {
                              const res = await api<{ id: string; connection_check: SiteConnectionCheck }>(
                                `/sites/${r.id}/check-connection`,
                                'POST',
                              );
                              const check = res.connection_check;
                              if (check?.ok === true) {
                                const version = typeof check.version === 'string' ? check.version.trim() : '';
                                notify(
                                  `${t('接口连通验证通过', 'API connection check passed')}${version ? ` · ${version}` : ''}`,
                                );
                              } else {
                                notify(
                                  `${t('接口连通验证失败', 'API connection check failed')}：${siteConnectionErrorMessage(check?.error, t)}`,
                                  true,
                                );
                              }
                            } catch (reason) {
                              notify(
                                `${t('接口连通验证失败', 'API connection check failed')}：${reason instanceof TypeError ? t('无法连接服务，请检查网络后重试。', 'Could not connect. Check your network and retry.') : reason instanceof Error ? reason.message : t('请稍后重试。', 'Retry shortly.')}`,
                                true,
                              );
                            } finally {
                              refresh();
                            }
                          })
                        }
                      >
                        <CheckCircle2 size={14} />
                        {t('验证', 'Verify')}
                      </button>
                    </Tooltip>
                    <Link to={`/upload-templates?site_id=${encodeURIComponent(r.id)}`}>
                      <Layers3 size={14} />
                      {t('分类模板', 'Category templates')}
                    </Link>
                    <button disabled={r.archived} onClick={() => setEdit(r)}>
                      <Edit3 size={14} />
                      {t('编辑', 'Edit')}
                    </button>
                    <button
                      disabled={busy || r.archived}
                      onClick={() =>
                        run(async () => {
                          const wantedEnabled = !r.enabled;
                          const result = await api<Site>(`/sites/${r.id}`, 'PATCH', {
                            enabled: wantedEnabled,
                          });
                          refresh();
                          if (wantedEnabled && !result.enabled) {
                            showDistributionFailure(result);
                          } else {
                            notify(
                              result.enabled
                                ? t('站点分发已启用', 'Site distribution enabled')
                                : t('站点分发已停用', 'Site distribution disabled'),
                            );
                          }
                        })
                      }
                    >
                      <Power size={14} />
                      {r.enabled ? t('停用', 'Disable') : t('启用', 'Enable')}
                    </button>
                    <button disabled={r.archived} className="danger-text" onClick={() => setRemove(r)}>
                      <Trash2 size={14} />
                      {t('删除', 'Delete')}
                    </button>
                  </div>
                ),
              },
            ]}
          />
        </DataState>
      </section>
      {edit && (
        <RecordForm
          title={edit.id ? t('编辑站点', 'Edit site') : t('添加站点', 'Add site')}
          fields={edit.id ? fields : fields.filter((field) => field.name !== 'enabled')}
          description={
            !edit.id && (
              <Notice>
                {t(
                  '创建后自动生成对应的分发模板，默认停用。请在“分发模板”中配置后启用。',
                  'Distribution templates are created automatically and remain disabled. Configure and enable them in Distribution templates.',
                )}
              </Notice>
            )
          }
          initial={edit}
          onClose={() => setEdit(null)}
          onSave={async (v) => {
            const payload: Row = { ...v };
            if (!edit.id) payload.routing_group = 'default';
            if (!payload.token) delete payload.token;
            const result = await api<Site>(
              `/sites${edit.id ? `/${edit.id}` : ''}`,
              edit.id ? 'PATCH' : 'POST',
              payload,
            );
            refresh();
            if (payload.enabled && !result.enabled) {
              showDistributionFailure(result);
              notify(t('站点已保存，分发未启用', 'Site saved. Distribution remains disabled.'));
            } else if (!edit.id) {
              notify(
                t('站点已创建，分发模板已生成并停用', 'Site created with disabled distribution templates.'),
              );
            } else {
              notify(t('站点已保存', 'Site saved.'));
            }
          }}
        />
      )}
      {distributionFailure && (
        <Modal
          title={t('暂时无法启用分发', 'Cannot enable distribution')}
          onClose={() => setDistributionFailure(null)}
        >
          <div className="form-stack">
            <p className="wrap-cell">{distributionFailure.site.name}</p>
            <Notice kind="error">
              <ul className="site-distribution-issues">
                {distributionFailure.issues.map((issue, index) => (
                  <li key={`${index}-${issue}`}>{issue}</li>
                ))}
              </ul>
            </Notice>
            <div className="form-actions">
              <Link
                className="button secondary"
                to={`/upload-templates?site_id=${encodeURIComponent(distributionFailure.site.id)}`}
                onClick={() => setDistributionFailure(null)}
              >
                <Layers3 size={16} />
                {t('分类模板', 'Category templates')}
              </Link>
              <button className="button" onClick={() => setDistributionFailure(null)}>
                {t('知道了', 'OK')}
              </button>
            </div>
          </div>
        </Modal>
      )}
      {remove && <SiteDelete site={remove} onClose={() => setRemove(null)} onDone={refresh} />}
    </Page>
  );
}

export function Announcements() {
  const { t, user, notify, lang } = useApp();
  const { data, loading, error, refresh } = useData('/announcements');
  const [edit, setEdit] = useState<Row | null>(null);
  const [view, setView] = useState<Row | null>(null);
  const { run, busy } = useAction();
  const rows = items(data);
  return (
    <Page
      title={t(
        user?.role === 'superadmin' ? '公告管理' : '公告中心',
        user?.role === 'superadmin' ? 'Manage announcements' : 'Announcements',
      )}
      subtitle={t(
        '及时了解工作空间消息与服务更新。',
        'Stay up to date with workspace news and service updates.',
      )}
      actions={
        <>
          <Refresh onClick={refresh} loading={loading} />
          {user?.role === 'superadmin' && (
            <button className="button" onClick={() => setEdit({})}>
              <Plus size={17} />
              {t('新增公告', 'New announcement')}
            </button>
          )}
        </>
      }
    >
      <DataState loading={loading} error={error}>
        {rows.length ? (
          <div className="announcement-grid">
            {rows.map((a) => (
              <article className="panel announcement-card" key={a.id}>
                <div className="announcement-meta">
                  <Status value={a.status} />
                  <span>{datetime(a.published_at || a.updated_at || a.created_at)}</span>
                </div>
                <h2>{t(a.title_zh, a.title_en || a.title_zh)}</h2>
                <p>{t(a.content_zh, a.content_en || a.content_zh)}</p>
                {lang === 'en' && a.english_fallback && (
                  <small className="muted">English unavailable · Showing Chinese</small>
                )}
                <div className="announcement-actions">
                  <button
                    className="text-button"
                    onClick={() => {
                      setView(a);
                      if (a.status === 'published')
                        run(async () => {
                          await api(`/announcements/${a.id}/read`, 'POST');
                          refresh();
                          window.dispatchEvent(new Event('announcements-updated'));
                        });
                    }}
                  >
                    {t('阅读全文', 'Read announcement')}
                    <ArrowRight size={15} />
                  </button>
                  {user?.role === 'superadmin' ? (
                    <button className="icon-button" aria-label="Edit" onClick={() => setEdit(a)}>
                      <Edit3 size={16} />
                    </button>
                  ) : (
                    <span className={`tag ${!a.unread ? '' : 'purple'}`}>
                      {!a.unread ? t('已读', 'Read') : t('未读', 'Unread')}
                    </span>
                  )}
                </div>
              </article>
            ))}
          </div>
        ) : (
          <section className="panel">
            <Empty text={t('暂无公告', 'No announcements')} />
          </section>
        )}
      </DataState>
      {view && (
        <Modal
          title={t(view.title_zh, view.title_en || view.title_zh)}
          subtitle={datetime(view.published_at || view.created_at)}
          onClose={() => setView(null)}
        >
          <div className="prose-text">{t(view.content_zh, view.content_en || view.content_zh)}</div>
        </Modal>
      )}
      {edit && (
        <RecordForm
          title={edit.id ? t('编辑公告', 'Edit announcement') : t('新增公告', 'New announcement')}
          wide
          fieldsClassName="announcement-form-grid"
          initial={{
            ...edit,
            audience: Array.isArray(edit.audience)
              ? edit.audience.length === 3
                ? 'all'
                : edit.audience[0]
              : edit.audience || 'all',
          }}
          onClose={() => setEdit(null)}
          fields={[
            {
              name: 'title_zh',
              label: t('中文标题', 'Chinese title'),
              required: true,
              className: 'announcement-title-zh',
            },
            {
              name: 'content_zh',
              label: t('中文内容', 'Chinese content'),
              type: 'textarea',
              required: true,
              className: 'announcement-content-zh',
            },
            { name: 'title_en', label: t('英文标题', 'English title'), className: 'announcement-title-en' },
            {
              name: 'content_en',
              label: t('英文内容', 'English content'),
              type: 'textarea',
              className: 'announcement-content-en',
            },
            {
              name: 'status',
              label: t('发布状态', 'Publication status'),
              className: 'announcement-status',
              type: 'select',
              required: true,
              value: 'draft',
              options: [
                { value: 'draft', label: t('草稿', 'Draft') },
                { value: 'published', label: t('发布', 'Published') },
                { value: 'withdrawn', label: t('下架', 'Unpublished') },
              ],
            },
            {
              name: 'audience',
              label: t('受众', 'Audience'),
              className: 'announcement-audience',
              type: 'select',
              value: 'all',
              required: true,
              options: [
                { value: 'all', label: t('所有成员', 'All members') },
                { value: 'admin', label: t('管理员', 'Administrators') },
                { value: 'user', label: t('普通用户', 'Members') },
                { value: 'superadmin', label: t('超级管理员', 'Super administrators') },
              ],
            },
          ]}
          onSave={async (v) => {
            await api(`/announcements${edit.id ? `/${edit.id}` : ''}`, edit.id ? 'PATCH' : 'POST', {
              ...v,
              audience: v.audience === 'all' ? ['superadmin', 'admin', 'user'] : [v.audience],
            });
            refresh();
            window.dispatchEvent(new Event('announcements-updated'));
            notify(t('公告已保存', 'Announcement saved'));
          }}
        />
      )}
    </Page>
  );
}

export { default as ModelGaps } from './ModelGaps';

export function Audit() {
  const { t } = useApp();
  const { data, error, loading, refresh } = useData('/audit');
  const [search, setSearch] = useState('');
  const [view, setView] = useState<Row | null>(null);
  return (
    <>
      <SystemSettingsHeader actions={<Refresh onClick={refresh} loading={loading} />} />
      <section className="panel system-settings-panel">
        <div className="panel-toolbar">
          <label className="search-input">
            <Search size={16} aria-hidden="true" />
            <input
              aria-label={t('搜索动作或对象', 'Search action or object')}
              placeholder={t('搜索动作或对象…', 'Search action or object…')}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
        </div>
        <DataState loading={loading} error={error}>
          <Table
            scrollLabel={t('操作审计记录', 'Audit records')}
            resetScrollOnPageChange
            rows={items(data).filter((r) => JSON.stringify(r).toLowerCase().includes(search.toLowerCase()))}
            columns={[
              { label: t('时间', 'Time'), render: (r) => datetime(r.created_at) },
              {
                label: t('操作者', 'Actor'),
                render: (r) => r.actor_username || r.username || shortId(r.actor_id || r.user_id),
              },
              { label: t('动作', 'Action'), key: 'action' },
              {
                label: t('对象', 'Object'),
                render: (r) => (
                  <span>
                    {r.object_type}
                    <small className="block muted mono">{shortId(r.object_id)}</small>
                  </span>
                ),
              },
              {
                label: t('摘要', 'Summary'),
                render: (r) => (
                  <span className="truncate-cell">
                    {typeof r.summary === 'string' ? r.summary : JSON.stringify(r.summary)}
                  </span>
                ),
              },
              {
                label: t('详情', 'Details'),
                className: 'table-actions-cell',
                render: (r) => (
                  <button className="text-button" onClick={() => setView(r)}>
                    {t('查看', 'View')}
                  </button>
                ),
              },
            ]}
          />
        </DataState>
      </section>
      {view && (
        <Modal title={t('审计详情', 'Audit details')} onClose={() => setView(null)}>
          <JsonDetails value={view} />
        </Modal>
      )}
    </>
  );
}
