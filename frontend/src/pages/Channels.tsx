import Select from '../components/Select';
import Tooltip from '../components/Tooltip';
import ChannelCategoryPicker from '../components/ChannelCategoryPicker';
import ChannelUsageTotal from '../components/ChannelUsageTotal';
import ChannelDistributionTable from '../components/ChannelDistributionTable';
import TablePagination from '../components/TablePagination';
import useDebouncedValue from '../useDebouncedValue';
import { latestChannelSyncTime } from '../channel-sync-time';
import { ChannelRemoteStatus, ChannelUploadStatus } from '../components/ChannelDistributionStatus';
import UploadedKeyTestDialog from '../components/UploadedKeyTestDialog';
import type { ChannelCategoryOption } from '../components/ChannelCategoryPicker';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  Copy,
  Download,
  Edit3,
  Eye,
  EyeOff,
  FileKey,
  Globe2,
  KeyRound,
  ListTodo,
  List,
  LoaderCircle,
  MoreHorizontal,
  Plus,
  Play,
  Power,
  RefreshCw,
  Search,
  ShieldCheck,
  Trash2,
  UploadCloud,
  X,
} from 'lucide-react';
import {
  api,
  ApiError,
  Confirm,
  DataState,
  datetime,
  Empty,
  Field,
  items,
  JsonDetails,
  Modal,
  money,
  nonce,
  Notice,
  Page,
  query,
  RecordForm,
  Refresh,
  shortId,
  Status,
  Table,
  ServerPager,
  useAction,
  useApp,
  useData,
} from '../core';
import type { Row } from '../core';
import { canAccessSection } from '../permissions';
import SystemSettingsHeader from './SystemSettings';

export function TaskModal({ id, onClose }: { id: string; onClose: () => void }) {
  const { t, user } = useApp();
  if (!canAccessSection(user?.role, 'tasks'))
    return (
      <Modal
        title={t('请求已提交', 'Request submitted')}
        onClose={onClose}
        footer={
          <>
            <button className="button secondary" onClick={onClose}>
              {t('知道了', 'Got it')}
            </button>
            <Link className="button" to="/channels" onClick={onClose}>
              {t('查看我的渠道', 'View my channels')}
            </Link>
          </>
        }
      >
        <Notice>
          {t(
            '请求已交由后台处理，请在“我的渠道”刷新查看分发状态。',
            'Your request is being processed. Refresh My channels to view distribution status.',
          )}
        </Notice>
      </Modal>
    );
  return <TaskDetailsModal id={id} onClose={onClose} />;
}

function TaskDetailsModal({ id, onClose }: { id: string; onClose: () => void }) {
  const { t, notify } = useApp();
  const [preparedTask, setPreparedTask] = useState('');
  const { data, error, loading, refresh } = useData(`/tasks/${id}`);
  const { run, busy } = useAction();
  const automaticSync = ['sync', 'scheduled_sync', 'sync_usage'].includes(data?.kind || data?.type);
  useEffect(() => {
    if (
      data &&
      ['completed', 'succeeded', 'success', 'failed', 'cancelled', 'partial', 'needs_review'].includes(
        data.status,
      )
    )
      return;
    const timer = setInterval(refresh, 4000);
    return () => clearInterval(timer);
  }, [data?.status, refresh]);
  return (
    <Modal
      wide
      title={t('任务执行详情', 'Task execution details')}
      subtitle={`#${shortId(id)}`}
      onClose={onClose}
      footer={
        <>
          <span className="muted">
            {t('关闭窗口后，已提交任务仍保留在任务中心。', 'Submitted tasks remain in Tasks after closing.')}
          </span>
          <div className="actions">
            <Refresh onClick={refresh} loading={loading} />
            {data?.can_reprepare_templates && (
              <button
                className="button secondary"
                disabled={busy}
                onClick={() =>
                  run(
                    async () => {
                      const result = await api(`/tasks/${id}/reprepare-templates`, 'POST');
                      setPreparedTask(result.id);
                      refresh();
                    },
                    t('已按当前模板创建新任务', 'New task prepared with current templates'),
                  )
                }
              >
                <RefreshCw size={15} />
                {t('按当前模板重建', 'Rebuild with current templates')}
              </button>
            )}
            {data &&
              !automaticSync &&
              (data.status === 'needs_review' ||
                data.items?.some((i: Row) => i.status === 'needs_review')) && (
                <button
                  className="button secondary"
                  disabled={busy}
                  onClick={() =>
                    run(
                      async () => {
                        await api(`/tasks/${id}/reconcile`, 'POST');
                        refresh();
                      },
                      t('只读核实任务已提交', 'Read-only reconciliation queued'),
                    )
                  }
                >
                  {t('核实未知结果', 'Reconcile unknown results')}
                </button>
              )}
            {data && !automaticSync && ['failed', 'partial', 'unknown'].includes(data.status) && (
              <button
                className="button secondary"
                disabled={busy}
                onClick={() =>
                  run(
                    async () => {
                      await api(`/tasks/${id}/retry`, 'POST');
                      refresh();
                    },
                    t('已请求重试失败项', 'Failed items queued for retry'),
                  )
                }
              >
                {t('重试失败项', 'Retry failed items')}
              </button>
            )}
            {data && ['queued', 'pending', 'running'].includes(data.status) && (
              <button
                className="button secondary"
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    await api(`/tasks/${id}/cancel`, 'POST');
                    refresh();
                  })
                }
              >
                {t('取消未开始项', 'Cancel pending items')}
              </button>
            )}
          </div>
        </>
      }
    >
      <DataState loading={loading && !data} error={error}>
        {data && (
          <>
            <div className="detail-strip">
              <span>
                {t('任务状态', 'Status')} <Status value={data.status} />
              </span>
              <span>
                {t('类型', 'Type')}{' '}
                <strong>
                  {data.kind === 'force_delete_async'
                    ? t('远端清理', 'Remote cleanup')
                    : data.kind === 'reupload'
                      ? t('重新上传', 'Reupload')
                      : data.kind || data.type}
                </strong>
              </span>
              <span>
                {t('创建时间', 'Created')} <strong>{datetime(data.created_at)}</strong>
              </span>
            </div>
            {automaticSync && (
              <p className="muted">
                {t(
                  '同步由后台定时执行，失败后由后台重试。',
                  'Sync runs automatically in the background, including retries.',
                )}
              </p>
            )}
            {data.error && <Notice kind="error">{data.error}</Notice>}
            <Table
              rows={data.items || []}
              columns={[
                { label: t('渠道', 'Channel'), render: (r) => shortId(r.channel_id) },
                { label: t('站点', 'Site'), render: (r) => r.site_name || shortId(r.site_id) },
                { label: t('状态', 'Status'), render: (r) => <Status value={r.status} /> },
                { label: t('执行次数', 'Attempts'), key: 'attempts' },
                { label: t('远端渠道', 'Remote ID'), render: (r) => r.remote_id || '—' },
                {
                  label: t('结果 / 错误', 'Result / Error'),
                  render: (r) => (
                    <span className="wrap-cell">{r.error || r.message || r.result?.message || '—'}</span>
                  ),
                },
              ]}
            />
          </>
        )}
      </DataState>
      {preparedTask && <TaskModal id={preparedTask} onClose={() => setPreparedTask('')} />}
    </Modal>
  );
}

