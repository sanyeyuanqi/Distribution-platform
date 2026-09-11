import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import type { FormEvent, ReactNode } from 'react';
import { api, changeRequestSession } from './api';
import { createDataRequest, startDataPolling } from './data-request';
import type { DataUpdate } from './data-request';
export { api, ApiError } from './api';
import Select from './components/Select';
import DateTimePicker from './components/DateTimePicker';
import {
  AlertCircle,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Inbox,
  LoaderCircle,
  RefreshCw,
  X,
} from 'lucide-react';

export type Row = Record<string, any>;
export type User = {
  id: string;
  display_id: number;
  username: string;
  nickname: string;
  role: 'superadmin' | 'admin' | 'user';
  parent_id?: string;
};
type AppState = {
  user: User | null;
  ready: boolean;
  sessionRevision: number;
  setUser: (u: User | null) => void;
  lang: string;
  toggleLang: () => void;
  t: (zh: string, en: string) => string;
  notify: (message: string, error?: boolean) => void;
};
const AppContext = createContext<AppState>(null!);
export function AppProvider({ children }: { children: ReactNode }) {
  const [user, setUserState] = useState<User | null>(null);
  const [sessionRevision, setSessionRevision] = useState(0);
  const setUser = useCallback((next: User | null) => {
    changeRequestSession(next !== null);
    setUserState(next);
    setSessionRevision((revision) => revision + 1);
  }, []);
  const [ready, setReady] = useState(false);
  const [lang, setLang] = useState(localStorage.getItem('keyacross-language') || 'zh');
  const [toasts, setToasts] = useState<{ id: number; message: string; error: boolean }[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    api('/auth/me', 'GET', undefined, controller.signal)
      .then((r) => setUser(r.user))
      .catch(() => {})
      .finally(() => {
        if (!controller.signal.aborted) setReady(true);
      });
    const expire = () => setUser(null);
    window.addEventListener('session-expired', expire);
    return () => {
      controller.abort();
      window.removeEventListener('session-expired', expire);
    };
  }, [setUser]);
  useEffect(() => {
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
    localStorage.setItem('keyacross-language', lang);
  }, [lang]);
  const notify = useCallback((message: string, error = false) => {
    const id = Date.now() + Math.random();
    setToasts((v) => [...v, { id, message, error }]);
    setTimeout(() => setToasts((v) => v.filter((x) => x.id !== id)), 6500);
  }, []);
  return (
    <AppContext.Provider
      value={{
        user,
        sessionRevision,
        setUser,
        ready,
        lang,
        t: (zh, en) => (lang === 'zh' ? zh : en),
        toggleLang: () => setLang((x) => (x === 'zh' ? 'en' : 'zh')),
        notify,
      }}
    >
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map((x) => (
          <div className={`toast ${x.error ? 'error' : ''}`} key={x.id}>
            {x.error ? <AlertCircle size={18} /> : <CheckCircle2 size={18} />}
            <span>{x.message}</span>
            <button
              aria-label="Close"
              className="icon-button"
              onClick={() => setToasts((v) => v.filter((a) => a.id !== x.id))}
            >
              <X size={15} />
            </button>
          </div>
        ))}
      </div>
    </AppContext.Provider>
  );
}
export const useApp = () => useContext(AppContext);
export function useData<T = any>(path: string | null, refreshInterval = 0) {
  const { sessionRevision } = useApp();
  const request = useMemo(
    () => createDataRequest<T>(path ? (signal) => api<T>(path, 'GET', undefined, signal) : null),
    [path, sessionRevision],
  );
  const currentRequest = useRef<typeof request | null>(request);
  useLayoutEffect(() => {
    currentRequest.current = request;
  }, [request]);
  const snapshot = useSyncExternalStore(request.subscribe, request.getSnapshot, request.getSnapshot);
  // Keep these callbacks stable: task dialogs include them in polling/effect dependencies.
  const refresh = useCallback(() => {
    void currentRequest.current?.load();
  }, []);
  const setData = useCallback((value: DataUpdate<T>) => currentRequest.current?.setData(value), []);
  useEffect(() => {
    currentRequest.current = request;
    void request.load();
    return () => {
      request.cancel();
      if (currentRequest.current === request) currentRequest.current = null;
    };
  }, [request]);
  useEffect(() => {
    if (path && refreshInterval > 0) return startDataPolling(request, refreshInterval);
  }, [request, path, refreshInterval]);
  return { ...snapshot, setData, refresh };
}
export const items = (data: any): Row[] => (Array.isArray(data) ? data : data?.items || []);
export const money = (v: any, digits = 2) =>
  v === undefined || v === null
    ? '—'
    : Number(v).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