export function UploadPage() {
  const { t, user, notify } = useApp();
  const [search] = useSearchParams();
  const categories = useData('/categories');
  const formats = useData('/formats');
  const [mode, setMode] = useState('batch');
  const [form, setForm] = useState<Row>({
    category_id: search.get('category_id') || '',
    format_id: '',
    credentials: '',
    remarks: '',
    proxies: '',
    models: search.get('model') || '',
    declaration: '',
    rpm_enabled: false,
    rpm_limit: '',
    enable_strategy: 'disabled',
  });
  const [preview, setPreview] = useState<Row | null>(null);
  const [partial, setPartial] = useState(false);
  const [task, setTask] = useState('');
  const [step, setStep] = useState(1);
  const { run, busy } = useAction();
  const key = useRef(nonce());
  const enabledCategories = items(categories.data).filter((c) => c.active);
  const availableFormats = items(formats.data).filter((f) => f.category_id === form.category_id);
  const selectedFormat = availableFormats.find((f) => f.id === form.format_id);
  const set = (name: string, value: any) => {
    setForm((v) => ({ ...v, [name]: value }));
    setPreview(null);
    setStep(1);
    key.current = nonce();
  };
  const body = () => ({
    ...form,
    models: String(form.models)
      .split(/[\n,]/)
      .map((x) => x.trim())
      .filter(Boolean),
    rpm_limit: form.rpm_enabled && form.rpm_limit !== '' ? Number(form.rpm_limit) : null,
  });
  const validate = () =>
    run(async () => {
      const result = await api('/uploads/preview', 'POST', body());
      setPreview(result);
      setPartial(false);
      setStep(2);
    });
  return (
    <Page
      title={t('高级上传配置', 'Advanced upload configuration')}
      subtitle={t(
        '先校验，再分发。每一次提交形成独立分组，每一条密钥独立追踪。',
        'Validate before distribution. Each submission creates one group; each key is tracked independently.',
      )}
      actions={
        <>
          <Link className="button secondary" to="/upload">
            <ArrowLeft size={16} />
            {t('简化上传', 'Simple upload')}
          </Link>
          {canAccessSection(user?.role, 'tasks') && (
            <Link className="button secondary" to="/settings/tasks">
              <ListTodo size={16} />
              {t('查看上传任务', 'Upload tasks')}
            </Link>
          )}
        </>
      }
    >
      <Notice>
        {t(
          '已设置分发模板的分类请使用简化上传；手动配置适用于尚未配置模板的分类。',
          'Use simple upload for categories with distribution templates. Manual settings are for categories without templates.',
        )}
      </Notice>
      <div className="upload-steps">
        {[
          t('配置与输入', 'Configure & input'),
          t('校验与预览', 'Validate & preview'),
          t('提交分发', 'Submit distribution'),
        ].map((s, i) => (
          <div className={step >= i + 1 ? 'active' : ''} key={s}>
            <span>{step > i + 1 ? <Check size={15} /> : i + 1}</span>
            <strong>{s}</strong>
            {i < 2 && <i />}
          </div>
        ))}
      </div>
      <div className="upload-layout">
        <div className="upload-main">
          <section className="panel upload-panel">
            <div className="panel-heading">
              <div>
                <h2>{t('凭据配置', 'Credential configuration')}</h2>
                <p>
                  {t('选择一个具体渠道分类及其已适配格式', 'Choose a channel category and supported format')}
                </p>
              </div>
              <span className="step-tag">01</span>
            </div>
            <div className="form-grid">
              <Field label={t('渠道分类 *', 'Category *')}>
                <Select
                  required
                  value={form.category_id}
                  onChange={(e) => {
                    set('category_id', e.target.value);
                    setForm((v) => ({ ...v, format_id: '' }));
                  }}
                >
                  <option value="">{t('选择渠道分类', 'Select category')}</option>
                  {enabledCategories.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name} · {c.family}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label={t('凭据格式 *', 'Credential format *')}>
                <Select required value={form.format_id} onChange={(e) => set('format_id', e.target.value)}>
                  <option value="">{t('选择凭据格式', 'Choose a credential format')}</option>
                  {availableFormats.map((f) => (
                    <option disabled={!f.enabled} key={f.id} value={f.id}>
                      {f.name}
                      {!f.enabled ? t(' · 待适配', ' · Unsupported') : ''}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            {categories.error || formats.error ? (
              <Notice kind="error">{categories.error || formats.error}</Notice>
            ) : null}
            {selectedFormat?.schema_config?.example && (
              <Notice>
                {t('格式示例（占位凭据）', 'Format example (placeholder credentials)')}:{' '}
                <code>
                  {typeof selectedFormat.schema_config.example === 'string'
                    ? selectedFormat.schema_config.example
                    : JSON.stringify(selectedFormat.schema_config.example)}
                </code>
              </Notice>
            )}
            <div className="form-section-divider" />
            <div className="panel-heading compact">
              <h2>{t('密钥内容', 'Credentials')}</h2>
              <div className="segmented small">
                <button
                  className={mode === 'single' ? 'selected' : ''}
                  onClick={() => {
                    setMode('single');
                    setPreview(null);
                    setStep(1);
                    key.current = nonce();
                  }}
                >
                  {t('单个上传', 'Single')}
                </button>
                <button
                  className={mode === 'batch' ? 'selected' : ''}
                  onClick={() => {
                    setMode('batch');
                    setPreview(null);
                    setStep(1);
                    key.current = nonce();
                  }}
                >
                  {t('批量上传', 'Batch')}
                </button>
              </div>
            </div>
            <Field
              label={t(
                mode === 'batch' ? '密钥列表 *' : '密钥 *',
                mode === 'batch' ? 'Credential list *' : 'Credential *',
              )}
              hint={t(
                '每行一条凭据。内部空行会报错；同配置重复项保留首次，配置冲突会阻止提交。',
                'One credential per line. Internal empty lines are invalid. Matching duplicates keep the first; configuration conflicts block submission.',
              )}
            >
              <textarea
                className="credential-input"
                rows={mode === 'single' ? 3 : 7}
                spellCheck={false}
                autoComplete="off"
                value={form.credentials}
                onChange={(e) => set('credentials', e.target.value)}
                placeholder={t(
                  '粘贴与你所选格式一致的凭据，每行一条',
                  'Paste credentials matching your selected format, one per line',
                )}
              />
            </Field>
            <div className="line-counter">
              <ShieldCheck size={14} />
              {t(
                '密钥仅发送到本系统服务端，列表默认脱敏',
                'Keys are sent only to this server; list responses are masked',
              )}
              <span>
                {form.credentials ? form.credentials.split('\n').length : 0} {t('行', 'lines')}
              </span>
            </div>
            <div className="form-grid">
              <Field
                label={t('逐行备注', 'Per-line remarks')}
                hint={t(
                  '留空或与密钥行数一致；在去重前绑定',
                  'Leave empty or match key line count; mapped before deduplication',
                )}
              >
                <textarea
                  rows={3}
                  value={form.remarks}
                  onChange={(e) => set('remarks', e.target.value)}
                  placeholder={t('对应每条密钥的备注', 'A remark for each key')}
                />
              </Field>
              <Field
                label={t('逐行代理', 'Per-line proxies')}
                hint={t(
                  '用于远端调用上游；不支持时预览明确报错',
                  'For remote upstream calls; unsupported sites will be flagged',
                )}
              >
                <textarea
                  rows={3}
                  spellCheck={false}
                  autoComplete="off"
                  value={form.proxies}
                  onChange={(e) => set('proxies', e.target.value)}
                  placeholder="https://user:password@proxy.example:443"
                />
              </Field>
            </div>
          </section>
          <section className="panel upload-panel">
            <div className="panel-heading">
              <div>
                <h2>{t('分发选项', 'Distribution options')}</h2>
                <p>
                  {t('所有启用站点都会参与能力校验', 'All enabled sites participate in capability checks')}
                </p>
              </div>
              <span className="step-tag">02</span>
            </div>
            <div className="form-grid">
              <Field
                label={t('模型范围 *', 'Models *')}
                hint={t(
                  '请填写至少一个模型，每行一个或使用逗号分隔。使用模板模型请前往简化上传。',
                  'Enter at least one model, one per line or comma-separated. Use simple upload for template models.',
                )}
              >
                <textarea
                  rows={3}
                  value={form.models}
                  onChange={(e) => set('models', e.target.value)}
                  placeholder="gpt-4.1\ngpt-4.1-mini"
                />
              </Field>
              <Field label={t('号况申报', 'Account declaration')}>
                <textarea
                  rows={3}
                  value={form.declaration}
                  onChange={(e) => set('declaration', e.target.value)}
                  placeholder={t(
                    '说明账号状态、额度或使用限制',
                    'Describe account state, allowance or restrictions',
                  )}
                />
              </Field>
              <Field label={t('创建后的启用策略', 'Enable strategy')}>
                <Select value={form.enable_strategy} onChange={(e) => set('enable_strategy', e.target.value)}>
                  <option value="disabled">
                    {t('保持停用，稍后人工启用', 'Keep disabled; enable manually later')}
                  </option>
                  <option value="test_then_enable">
                    {t('测试通过后启用', 'Enable after successful test')}
                  </option>
                </Select>
              </Field>
              <Field
                label={t('RPM 保护', 'RPM protection')}
                hint={t(
                  '仅在目标支持时可用，不代表跨站点共享限流',
                  'Requires target support; does not imply cross-site rate limiting',
                )}
              >
                <div className="inline-fields">
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={form.rpm_enabled}
                      onChange={(e) => set('rpm_enabled', e.target.checked)}
                    />
                    {t('开启', 'Enable')}
                  </label>
                  <input
                    type="number"
                    min={1}
                    disabled={!form.rpm_enabled}
                    value={form.rpm_limit}
                    onChange={(e) => set('rpm_limit', e.target.value)}
                    placeholder={t('每分钟请求上限', 'Requests per minute')}
                  />
                </div>
              </Field>
            </div>
          </section>
          {preview && (
            <section className="panel preview-panel">
              <div className="panel-heading">
                <h2>{t('校验预览', 'Validation preview')}</h2>
                <Status value={preview.can_submit ? 'valid' : 'invalid'} />
              </div>
              <div className="preview-counts">
                {[
                  ['original_count', t('原始行', 'Original')],
                  ['valid_count', t('有效密钥', 'Valid')],
                  ['duplicate_count', t('重复项', 'Duplicates')],
                  ['conflict_count', t('冲突项', 'Conflicts')],
                ].map(([k, l]) => (
                  <div key={k}>
                    <strong>{preview[k] ?? 0}</strong>
                    <span>{l}</span>
                  </div>
                ))}
              </div>
              {(preview.errors || []).length > 0 && (
                <Notice kind="error">
                  {preview.errors.map((e: any, i: number) => (
                    <div key={i}>{typeof e === 'string' ? e : JSON.stringify(e)}</div>
                  ))}
                </Notice>
              )}
              <Table
                rows={preview.rows || []}
                columns={[
                  { label: t('原始行', 'Line'), key: 'line' },
                  { label: t('密钥（脱敏）', 'Masked key'), key: 'key_hint' },
                  { label: t('备注', 'Remark'), key: 'remark' },
                  { label: t('状态', 'Status'), render: (r) => <Status value={r.status} /> },
                  { label: t('说明', 'Message'), key: 'message' },
                ]}
              />
              <div className="panel-heading">
                <h3>{t('目标站点兼容性', 'Target compatibility')}</h3>
              </div>
              <div className="target-list">
                {(preview.targets || []).map((s: Row) => (
                  <div key={s.id}>
                    <Globe2 size={18} />
                    <strong>{s.name}</strong>
                    <Status value={s.compatible ? 'valid' : 'unsupported'} />
                    <p>
                      {(s.issues || [])
                        .map((x: any) => (typeof x === 'string' ? x : JSON.stringify(x)))
                        .join('；')}
                    </p>
                  </div>
                ))}
              </div>
              {preview.targets?.some((s: Row) => !s.compatible) && (
                <label className="checkbox-label partial-choice">
                  <input type="checkbox" checked={partial} onChange={(e) => setPartial(e.target.checked)} />
                  {t(
                    '已核对不兼容原因，仅向兼容目标提交（不兼容目标保留明确结果）',
                    'I reviewed incompatibilities. Submit compatible targets only, retaining explicit results for excluded targets.',
                  )}
                </label>
              )}
            </section>
          )}
          <div className="upload-footer">
            <span>
              <ShieldCheck size={16} />
              {t(
                '提交时服务端会再次验证格式、权限和目标能力',
                'Server revalidates formats, permissions and targets on submission',
              )}
            </span>
            <div className="actions">
              <button
                className="button secondary"
                disabled={
                  busy ||
                  !form.category_id ||
                  !form.format_id ||
                  !form.credentials ||
                  (mode === 'single' && form.credentials.trim().includes('\n'))
                }
                onClick={validate}
              >
                {busy ? <LoaderCircle className="spin" size={16} /> : <Eye size={16} />}{' '}
                {t('校验并预览', 'Validate & preview')}
              </button>
              <button
                className="button"
                disabled={
                  busy ||
                  !preview ||
                  (!preview.can_submit && !(partial && preview.can_submit_partial)) ||
                  !!preview.conflict_count ||
                  !(preview.valid_count > 0)
                }
                onClick={() =>
                  run(
                    async () => {
                      const result = await api('/uploads/submit', 'POST', {
                        ...body(),
                        allow_partial: partial,
                        idempotency_key: key.current,
                      });
                      setTask(result.id);
                      setStep(3);
                      setForm((v) => ({ ...v, credentials: '', proxies: '' }));
                      setPreview(null);
                      key.current = nonce();
                    },
                    t('上传任务已提交', 'Upload task submitted'),
                  )
                }
              >
                <UploadCloud size={17} />
                {t('确认分发', 'Submit distribution')}
              </button>
            </div>
          </div>
        </div>
        <aside className="upload-aside">
          <div className="guide-card">
            <div className="guide-icon">
              <FileKey size={25} />
            </div>
            <h3>{t('让每条密钥都可追踪', 'Every key, traceable')}</h3>
            <p>
              {t(
                '一次上传，一个分组。各站点的分发结果可单独查看和恢复。',
                'One upload, one group. Inspect and recover each site’s distribution independently.',
              )}
            </p>
            <ol>
              <li>
                <strong>{t('绑定逐行配置', 'Map line settings')}</strong>
                <span>
                  {t('先关联备注与代理，再执行去重。', 'Map remarks and proxies before deduplication.')}
                </span>
              </li>
              <li>
                <strong>{t('预览全部目标', 'Preview every target')}</strong>
                <span>{t('不支持的选项会明确列出。', 'Unsupported options are explicitly listed.')}</span>
              </li>
              <li>
                <strong>{t('异步安全分发', 'Distribute asynchronously')}</strong>
                <span>
                  {t('成功项保留，失败项单独重试。', 'Keep successes; retry failed items separately.')}
                </span>
              </li>
            </ol>
            <Link to="/channels">
              {t('查看已有渠道', 'View existing channels')}
              <ArrowRight size={15} />
            </Link>
          </div>
          <Notice>
            {t(
              '请勿上传无权管理的凭据。原始 Key 不会保存在浏览器本地存储中。',
              'Upload only credentials you may manage. Raw keys are never stored in browser local storage.',
            )}
          </Notice>
        </aside>
      </div>
      {task && <TaskModal id={task} onClose={() => setTask('')} />}
    </Page>
  );
}

function DeleteChannelDialog({
  channelId,
  displayId,
  targets,
  onClose,
  onSubmitted,
}: {
  channelId: string;
  displayId?: number;
  targets: Row[];
  onClose: () => void;
  onSubmitted: (taskId: string) => void;
}) {
  const { t } = useApp();
  const { run, busy } = useAction();
  const [error, setError] = useState('');
  const close = () => {
    if (!busy) onClose();
  };
  return (
    <Modal
      title={t('确认删除渠道', 'Confirm channel deletion')}
      subtitle={`#${displayId ?? '—'}`}
      wide
      onClose={close}
      footer={
        <div className="actions" style={{ marginLeft: 'auto' }}>
          <button className="button secondary" disabled={busy} onClick={close}>
            {t('取消', 'Cancel')}
          </button>
          <button
            className="button danger"
            disabled={busy || !targets.length}
            onClick={() =>
              run(async () => {
                setError('');
                try {
                  const result = await api(`/channels/${channelId}/actions`, 'POST', {
                    action: 'delete_remote',
                    distribution_ids: targets.map((target) => target.id),
                    confirmation: `DELETE ${targets.length}`,
                  });
                  if (!result.id || !result.kind)
                    throw new Error(
                      t(
                        '删除请求结果未确认，请刷新后重试。',
                        'The deletion request was not confirmed. Refresh and retry.',
                      ),
                    );
                  onSubmitted(result.id);
                  onClose();
                } catch (reason) {
                  setError(
                    reason instanceof Error
                      ? reason.message
                      : t('删除请求失败，请重试。', 'Deletion request failed. Try again.'),
                  );
                }
              })
            }
          >
            {busy ? <LoaderCircle size={16} className="spin" /> : <Trash2 size={16} />}
            {busy ? t('正在提交…', 'Submitting…') : t('确认删除', 'Delete channels')}
          </button>
        </div>
      }
    >
      <div className="form-stack">
        {error && (
          <div role="alert">
            <Notice kind="error">{error}</Notice>
          </div>
        )}
        <Notice kind="warning">
          {t(
            `将删除以下 ${targets.length} 个远端渠道，删除后无法恢复。`,
            `This will permanently delete the following ${targets.length} remote channels.`,
          )}
        </Notice>
        <Table
          rows={targets}
          columns={[
            { label: t('站点', 'Site'), render: (row) => row.site_name || shortId(row.site_id) },
            {
              label: t('远端渠道', 'Remote channel'),
              render: (row) => (
                <span className="wrap-cell">
                  {row.remote_name || '—'}
                  {row.partition_label && (
                    <small className="block">
                      {row.partition_label} · {row.key_count ?? 1} {t('个密钥', 'keys')}
                    </small>
                  )}
                </span>
              ),
            },
            { label: t('远端编号', 'Remote ID'), render: (row) => row.remote_id },
            { label: t('状态', 'Status'), render: (row) => <Status value={row.status} /> },
          ]}
        />
        <p className="muted">
          {t(
            '本地渠道记录与历史消耗保留。归档仅适用于所有远端分发均已停用的渠道。远端状态不明确时，后台会先核实再处理。',
            'The local channel and usage history are retained. Archiving requires all remote distributions to be disabled. Uncertain remote status will be checked before deletion.',
          )}
        </p>
      </div>
    </Modal>
  );
}

function ForceDeleteDialog({
  channelId,
  target,
  onClose,
  onSubmitted,
}: {
  channelId: string;
  target: Row;
  onClose: () => void;
  onSubmitted: (result: Row) => void;
}) {
  const { t } = useApp();
  const { run, busy } = useAction();
  const [error, setError] = useState('');
  const requestKey = useRef(nonce());
  const close = () => {
    if (!busy) onClose();
  };
  return (
    <Modal
      title={t('确认强制删除', 'Confirm force deletion')}
      subtitle={target.site_name || shortId(target.site_id)}
      onClose={close}
      footer={
        <div className="actions" style={{ marginLeft: 'auto' }}>
          <button className="button secondary" disabled={busy} onClick={close}>
            {t('取消', 'Cancel')}
          </button>
          <button
            className="button danger"
            disabled={busy || !target.id}
            onClick={() =>
              run(async () => {
                setError('');
                try {
                  const result = await api(`/channels/${channelId}/actions`, 'POST', {
                    action: 'force_delete_async',
                    distribution_ids: [target.id],
                    confirmation: 'DELETE LOCAL AND QUEUE REMOTE 1',
                    idempotency_key: requestKey.current,
                  });
                  if (
                    !result.id ||
                    result.kind !== 'force_delete_async' ||
                    result.result?.local_deleted !== true
                  )
                    throw new Error(
                      t(
                        '删除任务尚未确认，请重试以查询同一次请求。',
                        'The deletion task was not confirmed. Retry to check the same request.',
                      ),
                    );
                  onSubmitted(result);
                  onClose();
                } catch (reason) {
                  setError(
                    reason instanceof TypeError
                      ? t(
                          '请求结果未知，请检查网络后重试，系统会查询同一次删除任务。',
                          'The request result is unknown. Check your connection and retry to find the same deletion task.',
                        )
                      : reason instanceof Error
                        ? reason.message
                        : t('永久删除失败，请重试。', 'Permanent deletion failed. Try again.'),
                  );
                }
              })
            }
          >
            {busy ? <LoaderCircle size={16} className="spin" /> : <Trash2 size={16} />}
            {busy ? t('正在提交…', 'Submitting…') : t('确认强制删除', 'Confirm deletion')}
          </button>
        </div>
      }
    >
      <div className="form-stack">
        {error && (
          <div role="alert">
            <Notice kind="error">{error}</Notice>
          </div>
        )}
        <Notice kind="warning">
          {t(
            '立即永久删除这一行的本地分发及相关记录，同时将远端渠道交给后台异步清理。远端清理失败或需要核实不会恢复本地记录，可在“远端清理任务”中查看结果并重试。',
            'Immediately and permanently delete this local distribution and its related records, and queue remote cleanup in the background. Failed or unconfirmed remote cleanup does not restore local records. View results and retry from Remote cleanup tasks.',
          )}
        </Notice>
        <p className="wrap-cell">
          <strong>{target.remote_name || t('未绑定远端渠道', 'No linked remote channel')}</strong>
          <small className="block">
            {t('远端编号', 'Remote ID')}：{target.remote_id ?? '—'}
          </small>
          {target.partition_label && (
            <small className="block">
              {target.partition_label} · {target.key_count ?? 1} {t('个密钥', 'keys')}
            </small>
          )}
        </p>
        <p className="muted">
          {t(
            '删除后无法恢复，其他本地分发保留。如果这是最后一条分发，还会删除本地渠道及全部密钥版本，之后可重新上传相同密钥。',
            'Deletion cannot be undone. Other local distributions remain. If this is the last distribution, the local channel and all key versions are also deleted, allowing the same key to be uploaded again.',
          )}
        </p>
      </div>
    </Modal>
  );
}

function ReuploadChannelDialog({
  channelId,
  target,
  onClose,
  onSubmitted,
}: {
  channelId: string;
  target: Row;
  onClose: () => void;
  onSubmitted: (taskId: string) => void;
}) {
  const { t } = useApp();
  const { run, busy } = useAction();
  const [error, setError] = useState('');
  const requestKey = useRef(nonce());
  const close = () => {
    if (!busy) onClose();
  };
  return (
    <Modal
      title={t('确认重新上传', 'Confirm reupload')}
      onClose={close}
      footer={
        <div className="actions" style={{ marginLeft: 'auto' }}>
          <button className="button secondary" disabled={busy} onClick={close}>
            {t('取消', 'Cancel')}
          </button>
          <button
            className="button"
            disabled={busy || !target.id}
            onClick={() =>
              run(async () => {
                setError('');
                try {
                  const result = await api(`/channels/${channelId}/actions`, 'POST', {
                    action: 'reupload',
                    distribution_ids: [target.id],
                    idempotency_key: requestKey.current,
                  });
                  if (!result.id || result.kind !== 'reupload')
                    throw new Error(
                      t(
                        '重新上传任务尚未确认，请重试以查询同一次请求。',
                        'The reupload task was not confirmed. Retry to check the same request.',
                      ),
                    );
                  onSubmitted(result.id);
                  onClose();
                } catch (reason) {
                  setError(
                    reason instanceof TypeError
                      ? t(
                          '请求结果未知，请检查网络后重试，系统会查询同一次重新上传任务。',
                          'The request result is unknown. Check your connection and retry to find the same reupload task.',
                        )
                      : reason instanceof Error
                        ? reason.message
                        : t('重新上传失败，请重试。', 'Reupload failed. Try again.'),
                  );
                }
              })
            }
          >
            {busy ? <LoaderCircle size={16} className="spin" /> : <UploadCloud size={16} />}
            {busy ? t('正在提交…', 'Submitting…') : t('确认重新上传', 'Confirm reupload')}
          </button>
        </div>
      }
    >
      <div className="form-stack">
        {error && (
          <div role="alert">
            <Notice kind="error">{error}</Notice>
          </div>
        )}
        <p className="wrap-cell">
          {t('目标站点', 'Destination site')}：<strong>{target.site_name || '—'}</strong>
          {target.partition_label && (
            <small className="block">
              {target.partition_label} · {target.key_count ?? 1} {t('个密钥', 'keys')}
            </small>
          )}
        </p>
        <Notice>
          {t(
            '使用当前保存的密钥和此站点当前模板，重新上传这一条失败分发。后台会先查重，其他分发不受影响。',
            'Reupload this failed distribution using the currently saved keys and this site’s current template. The backend checks for duplicates first. Other distributions are unaffected.',
          )}
        </Notice>
        <p className="muted">
          {t(
            '提交后进度和结果会自动更新。',
            'Progress and results will update automatically after submission.',
          )}
        </p>
      </div>
    </Modal>
  );
}

const CHECK_RUNNING = ['pending', 'queued', 'running'];
const CHECK_FINISHED = [
  'succeeded',
  'success',
  'completed',
  'passed',
  'synced',
  'failed',
  'needs_review',
  'cancelled',
  'unsupported',
];
const DELETE_FINISHED = [
  'succeeded',
  'success',
  'completed',
  'failed',
  'partial',
  'needs_review',
  'cancelled',
];

function RemoteCleanupTaskDialog({
  id,
  initial,
  onClose,
  onBack,
  onUpdated,
}: {
  id: string;
  initial?: Row;
  onClose: () => void;
  onBack?: () => void;
  onUpdated: () => void;
}) {
  const { t, notify } = useApp();
  const { data, setData, error, loading, refresh } = useData(`/channels/action-tasks/${id}`);
  const { run, busy } = useAction();
  const [retryError, setRetryError] = useState('');
  const candidate = data?.id === id ? data : initial?.id === id ? initial : null;
  const task = candidate?.kind === 'force_delete_async' ? candidate : null;
  const running = !task || !DELETE_FINISHED.includes(task.status);
  const close = () => {
    if (!busy) onClose();
  };
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(refresh, 2500);
    return () => clearInterval(timer);
  }, [running, refresh]);
  useEffect(() => {
    if (task?.status) onUpdated();
  }, [task?.status, onUpdated]);
  return (
    <Modal
      wide
      title={t('远端清理任务', 'Remote cleanup task')}
      onClose={close}
      footer={
        <>
          <span className="muted">
            {t(
              '可随时关闭窗口，稍后从标签分组查看清理结果。',
              'You can close this window anytime and view the cleanup result from Tag groups later.',
            )}
          </span>
          <div className="actions">
            {onBack && (
              <button className="button secondary" disabled={busy} onClick={onBack}>
                {t('任务列表', 'Task list')}
              </button>
            )}
            <Refresh onClick={refresh} loading={loading} />
            {task?.can_retry === true && (
              <button
                className="button"
                disabled={busy || loading}
                onClick={() =>
                  run(async () => {
                    setRetryError('');
                    try {
                      const result = await api(`/channels/action-tasks/${id}/retry`, 'POST');
                      if (result.id !== id || result.kind !== 'force_delete_async')
                        throw new Error(
                          t(
                            '重试结果尚未确认，请刷新任务查看。',
                            'The retry result is not confirmed. Refresh the task to check.',
                          ),
                        );
                      setData(result);
                      refresh();
                      onUpdated();
                      notify(t('已提交远端清理重试。', 'Remote cleanup retry submitted.'));
                    } catch (reason) {
                      setRetryError(
                        reason instanceof TypeError
                          ? t(
                              '重试结果未知，请检查网络并刷新任务查看。',
                              'The retry result is unknown. Check your connection and refresh the task.',
                            )
                          : reason instanceof Error
                            ? reason.message
                            : t('重试失败，请稍后重试。', 'Retry failed. Please try again later.'),
                      );
                    }
                  })
                }
              >
                {busy ? <LoaderCircle size={15} className="spin" /> : <RefreshCw size={15} />}
                {t('重试远端清理', 'Retry remote cleanup')}
              </button>
            )}
          </div>
        </>
      }
    >
      <DataState loading={loading && !task} error={error}>
        {retryError && (
          <div role="alert">
            <Notice kind="error">{retryError}</Notice>
          </div>
        )}
        {candidate && !task && (
          <Notice kind="error">
            {t(
              '无法确认此清理任务，请刷新后重试。',
              'This cleanup task could not be confirmed. Refresh and retry.',
            )}
          </Notice>
        )}
        {task && (
          <div className="form-stack">
            <div className="detail-strip">
              <span>
                {t('任务状态', 'Status')} <Status value={task.status} />
              </span>
              <span>
                {t('创建时间', 'Created')} <strong>{datetime(task.created_at)}</strong>
              </span>
            </div>
            <Notice kind={task.result?.local_deleted === true ? 'info' : 'warning'}>
              {task.result?.local_deleted === true
                ? t('本地记录已永久删除。', 'Local records have been permanently deleted.')
                : t(
                    '本地删除结果尚未确认，请刷新任务查看。',
                    'Local deletion is not confirmed. Refresh the task to check.',
                  )}{' '}
              {task.result?.remote_confirmed === true
                ? t('远端清理已确认完成。', 'Remote cleanup is confirmed complete.')
                : running
                  ? t(
                      '远端清理正在后台处理，结果会自动更新。',
                      'Remote cleanup is processing in the background. Results update automatically.',
                    )
                  : t(
                      '远端清理尚未确认完成，请查看下方原因。',
                      'Remote cleanup is not confirmed complete. See the reason below.',
                    )}
            </Notice>
            {task.error && <Notice kind="error">{task.error}</Notice>}
            <Table
              rows={task.items || []}
              columns={[
                {
                  label: t('站点', 'Site'),
                  render: (row) => (
                    <span className="wrap-cell">{row.site_name || t('目标站点', 'Destination site')}</span>
                  ),
                },
                { label: t('清理状态', 'Cleanup status'), render: (row) => <Status value={row.status} /> },
                {
                  label: t('说明', 'Details'),
                  render: (row) => <span className="wrap-cell">{row.error || row.message || '—'}</span>,
                },
              ]}
            />
          </div>
        )}
      </DataState>
    </Modal>
  );
}

function RemoteCleanupTasks({
  request,
  onOpenChange,
}: {
  request: Row | null;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useApp();
  const [offset, setOffset] = useState(0);
  const limit = 20;
  const { data, setData, error, loading, refresh } = useData(
    `/channels/action-tasks?${query({ kind: 'force_delete_async', offset, limit })}`,
  );
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState('');
  useEffect(() => {
    onOpenChange(open);
    return () => onOpenChange(false);
  }, [open, onOpenChange]);
  const tasks = items(data).filter((row) => row.kind === 'force_delete_async');
  const running = tasks.filter((row) => !DELETE_FINISHED.includes(row.status)).length;
  useEffect(() => {
    if (!request?.id) return;
    setOffset(0);
    setData(null);
    setSelected(request.id);
    setOpen(true);
    refresh();
  }, [request?.id, refresh, setData]);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(refresh, 4000);
    return () => clearInterval(timer);
  }, [running, refresh]);
  return (
    <>
      <div className="remote-cleanup-toolbar">
        <button
          className="button secondary"
          onClick={() => {
            setSelected('');
            setOpen(true);
            refresh();
          }}
        >
          <ListTodo size={16} />
          {t('远端清理任务', 'Remote cleanup tasks')}
        </button>
        {running > 0 && (
          <span role="status" className="muted">
            {t(`${running} 个任务正在后台处理`, `${running} tasks processing in the background`)}
          </span>
        )}
        {error && (
          <small className="danger-text">
            {t(
              '暂时无法读取清理任务，点击查看或重试。',
              'Cleanup tasks could not be loaded. Open the list to retry.',
            )}
          </small>
        )}
      </div>
      {open &&
        (selected ? (
          <RemoteCleanupTaskDialog
            key={selected}
            id={selected}
            initial={
              tasks.find((row) => row.id === selected) || (request?.id === selected ? request : undefined)
            }
            onClose={() => {
              setOpen(false);
              refresh();
            }}
            onBack={() => {
              setSelected('');
              refresh();
            }}
            onUpdated={refresh}
          />
        ) : (
          <Modal
            wide
            title={t('远端清理任务', 'Remote cleanup tasks')}
            onClose={() => setOpen(false)}
            footer={<Refresh onClick={refresh} loading={loading} />}
          >
            <DataState loading={loading && !data} error={error}>
              <Table
                key={offset}
                rows={tasks}
                pageSize={limit}
                columns={[
                  { label: t('创建时间', 'Created'), render: (row) => datetime(row.created_at) },
                  {
                    label: t('站点', 'Sites'),
                    render: (row) => (
                      <span className="wrap-cell">
                        {[
                          ...new Set((row.items || []).map((item: Row) => item.site_name).filter(Boolean)),
                        ].join('、') || '—'}
                      </span>
                    ),
                  },
                  { label: t('状态', 'Status'), render: (row) => <Status value={row.status} /> },
                  {
                    label: t('操作', 'Actions'),
                    className: 'table-actions-cell',
                    render: (row) => (
                      <button className="button secondary" onClick={() => setSelected(row.id)}>
                        {t('查看', 'View')}
                      </button>
                    ),
                  },
                ]}
              />
              <ServerPager
                offset={offset}
                total={data?.total ?? 0}
                limit={limit}
                loading={loading}
                onChange={(next) => {
                  setData(null);
                  setOffset(next);
                }}
              />
            </DataState>
          </Modal>
        ))}
    </>
  );
}

function syncedUsd(usage: Row): string | null {
  if (usage.conversion_status !== 'available' || usage.used_amount_unit !== 'USD') return null;
  const raw = usage.used_amount;
  if (typeof raw !== 'string' || !/^\d+(?:\.\d+)?$/.test(raw)) return null;
  const [whole, fraction = ''] = raw.split('.');
  const integral = whole.replace(/^0+(?=\d)/, '');
  if (integral === '0' && !/[1-9]/.test(fraction.slice(0, 8)) && /[1-9]/.test(fraction))
    return '<$0.00000001';
  const scaled = BigInt(integral + fraction.slice(0, 8).padEnd(8, '0'));
  const rounded = (scaled + (fraction.length > 8 && fraction[8] >= '5' ? 1n : 0n))
    .toString()
    .padStart(9, '0');
  const dollars = rounded.slice(0, -8).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const cents = rounded.slice(-8).replace(/0+$/, '').padEnd(2, '0');
  return `$${dollars}.${cents}`;
}

function ChannelDetails({
  id,
  onClose,
  onChange,
  onRemoteCleanup,
}: {
  id: string;
  onClose: () => void;
  onChange: () => void;
  onRemoteCleanup: (task: Row) => void;
}) {
  const { t, user, notify } = useApp();
  const { data, setData, error, loading, refresh } = useData(`/channels/${id}`);
  const [key, setKey] = useState('');
  const [edit, setEdit] = useState(false);
  const [rotate, setRotate] = useState(false);
  const [action, setAction] = useState('');
  const [task, setTask] = useState('');
  const [deleteTargets, setDeleteTargets] = useState<Row[] | null>(null);
  const [forceDeleteTarget, setForceDeleteTarget] = useState<Row | null>(null);
  const [forceJob, setForceJob] = useState<Row | null>(null);
  const restoredForceTasks = useRef(new Set<string>());
  const [reuploadTarget, setReuploadTarget] = useState<Row | null>(null);
  const [submittedChecks, setSubmittedChecks] = useState<
    { distributionId: string; kind: 'test' | 'reupload'; taskId: string }[]
  >([]);
  const { run, busy } = useAction();
  const d = data || {};
  const forceJobBusy = !!forceJob && !DELETE_FINISHED.includes(forceJob.status);
  useEffect(() => {
    const saved = data?.force_delete_task;
    if (!saved?.id || forceJobBusy || restoredForceTasks.current.has(saved.id)) return;
    restoredForceTasks.current.add(saved.id);
    setForceJob({ id: saved.id, status: 'restoring' });
  }, [data?.force_delete_task?.id]);
  useEffect(() => {
    if (!forceJob?.id || !forceJobBusy) return;
    let alive = true;
    let polling = false;
    const targetIds = new Set<string>(
      forceJob.distributionIds || (forceJob.distributionId ? [forceJob.distributionId] : []),
    );
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const result = await api(`/channels/action-tasks/${forceJob.id}`);
        if (!alive) return;
        if (result.id !== forceJob.id || result.kind !== 'force_delete')
          throw new Error(
            t('删除进度无法确认，请稍后重试。', 'Deletion progress could not be confirmed. Retry shortly.'),
          );
        for (const item of result.items || [])
          if (typeof item.distribution_id === 'string') targetIds.add(item.distribution_id);
        const terminal = DELETE_FINISHED.includes(result.status);
        if (
          ['succeeded', 'success', 'completed'].includes(result.status) &&
          result.result?.remote_confirmed === true &&
          Number.isInteger(result.result?.deleted_distribution_count) &&
          result.result.deleted_distribution_count > 0
        ) {
          if (result.result.deleted_channel_count > 0) {
            notify(
              t(
                '远端渠道及本地渠道、密钥已永久删除。',
                'The remote channel, local channel and keys were permanently deleted.',
              ),
            );
            onChange();
            onClose();
            return;
          }
          try {
            const channel = await api(`/channels/${id}`);
            if (!alive) return;
            if (
              channel.id !== id ||
              !Array.isArray(channel.distributions) ||
              channel.distributions.some((row: Row) => targetIds.has(row.id))
            )
              throw new Error(
                t('删除结果正在更新，请稍候。', 'Deletion results are still updating. Please wait.'),
              );
            setData(channel);
            setForceJob(null);
            notify(
              t(
                '远端渠道及本地分发记录已永久删除。',
                'The remote channel and local distribution were permanently deleted.',
              ),
            );
            onChange();
          } catch (reason) {
            if (!alive) return;
            if (reason instanceof ApiError && reason.status === 404) {
              notify(
                t(
                  '远端渠道及本地渠道、密钥已永久删除。',
                  'The remote channel, local channel and keys were permanently deleted.',
                ),
              );
              onChange();
              onClose();
            } else throw reason;
          }
          return;
        }
        const messages = [
          result.error,
          ...(result.items || []).map((item: Row) => item.error || item.message),
        ].filter((message): message is string => typeof message === 'string' && !!message.trim());
        setForceJob((current) =>
          current && current.id === forceJob.id
            ? {
                ...current,
                distributionIds: [...targetIds],
                siteName:
                  current.siteName ||
                  [...new Set((result.items || []).map((item: Row) => item.site_name).filter(Boolean))].join(
                    '、',
                  ),
                status:
                  terminal && ['succeeded', 'success', 'completed'].includes(result.status)
                    ? 'needs_review'
                    : result.status,
                error: [...new Set(messages)].join('；'),
                pollError: '',
              }
            : current,
        );
        if (terminal) refresh();
      } catch (reason) {
        if (alive)
          setForceJob((current) =>
            current?.id === forceJob.id
              ? {
                  ...current,
                  pollError:
                    reason instanceof TypeError
                      ? t(
                          '暂时无法读取删除进度，正在重试，请勿重复提交。',
                          'Could not read deletion progress. Retrying; do not submit again.',
                        )
                      : reason instanceof Error
                        ? reason.message
                        : t(
                            '暂时无法读取删除进度，正在重试。',
                            'Could not read deletion progress. Retrying.',
                          ),
                }
              : current,
          );
      } finally {
        polling = false;
      }
    };
    void poll();
    const timer = setInterval(poll, 2500);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [forceJob?.id, forceJobBusy, id]);
  useEffect(() => {
    if (!data) return;
    setSubmittedChecks((current) => {
      const remaining = current.filter((check) => {
        const row = (data.distributions || []).find((row: Row) => row.id === check.distributionId);
        if (!row || (check.kind !== 'test' && (row.locally_deleted || row.status === 'deleted')))
          return false;
        const result = row?.[checkField(check.kind)];
        return checkTaskId(result, check.kind) !== check.taskId || !CHECK_FINISHED.includes(result?.status);
      });
      return remaining.length === current.length ? current : remaining;
    });
  }, [data]);
  const checkField = (kind: 'test' | 'reupload') =>
    kind === 'reupload' ? 'reupload_task' : 'connectivity_test';
  const checkTaskId = (result: Row | undefined, kind: string) =>
    kind === 'reupload' ? result?.id : result?.task_id;
  const pendingChecks = submittedChecks.filter((check) => {
    const row = (d.distributions || []).find((row: Row) => row.id === check.distributionId);
    if (!row || (check.kind !== 'test' && (row.locally_deleted || row.status === 'deleted'))) return false;
    const result = row?.[checkField(check.kind)];
    return checkTaskId(result, check.kind) !== check.taskId || CHECK_RUNNING.includes(result?.status);
  });
  const checksRunning =
    pendingChecks.length > 0 ||
    (d.distributions || []).some(
      (row: Row) =>
        CHECK_RUNNING.includes(row.connectivity_test?.status) ||
        (!row.locally_deleted &&
          row.status !== 'deleted' &&
          (CHECK_RUNNING.includes(row.usage_sync?.status) ||
            CHECK_RUNNING.includes(row.upload_status) ||
            CHECK_RUNNING.includes(row.reupload_task?.status))),
    );
  useEffect(() => {
    if (!checksRunning || forceJobBusy) return;
    const timer = setInterval(refresh, 2500);
    return () => clearInterval(timer);
  }, [checksRunning, forceJobBusy, refresh]);
  const actionsPending = checksRunning || forceJobBusy;
  const submittedCheck = (distributionId: string, kind: 'test' | 'reupload', taskId: string) => {
    setSubmittedChecks((current) => [
      ...current.filter((check) => check.distributionId !== distributionId || check.kind !== kind),
      { distributionId, kind, taskId },
    ]);
    refresh();
    onChange();
    notify(
      t(
        kind === 'test' ? '测试已提交，结果将自动更新。' : '重新上传已提交，结果将自动更新。',
        'Request submitted. Results will update automatically.',
      ),
    );
  };
  const canDeleteDistribution = (row: Row) =>
    !!row.id &&
    !row.locally_deleted &&
    row.remote_id !== null &&
    row.remote_id !== undefined &&
    String(row.remote_id) !== '' &&
    row.status !== 'deleted';
  const toggleDistribution = (row: Row) =>
    run(async () => {
      if (!['enabled', 'disabled'].includes(row.remote_status)) return;
      const enable = row.remote_status === 'disabled';
      const action = enable ? 'enable' : 'disable';
      const result = await api(`/channels/${id}/actions`, 'POST', {
        action,
        distribution_ids: [row.id],
      });
      if (!result.id || result.kind !== action)
        throw new Error(
          enable
            ? t(
                '启用请求未确认，请刷新后查看任务状态。',
                'The enable request was not confirmed. Refresh to check its status.',
              )
            : t(
                '停用请求未确认，请刷新后查看任务状态。',
                'The disable request was not confirmed. Refresh to check its status.',
              ),
        );
      setTask(result.id);
      refresh();
      onChange();
      notify(
        enable
          ? t('启用请求已提交', 'Enable request submitted')
          : t('停用请求已提交', 'Disable request submitted'),
      );
    });
  const deletable = (d.distributions || []).filter(canDeleteDistribution);
  const canForceDeleteDistribution = (row: Row) => !!row.id;
  const canReuploadDistribution = (row: Row) =>
    !!row.id &&
    row.reupload_available === true &&
    !row.remote_id &&
    row.status === 'failed' &&
    !row.locally_deleted &&
    !d.archived;
  const openDeleteDialog = (rows: Row[]) =>
    setDeleteTargets(
      rows.map((row) => ({
        id: row.id,
        site_id: row.site_id,
        site_name: row.site_name,
        remote_id: row.remote_id,
        remote_name: row.remote_name,
        partition_label: row.partition_label,
        key_count: row.key_count,
        status: row.status,
      })),
    );
  return (
    <Modal
      wide
      className="channel-detail-modal"
      title={t('渠道详情', 'Channel details')}
      subtitle={`#${d.display_id ?? '—'} · ${d.category_name || shortId(d.category_id)}`}
      onClose={onClose}
    >
      <DataState loading={loading && !data} error={error}>
        {data && (
          <>
            <div className="detail-strip">
              <span>
                {t('所有者', 'Owner')}{' '}
                <strong>{d.owner_name || d.owner_username || shortId(d.owner_id)}</strong>
              </span>
              <span>
                {t('上传分组', 'Upload group')}{' '}
                <strong>{d.group_name || d.group_tag || shortId(d.group_id)}</strong>
              </span>
              <span>
                {t('Key 版本', 'Key version')} <strong>v{d.key_version}</strong>
              </span>
              <span>
                {t('密钥数量', 'Key count')} <strong>{d.key_count ?? 1}</strong>
                {d.key_mode === 'multiple' && <small> · {t('多密钥渠道', 'Multi-key channel')}</small>}
              </span>
              <span>
                {t('创建时间', 'Created')} <strong>{datetime(d.created_at)}</strong>
              </span>
            </div>
            <div className="secret-box">
              <div>
                <span className="muted">
                  {t('当前凭据', 'Current credentials')}
                  {d.key_count > 1 ? ` · ${d.key_count} ${t('个密钥', 'keys')}` : ''}
                </span>
                <code>{key || d.key_hint}</code>
              </div>
              <div className="actions">
                <button
                  className="button secondary"
                  disabled={busy}
                  onClick={() =>
                    key
                      ? setKey('')
                      : run(async () => {
                          const r = await api(`/channels/${id}/reveal`, 'POST');
                          setKey(r.key);
                        })
                  }
                >
                  {key ? <EyeOff size={16} /> : <Eye size={16} />}{' '}
                  {key ? t('隐藏', 'Hide') : t('查看完整 Key', 'Reveal full key')}
                </button>
                {key && (
                  <button
                    className="icon-button"
                    aria-label="Copy key"
                    onClick={() =>
                      run(
                        async () => {
                          await navigator.clipboard.writeText(key);
                        },
                        t('已复制', 'Copied'),
                      )
                    }
                  >
                    <Copy size={17} />
                  </button>
                )}
              </div>
            </div>
            {d.key_mode === 'multiple' && (
              <p className="muted wrap-cell">
                {t(
                  '同一批密钥归入本地渠道，远端按认证方式或资源分为独立分发，结果逐行显示。',
                  'Keys in this batch share one local channel. Authentication or resource differences create separate remote distributions, each shown below.',
                )}
              </p>
            )}
            <div className="panel-heading">
              <h3>{t('平台侧分发明细', 'Remote distributions')}</h3>
              <div className="actions">
                <Refresh
                  onClick={() => {
                    refresh();
                    onChange();
                  }}
                  loading={loading}
                />
                <button className="button secondary" onClick={() => setEdit(true)}>
                  <Edit3 size={15} />
                  {t('编辑', 'Edit')}
                </button>
                <button className="button secondary" onClick={() => setRotate(true)}>
                  <KeyRound size={15} />
                  {d.key_mode === 'multiple'
                    ? t('更换整组 Key', 'Rotate all keys')
                    : t('更换 Key', 'Rotate key')}
                </button>
              </div>
            </div>
            {forceJob && (
              <div role={forceJobBusy ? 'status' : 'alert'}>
                <Notice kind={forceJobBusy ? 'info' : 'error'}>
                  <strong>
                    {forceJobBusy
                      ? t(
                          '正在强制删除，完成后自动更新。',
                          'Deletion is in progress. Results will update automatically.',
                        )
                      : forceJob.status === 'cancelled'
                        ? t(
                            '删除任务已取消，本地记录保留。',
                            'Deletion was cancelled. Local records are retained.',
                          )
                        : forceJob.status === 'needs_review'
                          ? t(
                              '远端删除结果待核实，本地记录保留。',
                              'Remote deletion needs verification. Local records are retained.',
                            )
                          : t('强制删除失败，本地记录保留。', 'Deletion failed. Local records are retained.')}
                  </strong>
                  <small className="block wrap-cell">{forceJob.siteName}</small>
                  {(forceJob.error || forceJob.pollError) && (
                    <small className="block wrap-cell">{forceJob.error || forceJob.pollError}</small>
                  )}
                </Notice>
              </div>
            )}
            <Table
              rows={d.distributions || []}
              scrollLabel={t('平台侧分发明细', 'Remote distributions')}
              columns={[
                { label: t('站点', 'Site'), render: (r) => r.site_name || shortId(r.site_id) },
                {
                  label: t('远端渠道', 'Remote channel'),
                  className: 'channel-remote-cell',
                  render: (r) => (
                    <span>
                      {r.remote_name || '—'}
                      <small className="block mono muted">{r.remote_id || '—'}</small>
                      {r.locally_deleted && (
                        <small className="block muted">
                          {t('本地已删除，历史记录保留', 'Removed locally; history retained')}
                        </small>
                      )}
                    </span>
                  ),
                },
                {
                  label: t('密钥类型', 'Key type'),
                  className: 'channel-key-type-cell',
                  render: (r) => r.partition_label || '—',
                },
                {
                  label: t('Key 数量', 'Key count'),
                  render: (r) => r.key_count ?? 1,
                },
                {
                  label: t('上传状态', 'Upload status'),
                  className: 'channel-upload-cell',
                  render: (r) => {
                    const pending = pendingChecks.some(
                      (check) => check.distributionId === r.id && check.kind === 'reupload',
                    );
                    return <ChannelUploadStatus row={r} pending={pending} />;
                  },
                },
                {
                  label: t('远端状态', 'Remote status'),
                  className: 'channel-status-cell',
                  render: (r) => <ChannelRemoteStatus row={r} />,
                },
                {
                  label: t('消耗额度', 'Usage amount'),
                  className: 'channel-usage-cell',
                  render: (r) => {
                    const result = r.usage_sync || {};
                    const usage = result.remote_usage || {};
                    const usd = syncedUsd(usage);
                    const hasAmount = usd != null || usage.used_quota != null || usage.balance != null;
                    const status = result.status || r.usage_status || 'not_synced';
                    const failed = ['failed', 'needs_review', 'unsupported'].includes(status);
                    return (
                      <Tooltip
                        content={
                          <>
                            <span className="block">
                              {usd != null
                                ? t('最近同步的美元累计消耗。', 'Latest synced cumulative usage in USD.')
                                : usage.conversion_message ||
                                  t(
                                    '暂无可靠的美元消耗金额。',
                                    'No reliable usage amount in USD is available.',
                                  )}
                            </span>
                            {usage.used_quota != null && (
                              <span className="block">
                                {t('原始消耗', 'Raw usage')}{' '}
                                {Number(usage.used_quota).toLocaleString('en-US', {
                                  maximumFractionDigits: 8,
                                })}{' '}
                                {usage.quota_unit || 'quota'}
                              </span>
                            )}
                            {usage.balance != null && (
                              <span className="block">
                                {t('余额', 'Balance')} {money(usage.balance)}{' '}
                                {usage.balance_unit || t('（原始值）', '(raw value)')}
                              </span>
                            )}
                            {!hasAmount && (
                              <span className="block">
                                {['succeeded', 'success', 'synced', 'remote_snapshot'].includes(status)
                                  ? t('未提供额度', 'No amount provided')
                                  : t('未同步', 'Not synced')}
                              </span>
                            )}
                            {CHECK_RUNNING.includes(status) && <Status value={status} />}
                            {failed && (
                              <span className="block danger-text">
                                {result.message ||
                                  t(
                                    '同步失败，后台将自动重试。',
                                    'Sync failed. The background service will retry.',
                                  )}
                              </span>
                            )}
                          </>
                        }
                      >
                        <span className="numeric" style={{ whiteSpace: 'nowrap' }}>
                          {usd ?? '—'}
                        </span>
                      </Tooltip>
                    );
                  },
                },
                {
                  label: t('最近同步', 'Last sync'),
                  className: 'channel-sync-cell',
                  render: (r) => datetime(r.usage_sync?.synced_at || r.last_sync_at),
                },
                {
                  label: t('操作', 'Actions'),
                  className: 'table-actions-cell',
                  render: (r) => (
                    <>
                      <div className="channel-row-actions channel-row-actions-with-reupload">
                        <Tooltip
                          content={
                            r.remote_status === 'disabled'
                              ? t('启用此条远端渠道', 'Enable this remote channel')
                              : r.remote_status === 'enabled'
                                ? t('停用此条远端渠道', 'Disable this remote channel')
                                : t('等待后台同步远端状态', 'Waiting for the background status sync')
                          }
                        >
                          <button
                            className="button secondary"
                            disabled={
                              busy ||
                              loading ||
                              actionsPending ||
                              d.archived ||
                              !canDeleteDistribution(r) ||
                              !['enabled', 'disabled'].includes(r.remote_status)
                            }
                            onClick={() => toggleDistribution(r)}
                          >
                            <Power size={15} />
                            {r.remote_status === 'disabled' ? t('启用', 'Enable') : t('停用', 'Disable')}
                          </button>
                        </Tooltip>
                        <Tooltip
                          content={
                            !canReuploadDistribution(r)
                              ? r.reupload_reason ||
                                t(
                                  '仅可重新上传尚未建立远端渠道的失败分发。',
                                  'Only failed distributions without a remote channel can be reuploaded.',
                                )
                              : t(
                                  '使用当前保存的密钥和当前模板重新上传到此站点',
                                  'Reupload to this site using the current keys and template',
                                )
                          }
                        >
                          <button
                            className="button secondary"
                            disabled={busy || loading || actionsPending || !canReuploadDistribution(r)}
                            onClick={() => setReuploadTarget({ ...r })}
                          >
                            <UploadCloud size={15} />
                            {t('重新上传', 'Reupload')}
                          </button>
                        </Tooltip>
                        <Tooltip
                          content={t(
                            '立即删除本地分发，远端渠道交给后台异步清理',
                            'Delete this local distribution immediately and queue remote cleanup',
                          )}
                        >
                          <button
                            className="button secondary danger-text"
                            disabled={
                              busy ||
                              loading ||
                              forceJobBusy ||
                              pendingChecks.some((check) => check.kind === 'reupload') ||
                              (d.distributions || []).some((row: Row) =>
                                CHECK_RUNNING.includes(row.reupload_task?.status),
                              ) ||
                              !canForceDeleteDistribution(r)
                            }
                            onClick={() => setForceDeleteTarget({ ...r })}
                          >
                            <Trash2 size={15} />
                            {t('强制删除', 'Force delete')}
                          </button>
                        </Tooltip>
                        <Tooltip
                          content={t(
                            '删除远端渠道，保留本地记录',
                            'Delete the remote channel and retain local records',
                          )}
                        >
                          <button
                            className="button secondary danger-text"
                            disabled={
                              busy || loading || actionsPending || d.archived || !canDeleteDistribution(r)
                            }
                            onClick={() => openDeleteDialog([r])}
                          >
                            <Trash2 size={15} />
                            {t('删除', 'Delete')}
                          </button>
                        </Tooltip>
                      </div>
                      {!r.locally_deleted && r.error && r.error !== r.upload_message && (
                        <small className="channel-operation-message block wrap-cell danger-text">
                          {t('操作提示', 'Operation notice')}：{r.error}
                        </small>
                      )}
                    </>
                  ),
                },
              ]}
            />
            <div className="form-actions">
              {['enable', 'disable', 'redistribute'].map((a) => (
                <button
                  key={a}
                  className="button secondary"
                  disabled={busy || actionsPending || d.archived}
                  onClick={() => setAction(a)}
                >
                  {
                    (
                      {
                        enable: t('启用渠道', 'Enable'),
                        disable: t('停用渠道', 'Disable'),
                        redistribute: t('补分发', 'Redistribute'),
                      } as Row
                    )[a]
                  }
                </button>
              ))}
              {d.archived && (
                <button
                  className="button secondary"
                  disabled={busy || loading || actionsPending}
                  onClick={() =>
                    run(
                      async () => {
                        await api(`/channels/${id}/actions`, 'POST', { action: 'unarchive' });
                        refresh();
                        onChange();
                      },
                      t('已取消归档，渠道已恢复到未归档列表。', 'Channel restored to the Not archived list.'),
                    )
                  }
                >
                  {t('取消归档', 'Unarchive')}
                </button>
              )}
              <Tooltip
                content={
                  !deletable.length
                    ? t(
                        '没有可删除的远端渠道。归档需要所有远端分发均已确认停用。',
                        'No remote channel to delete. Archiving requires all remote distributions to be confirmed disabled.',
                      )
                    : undefined
                }
              >
                <button
                  className="button secondary danger-text"
                  disabled={busy || loading || actionsPending || d.archived || !deletable.length}
                  onClick={() => openDeleteDialog(deletable)}
                >
                  <Trash2 size={15} />
                  {t('删除渠道', 'Delete channel')}
                </button>
              </Tooltip>
            </div>
          </>
        )}
      </DataState>
      {edit && (
        <RecordForm
          title={t('编辑渠道', 'Edit channel')}
          initial={{ ...d, models: (d.models || []).join('\n') }}
          onClose={() => setEdit(false)}
          fields={[
            { name: 'remark', label: t('备注', 'Remark') },
            { name: 'declaration', label: t('号况申报', 'Declaration'), type: 'textarea' },
            {
              name: 'models',
              label: t('模型范围（每行一个）', 'Models (one per line)'),
              type: 'textarea',
              readonly: d.upload_mode === 'template',
              hint:
                d.upload_mode === 'template'
                  ? t('模型由各站点模板分别配置。', 'Models are configured by each site template.')
                  : undefined,
            },
          ]}
          onSave={async (v) => {
            const payload = { ...v };
            if (d.upload_mode === 'template') delete payload.models;
            else
              payload.models = v.models
                .split(/[\n,]/)
                .map((x: string) => x.trim())
                .filter(Boolean);
            await api(`/channels/${id}`, 'PATCH', payload);
            refresh();
            onChange();
          }}
        />
      )}
      {rotate && (
        <RecordForm
          title={
            d.key_mode === 'multiple'
              ? t('更换整组凭据', 'Rotate all credentials')
              : t('更换凭据', 'Rotate credential')
          }
          description={
            <Notice kind="warning">
              {d.key_mode === 'multiple' && (
                <p>
                  {t(
                    `请按原顺序填写全部 ${d.key_count} 条新凭据，保持各条认证方式和资源对应。`,
                    `Enter all ${d.key_count} replacement credentials in their original order, preserving authentication and resource assignments.`,
                  )}
                </p>
              )}
              {canAccessSection(user?.role, 'tasks')
                ? t(
                    '将创建新的 Key 版本。旧版本及历史消耗保留，远端更新可在任务中心追踪。',
                    'Creates a new key version and preserves history. Track remote updates in Tasks.',
                  )
                : t(
                    '将创建新的凭据版本，保留历史记录。提交后请刷新渠道详情查看分发状态。',
                    'Creates a new credential version and preserves history. Refresh channel details to view distribution status.',
                  )}
            </Notice>
          }
          onClose={() => setRotate(false)}
          fields={[
            {
              name: 'key',
              label: t('新凭据', 'New credential'),
              type:
                d.key_mode === 'multiple' ||
                d?.credential_format?.json ||
                d?.credential_format?.input_mode === 'auto'
                  ? 'textarea'
                  : 'password',
              required: true,
              hint: d?.credential_format?.help,
            },
          ]}
          onSave={async (v) => {
            const r = await api(`/channels/${id}/rotate`, 'POST', v);
            if (r.task_id || r.task?.id) setTask(r.task_id || r.task.id);
            setKey('');
            refresh();
            onChange();
          }}
        />
      )}
      {action && (
        <Confirm
          title={t('确认渠道操作', 'Confirm channel action')}
          onClose={() => setAction('')}
          onConfirm={async () => {
            const r = await api(`/channels/${id}/actions`, 'POST', { action });
            if (r.id && r.kind) setTask(r.id);
            else if (r.task_id) setTask(r.task_id);
            refresh();
            onChange();
          }}
        >
          <p>
            {t('操作', 'Action')}: <strong>{action}</strong> · {t('本地渠道', 'Local channel')} #
            {d.display_id ?? '—'}
          </p>
          <Notice>
            {t(
              '操作针对当前渠道的授权分发站点。已完成的远端结果和历史记录会保留。',
              'Applies to authorized distributions of this channel. Completed remote results and history are preserved.',
            )}
          </Notice>
        </Confirm>
      )}
      {deleteTargets && (
        <DeleteChannelDialog
          channelId={id}
          displayId={d.display_id}
          targets={deleteTargets}
          onClose={() => setDeleteTargets(null)}
          onSubmitted={(taskId) => {
            setTask(taskId);
            refresh();
            onChange();
          }}
        />
      )}
      {reuploadTarget && (
        <ReuploadChannelDialog
          channelId={id}
          target={reuploadTarget}
          onClose={() => setReuploadTarget(null)}
          onSubmitted={(taskId) => submittedCheck(reuploadTarget.id, 'reupload', taskId)}
        />
      )}
      {forceDeleteTarget && (
        <ForceDeleteDialog
          channelId={id}
          target={forceDeleteTarget}
          onClose={() => setForceDeleteTarget(null)}
          onSubmitted={(result) => {
            onRemoteCleanup(result);
          }}
        />
      )}
      {task && (
        <TaskModal
          id={task}
          onClose={() => {
            setTask('');
            refresh();
            onChange();
          }}
        />
      )}
    </Modal>
  );
}