export const datetime = (v: any) =>
  v
    ? new Date(/[zZ]|[+-]\d\d:\d\d$/.test(String(v)) ? v : `${v}Z`).toLocaleString('zh-CN', {
        timeZone: 'Asia/Shanghai',
        hour12: false,
      })
    : '—';
export const shortId = (v: any) => String(v || '—').slice(0, 8);
export const query = (obj: Row) =>
  new URLSearchParams(
    Object.fromEntries(
      Object.entries(obj)
        .filter(([, v]) => v !== '' && v !== null && v !== undefined)
        .map(([k, v]) => [k, String(v)]),
    ),
  ).toString();
export const nonce = () => crypto.randomUUID();
export function decimalSum(values: any[]): string {
  const scale = 100000000n;
  const total = values.reduce<bigint>((sum, value) => {
    const raw = String(value ?? '0');
    const negative = raw.startsWith('-');
    const [whole, fraction = ''] = raw.replace(/^[+-]/, '').split('.');
    const amount = BigInt(whole || '0') * scale + BigInt(fraction.padEnd(8, '0').slice(0, 8));
    return sum + (negative ? -amount : amount);
  }, 0n);
  const amount = total < 0n ? -total : total;
  return `${total < 0n ? '-' : ''}${amount / scale}.${String(amount % scale).padStart(8, '0')}`.replace(
    /\.?0+$/,
    '',
  );
}
export function useAction() {
  const [busy, setBusy] = useState(false);
  const { notify, t } = useApp();
  const running = useRef(false);
  return {
    busy,
    run: async (fn: () => Promise<any>, message?: string) => {
      if (running.current) return;
      running.current = true;
      setBusy(true);
      try {
        const result = await fn();
        if (message) notify(message);
        return result;
      } catch (e) {
        notify(e instanceof Error ? e.message : t('操作失败', 'Action failed'), true);
        return undefined;
      } finally {
        setBusy(false);
        running.current = false;
      }
    },
  };
}
export function Page({
  title,
  subtitle,
  actions,
  children,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <>
      <div className="page-heading">
        <div className="page-heading-copy">
          <h1>{title}</h1>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {actions && <div className="actions">{actions}</div>}
      </div>
      {children}
    </>
  );
}
export function Refresh({ onClick, loading = false }: { onClick: () => void; loading?: boolean }) {
  const { t } = useApp();
  return (
    <button className="button secondary" onClick={onClick} disabled={loading}>
      <RefreshCw size={16} className={loading ? 'spin' : ''} />
      {t('刷新', 'Refresh')}
    </button>
  );
}
export function Loading() {
  const { t } = useApp();
  return (
    <div className="state-block">
      <LoaderCircle size={25} className="spin" />
      <span>{t('正在加载…', 'Loading…')}</span>
    </div>
  );
}
export function Empty({ text, action }: { text?: string; action?: ReactNode }) {
  const { t } = useApp();
  return (
    <div className="state-block empty">
      <div className="empty-icon">
        <Inbox size={30} />
      </div>
      <strong>{text || t('暂无数据', 'No data yet')}</strong>
      <span>
        {t(
          '完成配置或提交数据后，记录会显示在这里。',
          'Records will appear after configuration or submission.',
        )}
      </span>
      {action}
    </div>
  );
}
export function Notice({
  children,
  kind = 'info',
}: {
  children: ReactNode;
  kind?: 'info' | 'warning' | 'error' | 'success';
}) {
  return (
    <div className={`notice ${kind}`}>
      <AlertCircle size={17} />
      <div>{children}</div>
    </div>
  );
}
export function DataState({
  loading,
  error,
  children,
}: {
  loading: boolean;
  error: string;
  children: ReactNode;
}) {
  return (
    <>
      {error && <Notice kind="error">{error}</Notice>}
      {loading ? <Loading /> : children}
    </>
  );
}
export function Status({ value }: { value: any }) {
  const { t } = useApp();
  const key = String(value ?? 'unknown');
  const labels: Record<string, [string, string]> = {
    active: ['启用', 'Active'],
    enabled: ['已启用', 'Enabled'],
    disabled: ['已停用', 'Disabled'],
    remote_status_unknown: ['状态未知', 'Unknown status'],
    remote_deleted: ['远端已删除', 'Remote deleted'],
    upload_pending: ['待上传', 'Pending upload'],
    upload_running: ['上传中', 'Uploading'],
    upload_succeeded: ['上传成功', 'Uploaded'],
    upload_failed: ['上传失败', 'Upload failed'],
    upload_needs_review: ['待核实', 'Needs verification'],
    upload_cancelled: ['已取消', 'Cancelled'],
    upload_unknown: ['未确认', 'Unconfirmed'],
    pending: ['等待中', 'Pending'],
    queued: ['排队中', 'Queued'],
    running: ['执行中', 'Running'],
    success: ['成功', 'Success'],
    succeeded: ['成功', 'Succeeded'],
    completed: ['已完成', 'Completed'],
    failed: ['失败', 'Failed'],
    partial: ['部分成功', 'Partial'],
    unknown: ['待核实', 'Unknown'],
    needs_review: ['需核实', 'Needs review'],
    unsupported: ['未支持', 'Unsupported'],
    unverified: ['待验证', 'Unverified'],
    verified: ['已验证', 'Verified'],
    healthy: ['连接正常', 'Healthy'],
    unhealthy: ['连接异常', 'Unhealthy'],
    draft: ['草稿', 'Draft'],
    confirmed: ['待支付', 'Awaiting payment'],
    paid: ['已支付', 'Paid'],
    void: ['已作废', 'Void'],
    voided: ['已作废', 'Voided'],
    cancelled: ['已取消', 'Cancelled'],
    published: ['已发布', 'Published'],
    unpublished: ['已下架', 'Unpublished'],
    withdrawn: ['已下架', 'Withdrawn'],
    verification_failed: ['验证失败', 'Verification failed'],
    authentication_error: ['令牌失效', 'Invalid token'],
    identity_mismatch: ['身份不匹配', 'Identity mismatch'],
    permission_denied: ['权限不足', 'Permission denied'],
    rate_limited: ['请求限流', 'Rate limited'],
    connection_error: ['连接失败', 'Connection failed'],
    server_error: ['远端异常', 'Remote server error'],
    configuration_error: ['配置异常', 'Configuration error'],
    protocol_error: ['接口不兼容', 'Incompatible API'],
    business_error: ['验证被拒绝', 'Verification rejected'],
    remote_error: ['验证失败', 'Verification failed'],
    passed: ['测试通过', 'Passed'],
    untested: ['未测试', 'Not tested'],
    not_tested: ['未测试', 'Not tested'],
    not_synced: ['未同步', 'Not synced'],
    remote_snapshot: ['已同步原始数据', 'Raw data synced'],
    synced: ['已同步', 'Synced'],
    unavailable: ['不可用', 'Unavailable'],
    verified_sources: ['已核实', 'Verified sources'],
    pending_verification: ['待核实', 'Pending verification'],
    covered: ['已覆盖', 'Covered'],
    gap: ['存在缺口', 'Gap'],
    archived: ['已归档', 'Archived'],
    deleted: ['已删除', 'Deleted'],
    locally_deleted: ['已强制删除（本地）', 'Removed locally'],
    missing: ['远端未找到', 'Remote not found'],
    final_usage_unavailable: ['末次消耗不可用', 'Final usage unavailable'],
    valid: ['有效', 'Valid'],
    duplicate: ['重复已忽略', 'Duplicate'],
    already_distributed: ['已分发', 'Already distributed'],
    redistribute: ['补分发', 'Redistribute'],
    conflict: ['配置冲突', 'Conflict'],
    invalid: ['无效', 'Invalid'],
  };
  const tone = [
    'active',
    'enabled',
    'success',
    'succeeded',
    'upload_succeeded',
    'completed',
    'verified',
    'healthy',
    'passed',
    'synced',
    'paid',
    'covered',
    'valid',
    'published',
  ].includes(key)
    ? 'green'
    : [
          'failed',
          'upload_failed',
          'unhealthy',
          'conflict',
          'invalid',
          'authentication_error',
          'identity_mismatch',
          'permission_denied',
          'connection_error',
          'server_error',
          'configuration_error',
          'protocol_error',
          'business_error',
          'remote_error',
        ].includes(key)
      ? 'red'
      : [
            'pending',
            'queued',
            'running',
            'partial',
            'confirmed',
            'unverified',
            'unknown',
            'gap',
            'upload_pending',
            'upload_running',
            'upload_needs_review',
          ].includes(key)
        ? 'amber'
        : 'gray';
  return (
    <span className={`status ${tone}`}>
      <i />
      {labels[key] ? t(...labels[key]) : key}
    </span>
  );
}
export function Modal({
  title,
  subtitle,
  children,
  onClose,
  wide = false,
  footer,
  className = '',
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
  footer?: ReactNode;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    const handle = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      const dialogs = document.querySelectorAll('[role=dialog]');
      if (dialogs[dialogs.length - 1] !== ref.current) return;
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
      if (e.key === 'Tab' && ref.current) {
        const els = Array.from(
          ref.current.querySelectorAll<HTMLElement>(
            'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex]',
          ),
        ).filter(
          (element) =>
            element.tabIndex >= 0 && !element.matches(':disabled') && element.getClientRects().length > 0,
        );
        if (!els.length) return;
        const first = els[0],
          last = els[els.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', handle);
    ref.current?.focus();
    const before = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', handle);
      document.body.style.overflow = before;
      previous?.focus();
    };
  }, [onClose]);
  return (
    <div
      className="modal-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`modal ${wide ? 'wide' : ''} ${className}`}
      >
        <div className="modal-heading">
          <div>
            <h2>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button className="icon-button" aria-label="Close" onClick={onClose}>
            <X size={22} />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-footer">{footer}</div>}
      </div>
    </div>
  );
}
export type Column = { label: ReactNode; key?: string; render?: (row: Row) => ReactNode; className?: string };
export function Table({
  rows,
  columns,
  empty,
  pageSize = 15,
  scrollLabel,
  resetScrollOnPageChange = false,
  hidePagination = false,
  rowClassName,
}: {
  rows: Row[];
  columns: Column[];
  empty?: string;
  pageSize?: number;
  scrollLabel?: string;
  resetScrollOnPageChange?: boolean;
  hidePagination?: boolean;
  rowClassName?: (row: Row) => string | undefined;
}) {
  const { t } = useApp();
  const [page, setPage] = useState(1);
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => setPage(1), [rows]);
  useEffect(() => {
    if (resetScrollOnPageChange) scrollRef.current?.scrollTo({ top: 0 });
  }, [page, resetScrollOnPageChange]);
  if (!rows.length) return <Empty text={empty} />;
  const pages = Math.max(1, Math.ceil(rows.length / pageSize));
  return (
    <>
      <div
        ref={scrollRef}
        className="table-scroll"
        role={scrollLabel ? 'region' : undefined}
        aria-label={scrollLabel}
        tabIndex={scrollLabel ? 0 : undefined}
      >
        <table>
          <thead>
            <tr>
              {columns.map((col, i) => (
                <th key={i} className={col.className}>
                  {col.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.slice((page - 1) * pageSize, page * pageSize).map((row, i) => (
              <tr key={row.id || i} className={rowClassName?.(row)}>
                {columns.map((col, k) => (
                  <td key={k} className={col.className}>
                    {col.render ? col.render(row) : (row[col.key || ''] ?? '—')}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!hidePagination && (
        <div className="table-footer">
          <span>{t(`共 ${rows.length} 条记录`, `${rows.length} records`)}</span>
          <div className="actions">
            <button
              className="icon-button"
              aria-label="Previous page"
              disabled={page === 1}
              onClick={() => setPage((x) => x - 1)}
            >
              <ChevronLeft size={16} />
            </button>
            <span>
              {page} / {pages}
            </span>
            <button
              className="icon-button"
              aria-label="Next page"
              disabled={page >= pages}
              onClick={() => setPage((x) => x + 1)}
            >
              <ChevronRight size={16} />
            </button>
          </div>
        </div>
      )}
    </>
  );
}
export function Field({
  label,
  hint,
  children,
  className = '',
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={`field ${className}`}>
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
export type FormField = {
  name: string;
  label: string;
  className?: string;
  type?: string;
  required?: boolean;
  minLength?: number;
  maxLength?: number;
  hint?: string;
  options?: { value: string; label: string }[];
  value?: any;
  readonly?: boolean;
};
export function RecordForm({
  title,
  fields,
  initial = {},
  onClose,
  onSave,
  description,
  wide = false,
  fieldsClassName = 'form-stack',
}: {
  title: string;
  fields: FormField[];
  initial?: Row;
  onClose: () => void;
  onSave: (v: Row) => Promise<any>;
  description?: ReactNode;
  wide?: boolean;
  fieldsClassName?: string;
}) {
  const { t } = useApp();
  const { busy, run } = useAction();
  const [form, setForm] = useState<Row>(() =>
    Object.fromEntries(
      fields.map((f) => [f.name, initial[f.name] ?? f.value ?? (f.type === 'checkbox' ? false : '')]),
    ),
  );
  const [error, setError] = useState('');
  const setField = (name: string, value: any) => {
    setForm((current) => ({ ...current, [name]: value }));
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError('');
    for (const field of fields) {
      const value = form[field.name];
      if (typeof value !== 'string' || !value) continue;
      // Count Unicode characters consistently with the API, including pasted values.
      const length = Array.from(value).length;
      if (field.minLength !== undefined && length < field.minLength) {
        setError(
          `${field.label}：${t(`至少需要 ${field.minLength} 位`, `Use at least ${field.minLength} characters`)}`,
        );
        return;
      }
      if (field.maxLength !== undefined && length > field.maxLength) {
        setError(
          `${field.label}：${t(`最多允许 ${field.maxLength} 位`, `Use at most ${field.maxLength} characters`)}`,
        );
        return;
      }
    }
    await run(async () => {
      try {
        await onSave(form);
        onClose();
      } catch (e) {
        setError((e as Error).message);
        throw e;
      }
    });
  };
  return (
    <Modal title={title} onClose={onClose} wide={wide}>
      <form onSubmit={submit} className="form-stack">
        {description}
        {error && <Notice kind="error">{error}</Notice>}
        <div className={fieldsClassName}>
          {fields.map((f) => (
            <Field key={f.name} label={f.label} hint={f.hint} className={f.className}>
              {f.type === 'select' ? (
                <Select
                  value={form[f.name]}
                  required={f.required}
                  disabled={f.readonly}
                  onChange={(e) => setField(f.name, e.target.value)}
                >
                  <option value="">{t('请选择', 'Select…')}</option>
                  {f.options?.map((o) => (
                    <option value={o.value} key={o.value}>
                      {o.label}
                    </option>
                  ))}
                </Select>
              ) : f.type === 'textarea' || f.type === 'json' ? (
                <textarea
                  rows={f.type === 'json' ? 6 : 4}
                  value={
                    typeof form[f.name] === 'object' ? JSON.stringify(form[f.name], null, 2) : form[f.name]
                  }
                  required={f.required}
                  readOnly={f.readonly}
                  onChange={(e) => setField(f.name, e.target.value)}
                />
              ) : f.type === 'checkbox' ? (
                <input
                  type="checkbox"
                  checked={!!form[f.name]}
                  onChange={(e) => setField(f.name, e.target.checked)}
                />
              ) : f.type === 'datetime-local' ? (
                <DateTimePicker
                  value={form[f.name]}
                  locale={t('zh', 'en') as 'zh' | 'en'}
                  aria-label={f.label}
                  required={f.required}
                  readOnly={f.readonly}
                  onChange={(e) => setField(f.name, e.target.value)}
                />
              ) : (
                <input
                  autoComplete={f.type === 'password' ? 'new-password' : 'off'}
                  type={f.type || 'text'}
                  value={form[f.name]}
                  required={f.required}
                  readOnly={f.readonly}
                  step={f.type === 'number' ? 'any' : undefined}
                  onChange={(e) => setField(f.name, e.target.value)}
                />
              )}
            </Field>
          ))}
        </div>
        <div className="form-actions">
          <button type="button" className="button secondary" onClick={onClose}>
            {t('取消', 'Cancel')}
          </button>
          <button className="button" disabled={busy}>
            {busy ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />} {t('保存', 'Save')}
          </button>
        </div>
      </form>
    </Modal>
  );
}
export function Confirm({
  title,
  children,
  onClose,
  onConfirm,
  danger = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  onConfirm: () => Promise<any>;
  danger?: boolean;
}) {
  const { t } = useApp();
  const { run, busy } = useAction();
  return (
    <Modal title={title} onClose={onClose}>
      <div className="form-stack">
        {children}
        <div className="form-actions">
          <button className="button secondary" onClick={onClose}>
            {t('取消', 'Cancel')}
          </button>
          <button
            className={`button ${danger ? 'danger' : ''}`}
            disabled={busy}
            onClick={() =>
              run(async () => {
                await onConfirm();
                onClose();
              })
            }
          >
            {busy ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />} {t('确认', 'Confirm')}
          </button>
        </div>
      </div>
    </Modal>
  );
}
export function JsonDetails({ value }: { value: any }) {
  return <pre className="json-details">{JSON.stringify(value, null, 2)}</pre>;
}
export function ServerPager({
  offset,
  total,
  limit,
  onChange,
  loading,
}: {
  offset: number;
  total: number;
  limit: number;
  onChange: (n: number) => void;
  loading: boolean;
}) {
  const { t } = useApp();
  if (total <= limit) return null;
  return (
    <div className="server-pager">
      <span>
        {t(
          `服务器共 ${total} 条，当前 ${offset + 1}–${Math.min(total, offset + limit)} 条`,
          `${total} server records · Showing ${offset + 1}–${Math.min(total, offset + limit)}`,
        )}
      </span>
      <div className="actions">
        <button
          className="button secondary"
          disabled={loading || offset === 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          <ChevronLeft size={14} />
          {t('上一批', 'Previous batch')}
        </button>
        <button
          className="button secondary"
          disabled={loading || offset + limit >= total}
          onClick={() => onChange(offset + limit)}
        >
          {t('下一批', 'Next batch')}
          <ChevronRight size={14} />
        </button>
      </div>
    </div>
  );
}