function canOpenChannelTest(channel: Row): boolean {
  return (
    !!channel.connectivity_test?.task_id ||
    (!channel.archived && channel.local_test_available === true && channel.local_test_models?.length > 0)
  );
}

function canArchiveChannel(channel?: Row): boolean {
  return !channel?.archived && channel?.archive_available === true && !hasPendingChannelOperations(channel);
}

function hasPendingChannelOperations(channel?: Row): boolean {
  return (
    CHECK_RUNNING.includes(channel?.connectivity_test?.status) ||
    (!!channel?.force_delete_task && !DELETE_FINISHED.includes(channel.force_delete_task.status)) ||
    (channel?.distributions || []).some(
      (row: Row) =>
        CHECK_RUNNING.includes(row.connectivity_test?.status) ||
        (!row.locally_deleted &&
          row.status !== 'deleted' &&
          (CHECK_RUNNING.includes(row.usage_sync?.status) ||
            CHECK_RUNNING.includes(row.upload_status) ||
            CHECK_RUNNING.includes(row.reupload_task?.status))),
    )
  );
}

type DistributionSiteOption = { id: string; name: string };

function DistributionSitePicker({
  owner,
  value,
  refreshVersion,
  onChange,
}: {
  owner: string;
  value: DistributionSiteOption | null;
  refreshVersion: number;
  onChange: (value: DistributionSiteOption | null) => void;
}) {
  const { t } = useApp();
  const { data, loading, error, refresh } = useData<{ items: DistributionSiteOption[] }>(
    `/channel-distribution-sites?${query({ owner_id: owner })}`,
  );
  const revision = useRef(refreshVersion);
  useEffect(() => {
    if (revision.current !== refreshVersion) {
      revision.current = refreshVersion;
      refresh();
    }
  }, [refreshVersion, refresh]);
  const pending = loading || (!data && !error);
  const options = pending || error ? [] : data?.items || [];
  return (
    <>
      <Select
        aria-label={t('站点', 'Site')}
        className="compact-select"
        value={value?.id || ''}
        disabled={pending}
        onChange={(event) => onChange(options.find((option) => option.id === event.target.value) || null)}
      >
        <option value="">{pending ? t('加载站点…', 'Loading sites…') : t('全部站点', 'All sites')}</option>
        {value && !options.some((option) => option.id === value.id) && (
          <option value={value.id}>{value.name}</option>
        )}
        {options.map((option) => (
          <option key={option.id} value={option.id}>
            {option.name}
          </option>
        ))}
      </Select>
      {error && (
        <span className="muted" role="alert">
          {t('站点加载失败。', 'Could not load sites. ')}
          <button type="button" className="text-button" onClick={refresh}>
            {t('重试', 'Retry')}
          </button>
        </span>
      )}
    </>
  );
}

export function Channels() {
  const { t, user, notify } = useApp();
  const [params, setParams] = useSearchParams();
  const owner = params.get('owner_id') || '';
  const ownerName = params.get('owner_name') || shortId(owner);
  const [search, setSearch] = useState('');
  const settledSearch = useDebouncedValue(search.trim());
  const [view, setView] = useState<'channels' | 'distributions'>('channels');
  const [siteSelection, setSiteSelection] = useState<{
    owner: string;
    value: DistributionSiteOption;
  } | null>(null);
  const site = siteSelection?.owner === owner ? siteSelection.value : null;
  const siteId = view === 'distributions' ? site?.id || '' : '';
  const [archiveStatus, setArchiveStatus] = useState<'all' | 'active' | 'archived'>('active');
  const archived = archiveStatus === 'all' ? 'all' : archiveStatus === 'archived';
  const filterKey = query({ owner_id: owner, archived });
  const [serviceSelection, setServiceSelection] = useState<{
    filterKey: string;
    value: ChannelCategoryOption;
  } | null>(null);
  const service = serviceSelection?.filterKey === filterKey ? serviceSelection.value : null;
  const category = service?.category_id || '';
  const variant = service?.variant || '';
  const pageKey = JSON.stringify([view, filterKey, category, variant, siteId, settledSearch]);
  const [page, setPage] = useState({ key: '', offset: 0 });
  const offset = page.key === pageKey ? page.offset : 0;
  const setOffset = (value: number) => setPage({ key: pageKey, offset: value });
  const changeView = (value: 'channels' | 'distributions') => {
    setView(value);
    setPage({ key: '', offset: 0 });
    setSelected([]);
  };
  const [categoryRevision, setCategoryRevision] = useState(0);
  const [detail, setDetail] = useState('');
  const [archiveTarget, setArchiveTarget] = useState<Row | null>(null);
  const archiveSubmitting = useRef(false);
  const [channelTest, setChannelTest] = useState<Row | null>(null);
  const [cleanupRequest, setCleanupRequest] = useState<{ actorId: string; task: Row } | null>(null);
  const [cleanupOpen, setCleanupOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [bulk, setBulk] = useState('');
  const { run, busy } = useAction();
  const listRefreshInterval =
    selected.length || detail || channelTest || archiveTarget || bulk || cleanupOpen || busy ? 0 : 30000;
  const { data, error, loading, refresh } = useData(
    view === 'channels'
      ? `/channels?${query({ owner_id: owner, category_id: category, variant, search: settledSearch, archived, offset, limit: 50 })}`
      : null,
    listRefreshInterval,
  );
  const distributions = useData(
    view === 'distributions'
      ? `/channel-distributions?${query({ owner_id: owner, site_id: siteId, category_id: category, variant, search: settledSearch, archived, offset, limit: 50 })}`
      : null,
    listRefreshInterval,
  );
  const visibleData = view === 'distributions' ? distributions.data : data;
  const visibleLoading = view === 'distributions' ? distributions.loading : loading;
  const visibleError = view === 'distributions' ? distributions.error : error;
  useEffect(() => {
    if (visibleLoading || visibleError || !visibleData) return;
    const total = visibleData.total;
    if (Number.isInteger(total) && total >= 0 && offset > 0 && offset >= total)
      setPage({ key: pageKey, offset: Math.max(0, Math.floor((total - 1) / 50) * 50) });
  }, [visibleData, visibleLoading, visibleError, offset, pageKey]);
  const rows = items(data);
  const channelNumber = (id: string) => rows.find((row) => row.id === id)?.display_id ?? '—';
  const archiveReason = (channel?: Row) =>
    channel?.archive_reason ||
    (hasPendingChannelOperations(channel)
      ? t(
          '渠道仍有操作正在进行，请完成后刷新再归档。',
          'Channel operations are in progress. Refresh after they finish before archiving.',
        )
      : '') ||
    t(
      '只有所有远端分发均已确认停用的渠道才可归档，请先停用并刷新状态。',
      'All remote distributions must be confirmed disabled before archiving. Disable them and refresh their status.',
    );
  const blockedArchiveChannels = selected
    .map((id) => ({ id, channel: rows.find((row) => row.id === id) }))
    .filter(({ channel }) => !canArchiveChannel(channel));
  const bulkArchiveReason = blockedArchiveChannels.length
    ? t(
        `所选 ${blockedArchiveChannels.length} 条渠道暂不能归档，请取消选择或先处理以下原因：`,
        `${blockedArchiveChannels.length} selected channels cannot be archived. Deselect them or resolve these issues:`,
      ) +
      '\n' +
      blockedArchiveChannels
        .map(({ id, channel }) => `${channelNumber(id)}: ${archiveReason(channel)}`)
        .join('\n')
    : '';
  useEffect(() => setSelected([]), [pageKey, view]);
  useEffect(() => {
    setServiceSelection((current) => (current?.filterKey === filterKey ? current : null));
  }, [filterKey]);
  useEffect(() => {
    setSiteSelection((current) => (current?.owner === owner ? current : null));
  }, [owner]);
  const refreshLists = useCallback(() => {
    setSelected([]);
    refresh();
    distributions.refresh();
    setCategoryRevision((current) => current + 1);
  }, [refresh, distributions.refresh]);
  const openChannelTest = (row: Row) =>
    run(async () => {
      const channel = await api(`/channels/${row.id}`);
      if (channel.id !== row.id || !Array.isArray(channel.local_test_models))
        throw new Error(
          t('渠道信息无法确认，请刷新后重试。', 'Could not confirm channel details. Refresh and retry.'),
        );
      if (!canOpenChannelTest(channel))
        throw new Error(
          channel.local_test_reason ||
            t(
              '仅可测试已确认上传的 Key，请先完成上传。',
              'Only confirmed uploaded keys can be tested. Complete the upload first.',
            ),
        );
      setChannelTest(channel);
    });
  return (
    <Page
      title={
        owner
          ? t(`${ownerName} 的渠道`, `${ownerName}'s channels`)
          : t(
              user?.role !== 'user' ? '渠道管理' : '我的渠道',
              user?.role !== 'user' ? 'Channel management' : 'My channels',
            )
      }
      subtitle={t(
        '一条本地渠道，多个站点分发。凭据归属与历史消耗始终关联。',
        'One local channel, multiple remote distributions. Ownership and usage history stay connected.',
      )}
      actions={
        user?.role !== 'superadmin' && (
          <Link className="button" to="/upload">
            <Plus size={16} />
            {t('上传密钥', 'Upload keys')}
          </Link>
        )
      }
    >
      {owner && (
        <Notice>
          <div className="scope-banner">
            <span>
              {t('当前查看账号：', 'Viewing account: ')}
              <strong>{ownerName}</strong>
            </span>
            <button
              className="text-button"
              onClick={() => {
                const next = new URLSearchParams(params);
                next.delete('owner_id');
                next.delete('owner_name');
                setParams(next);
              }}
            >
              {t('返回全部可见渠道', 'Return to all visible channels')}
              <X size={14} />
            </button>
          </div>
        </Notice>
      )}
      <div className="channel-view-toolbar">
        <div className="channel-view-switch" role="group" aria-label={t('渠道视图', 'Channel view')}>
          <button type="button" aria-pressed={view === 'channels'} onClick={() => changeView('channels')}>
            <KeyRound size={16} aria-hidden="true" />
            {t('标签分组', 'Tag groups')}
          </button>
          <button
            type="button"
            aria-pressed={view === 'distributions'}
            onClick={() => changeView('distributions')}
          >
            <List size={16} aria-hidden="true" />
            {t('列表详情', 'List details')}
          </button>
        </div>
        <div className="channel-view-actions">
          <span className="muted">{t('后台自动同步', 'Automatic background sync')}</span>
          <Refresh onClick={refreshLists} loading={visibleLoading} />
          <RemoteCleanupTasks
            key={user?.id}
            request={cleanupRequest?.actorId === user?.id ? cleanupRequest?.task || null : null}
            onOpenChange={setCleanupOpen}
          />
        </div>
      </div>
      <section className="panel">
        <div className="panel-toolbar channel-list-toolbar">
          {view === 'distributions' && (
            <DistributionSitePicker
              key={owner}
              owner={owner}
              value={site}
              refreshVersion={categoryRevision}
              onChange={(value) => {
                setSiteSelection(value ? { owner, value } : null);
                setSelected([]);
                setOffset(0);
              }}
            />
          )}
          <ChannelCategoryPicker
            key={filterKey}
            queryString={filterKey}
            value={service}
            refreshVersion={categoryRevision}
            onChange={(value) => {
              setServiceSelection(value ? { filterKey, value } : null);
              setSelected([]);
              setOffset(0);
            }}
          />
          <Select
            aria-label={t('渠道状态', 'Channel status')}
            className="compact-select"
            value={archiveStatus}
            onChange={(event) => {
              setArchiveStatus(event.target.value as 'all' | 'active' | 'archived');
              setSelected([]);
              setOffset(0);
            }}
          >
            <option value="all">{t('全部', 'All')}</option>
            <option value="active">{t('未归档', 'Not archived')}</option>
            <option value="archived">{t('已归档', 'Archived')}</option>
          </Select>
          <label className="search-input">
            <Search size={16} aria-hidden="true" />
            <input
              aria-label={t('搜索渠道', 'Search channels')}
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setSelected([]);
              }}
              placeholder={
                view === 'distributions'
                  ? t(
                      '搜索上传用户、分组、站点或远端渠道…',
                      'Search uploader, group, site or remote channel…',
                    )
                  : t('搜索上传用户、渠道备注、分组或编号…', 'Search uploader, remark, group or ID…')
              }
            />
          </label>
          {view === 'channels' && (
            <a
              className="button secondary channel-usage-export"
              href={`/api/usage/export?${query({ owner_id: owner, category_id: category, variant, archived })}`}
            >
              <Download size={15} />
              {t('导出消耗', 'Export usage')}
            </a>
          )}
        </div>
        {view === 'channels' && selected.length > 0 && (
          <div className="bulk-toolbar">
            <strong>{t(`已选 ${selected.length} 条渠道`, `${selected.length} channels selected`)}</strong>
            {['test', 'enable', 'disable', 'archive'].map((a) => (
              <Tooltip key={a} content={a === 'archive' ? bulkArchiveReason : undefined}>
                <button
                  className="text-button"
                  disabled={a === 'archive' && (busy || loading || !!bulkArchiveReason)}
                  onClick={() => setBulk(a)}
                >
                  {
                    (
                      {
                        test: t('批量测试', 'Test selected'),
                        enable: t('批量启用', 'Enable selected'),
                        disable: t('批量停用', 'Disable selected'),
                        archive: t('批量归档', 'Archive selected'),
                      } as Row
                    )[a]
                  }
                </button>
              </Tooltip>
            ))}
            <button className="text-button" onClick={() => setSelected([])}>
              {t('清空', 'Clear')}
            </button>
          </div>
        )}
        <DataState loading={visibleLoading} error={visibleError}>
          {view === 'distributions' ? (
            !distributions.error && (
              <ChannelDistributionTable rows={items(distributions.data)} onDetails={setDetail} />
            )
          ) : (
            <Table
              rows={rows}
              pageSize={50}
              hidePagination
              columns={[
                {
                  label: (
                    <input
                      type="checkbox"
                      aria-label="Select all channels"
                      checked={rows.length > 0 && selected.length === rows.length}
                      onChange={(e) => setSelected(e.target.checked ? rows.map((r) => r.id) : [])}
                    />
                  ),
                  render: (r) => (
                    <input
                      type="checkbox"
                      aria-label={`Select ${r.display_id ?? '—'}`}
                      checked={selected.includes(r.id)}
                      onChange={(e) =>
                        setSelected((v) => (e.target.checked ? [...v, r.id] : v.filter((x) => x !== r.id)))
                      }
                    />
                  ),
                },
                {
                  label: 'ID',
                  render: (r) => <span className="numeric">{r.display_id ?? '—'}</span>,
                },
                {
                  label: t('分组标签', 'Group tag'),
                  className: 'channel-group-tag-cell',
                  render: (r) => (
                    <button className="channel-link" onClick={() => setDetail(r.id)}>
                      <strong>{r.group_tag || r.group_name || shortId(r.group_id)}</strong>
                      {r.remark && <small>{r.remark}</small>}
                    </button>
                  ),
                },
                {
                  label: t('分类', 'Category'),
                  render: (r) => (
                    <strong>
                      {t(
                        r.service_name || r.category_name || '—',
                        r.service_name_en || r.service_name || r.category_name || '—',
                      )}
                    </strong>
                  ),
                },
                {
                  label: t('Key 数量', 'Key count'),
                  render: (r) => r.key_count ?? 1,
                },
                {
                  label: t('上传者', 'Uploader'),
                  render: (r) => r.owner_name || r.owner_username || shortId(r.owner_id),
                },
                {
                  label: t('平台侧渠道', 'Remote channels'),
                  render: (r) =>
                    r.remote_channels ??
                    r.remote_count ??
                    r.distributions?.filter((d: Row) => d.remote_id).length ??
                    '—',
                },
                {
                  label: t('总消耗金额', 'Total usage amount'),
                  render: (r) => <ChannelUsageTotal channel={r} />,
                },
                {
                  label: t('状态', 'Status'),
                  render: (r) => <Status value={r.archived ? 'archived' : r.status || 'active'} />,
                },
                {
                  label: t('创建时间', 'Created'),
                  render: (r) => datetime(r.created_at),
                },
                {
                  label: t('同步时间', 'Last synced'),
                  className: 'channel-sync-time-cell',
                  render: (r) => datetime(latestChannelSyncTime(r.distributions)),
                },
                {
                  label: t('操作', 'Actions'),
                  className: 'table-actions-cell',
                  render: (r) => (
                    <div className="row-actions">
                      <Tooltip
                        content={
                          canOpenChannelTest(r)
                            ? t(
                                '直连官方测试已上传的 Key',
                                'Test uploaded keys directly with the official API',
                              )
                            : r.local_test_reason ||
                              t('仅可测试已确认上传的 Key', 'Only confirmed uploaded keys can be tested')
                        }
                      >
                        <button
                          className="text-button"
                          disabled={busy || loading || !canOpenChannelTest(r)}
                          onClick={() => openChannelTest(r)}
                        >
                          <Play size={14} />
                          {t('测试', 'Test')}
                        </button>
                      </Tooltip>
                      <button className="text-button" onClick={() => setDetail(r.id)}>
                        {t('查看详情', 'Details')}
                        <ArrowRight size={14} />
                      </button>
                      {!r.archived && (
                        <Tooltip content={!canArchiveChannel(r) ? archiveReason(r) : undefined}>
                          <button
                            className="text-button danger-text"
                            disabled={busy || loading || !canArchiveChannel(r)}
                            onClick={() => setArchiveTarget(r)}
                          >
                            {t('归档', 'Archive')}
                          </button>
                        </Tooltip>
                      )}
                      {r.archived && (
                        <button
                          className="text-button"
                          disabled={busy || loading}
                          onClick={() =>
                            run(
                              async () => {
                                await api(`/channels/${r.id}/actions`, 'POST', { action: 'unarchive' });
                                refreshLists();
                              },
                              t(
                                '已取消归档，渠道已恢复到未归档列表。',
                                'Channel restored to the Not archived list.',
                              ),
                            )
                          }
                        >
                          {t('取消归档', 'Unarchive')}
                        </button>
                      )}
                    </div>
                  ),
                },
              ]}
            />
          )}
        </DataState>
        {!visibleError && (
          <TablePagination
            offset={offset}
            total={visibleData?.total ?? 0}
            limit={50}
            loading={visibleLoading || search.trim() !== settledSearch}
            onChange={setOffset}
          />
        )}
      </section>
      {channelTest && (
        <UploadedKeyTestDialog
          key={channelTest.id}
          channel={channelTest}
          onChannelUpdated={(channel) =>
            setChannelTest((current) => (current && current.id === channel.id ? channel : current))
          }
          onClose={() => {
            setChannelTest(null);
            refreshLists();
          }}
          onSubmitted={(taskId) => {
            setChannelTest(
              (current) =>
                current && {
                  ...current,
                  connectivity_test: { task_id: taskId, status: 'queued' },
                },
            );
            refreshLists();
            notify(t('测试已提交，结果将自动更新。', 'Test submitted. Results will update automatically.'));
          }}
        />
      )}
      {detail && (
        <ChannelDetails
          id={detail}
          onClose={() => setDetail('')}
          onChange={refreshLists}
          onRemoteCleanup={(task) => {
            setCleanupRequest({ actorId: user?.id || '', task });
            setDetail('');
            refreshLists();
            notify(
              t(
                '本地记录已删除，远端清理已交给后台处理。',
                'Local records deleted. Remote cleanup is queued in the background.',
              ),
            );
          }}
        />
      )}
      {archiveTarget && (
        <Confirm
          title={t('确认归档渠道', 'Confirm channel archive')}
          danger
          onClose={() => {
            if (!archiveSubmitting.current) setArchiveTarget(null);
          }}
          onConfirm={async () => {
            if (archiveSubmitting.current) return;
            archiveSubmitting.current = true;
            try {
              const channel = await api(`/channels/${archiveTarget.id}`);
              if (channel.id !== archiveTarget.id || !canArchiveChannel(channel)) {
                refreshLists();
                throw new Error(archiveReason(channel));
              }
              await api(`/channels/${archiveTarget.id}/actions`, 'POST', { action: 'archive' });
              refreshLists();
              notify(t('渠道已归档', 'Channel archived'));
            } finally {
              archiveSubmitting.current = false;
            }
          }}
        >
          <p>
            {t('本地渠道', 'Local channel')} <strong>#{archiveTarget.display_id ?? '—'}</strong>
            {archiveTarget.group_tag && <> · {archiveTarget.group_tag}</>}
          </p>
          <Notice>
            {t(
              '仅归档所有远端分发均已停用的渠道，提交时会再次核实远端状态。本地渠道记录与历史消耗保留。',
              'Only channels with all remote distributions disabled can be archived. Remote status is checked again on submission. Local channel records and usage history are retained.',
            )}
          </Notice>
        </Confirm>
      )}
      {bulk && (
        <Confirm
          title={
            bulk === 'archive'
              ? t('确认批量归档', 'Confirm bulk archive')
              : t('确认批量远端操作', 'Confirm bulk remote action')
          }
          danger={bulk === 'archive'}
          onClose={() => setBulk('')}
          onConfirm={async () => {
            if (bulk === 'archive') {
              if (!selected.length)
                throw new Error(t('请重新选择需要归档的渠道。', 'Select the channels to archive again.'));
              if (bulkArchiveReason) throw new Error(bulkArchiveReason);
              const latestChannels = await Promise.all(selected.map((id) => api(`/channels/${id}`)));
              const blocked = latestChannels.flatMap((channel, index) =>
                channel.id !== selected[index] || !canArchiveChannel(channel)
                  ? [`${channelNumber(selected[index])}: ${archiveReason(channel)}`]
                  : [],
              );
              if (blocked.length)
                throw new Error(
                  t(
                    '以下渠道不符合归档条件，本次批量归档未提交：',
                    'These channels cannot be archived. No archive requests were submitted:',
                  ) +
                    '\n' +
                    blocked.join('\n'),
                );
            }
            let success = 0;
            const failed: string[] = [];
            for (const id of selected) {
              try {
                await api(`/channels/${id}/actions`, 'POST', { action: bulk });
                success++;
              } catch (e) {
                failed.push(`${channelNumber(id)}: ${(e as Error).message}`);
              }
            }
            refreshLists();
            setSelected([]);
            notify(t(`已提交 ${success} 条渠道操作`, `${success} channel actions submitted`));
            if (failed.length) notify(failed.join('；'), true);
          }}
        >
          <p>
            {t(
              `对 ${selected.length} 条本地渠道执行 ${bulk} 操作。`,
              `${bulk} ${selected.length} local channels.`,
            )}
          </p>
          <div className="selected-ids">
            {selected.map((id) => (
              <span className="tag mono" key={id}>
                {channelNumber(id)}
              </span>
            ))}
          </div>
          <Notice kind="warning">
            {bulk === 'archive'
              ? t(
                  '仅归档所有远端分发均已停用的渠道，提交时会再次核实远端状态。本地渠道记录与历史消耗保留。所选渠道中有不符合条件的项时，请先取消选择或处理后重试。',
                  'Only channels with all remote distributions disabled can be archived. Remote status is checked again on submission. Local channel records and usage history are retained. Deselect ineligible channels or resolve their issues before retrying.',
                )
              : canAccessSection(user?.role, 'tasks')
                ? t(
                    '该操作可能影响对应站点上的渠道可用性。任务中心保留逐项结果。',
                    'This may change remote channel availability. Per-item results remain in Tasks.',
                  )
                : t(
                    '该操作可能影响对应站点上的渠道可用性。提交后请刷新渠道详情查看分发状态。',
                    'This may change remote channel availability. Refresh channel details to view distribution status.',
                  )}
          </Notice>
        </Confirm>
      )}
    </Page>
  );
}

export function Tasks() {
  const { t } = useApp();
  const { data, error, loading, refresh } = useData('/tasks');
  const [status, setStatus] = useState(''),
    [selected, setSelected] = useState('');
  const rows = items(data).filter((r) => !status || r.status === status);
  useEffect(() => {
    if (!items(data).some((r) => ['pending', 'queued', 'running'].includes(r.status))) return;
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [data, refresh]);
  return (
    <>
      <SystemSettingsHeader actions={<Refresh onClick={refresh} loading={loading} />} />
      <section className="panel system-settings-panel">
        <div className="panel-toolbar">
          <Select
            aria-label={t('任务状态', 'Task status')}
            className="compact-select"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            <option value="">{t('全部状态', 'All statuses')}</option>
            {[
              'pending',
              'queued',
              'running',
              'completed',
              'succeeded',
              'partial',
              'failed',
              'unknown',
              'needs_review',
              'cancelled',
            ].map((s) => (
              <option value={s} key={s}>
                {s}
              </option>
            ))}
          </Select>
        </div>
        <DataState loading={loading && !data} error={error}>
          <Table
            scrollLabel={t('任务记录', 'Task records')}
            resetScrollOnPageChange
            rows={rows}
            columns={[
              {
                label: t('任务编号', 'Task ID'),
                render: (r) => <strong className="mono">#{shortId(r.id)}</strong>,
              },
              {
                label: t('任务类型', 'Type'),
                render: (r) =>
                  r.kind === 'force_delete_async'
                    ? t('远端清理', 'Remote cleanup')
                    : r.kind === 'reupload'
                      ? t('重新上传', 'Reupload')
                      : r.kind || r.type,
              },
              { label: t('状态', 'Status'), render: (r) => <Status value={r.status} /> },
              {
                label: t('成功 / 总数', 'Success / Total'),
                render: (r) =>
                  `${r.succeeded_count ?? r.success_count ?? r.completed_items ?? r.counts?.succeeded ?? r.counts?.success ?? '—'} / ${r.total_items ?? r.item_count ?? r.total_count ?? r.total ?? '—'}`,
              },
              { label: t('创建时间', 'Created'), render: (r) => datetime(r.created_at) },
              { label: t('最近更新', 'Updated'), render: (r) => datetime(r.updated_at || r.finished_at) },
              {
                label: t('操作', 'Actions'),
                className: 'table-actions-cell',
                render: (r) => (
                  <div className="row-actions">
                    <button type="button" onClick={() => setSelected(r.id)}>
                      <Eye size={14} />
                      {t('查看任务', 'View task')}
                    </button>
                  </div>
                ),
              },
            ]}
          />
        </DataState>
      </section>
      {selected &&
        (items(data).find((row) => row.id === selected)?.kind === 'force_delete_async' ? (
          <RemoteCleanupTaskDialog
            key={selected}
            id={selected}
            onClose={() => setSelected('')}
            onUpdated={refresh}
          />
        ) : (
          <TaskModal id={selected} onClose={() => setSelected('')} />
        ))}
    </>
  );
}
