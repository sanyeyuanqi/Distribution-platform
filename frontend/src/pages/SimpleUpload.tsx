import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  ArrowRight,
  CheckCircle2,
  ChevronRight,
  Info,
  ListTodo,
  LoaderCircle,
  Settings2,
  Tag,
  UploadCloud,
} from 'lucide-react';
import {
  api,
  ApiError,
  DataState,
  items,
  nonce,
  Notice,
  Page,
  Refresh,
  Status,
  Table,
  useApp,
  useData,
} from '../core';
import type { Row } from '../core';
import Tooltip from '../components/Tooltip';
import { credentialSummary } from '../components/UploadConfiguration';
import UploadAdvancedOptions, {
  emptyUploadAdvanced,
  uploadAdvancedError,
  uploadModelChoices,
} from '../components/UploadAdvancedOptions';
import type { UploadAdvancedValues } from '../components/UploadAdvancedOptions';
import {
  catalogRows,
  categoryAppearance,
  categoryFamily,
  formatLabel,
  googleUploadFormats,
  googleUploadType,
} from '../catalog';
import { canAccessSection } from '../permissions';
import { TaskModal } from './Channels';
import { apiAwsUrlError, awsCredentialKind, awsUploadFormats } from '../aws-upload';
import './upload-workbench.css';

const categoryReady = (category?: Row) =>
  category?.variants?.length
    ? category.variants.some((variant: Row) => variant.ready === true)
    : category?.ready === true;

const formatVariant = (category: Row | undefined, formatId: string) =>
  category?.variants?.find(
    (variant: Row) =>
      variant.format_id === formatId || variant.formats?.some((format: Row) => format.id === formatId),
  );

const firstReadyFormat = (category?: Row) => {
  const source = category?.variants?.length
    ? category.variants.find((variant: Row) => variant.ready === true)
    : categoryReady(category)
      ? category
      : undefined;
  return source?.format_id || source?.formats?.[0]?.id || '';
};

const formatReady = (category: Row | undefined, formatId: string) => {
  if (!categoryReady(category)) return false;
  if (category?.variants?.length) return formatVariant(category, formatId)?.ready === true;
  return category?.formats?.some((format: Row) => format.id === formatId) === true;
};

function distributionPlan(result: Row): Row | null {
  return Number.isInteger(result.channel_count) && Number.isInteger(result.remote_channel_count)
    ? { channel_count: result.channel_count, remote_channel_count: result.remote_channel_count }
    : null;
}

function uploadIssueMessages(value: unknown): string[] {
  return (Array.isArray(value) ? value : [value])
    .filter((message): message is string => typeof message === 'string')
    .map((message) => message.trim())
    .filter(Boolean);
}

function uploadRowReasons(row: Row): string[] {
  const reasons = uploadIssueMessages(row.reasons);
  return [...new Set(reasons.length ? reasons : uploadIssueMessages(row.message))];
}

function uploadBlockingReasons(preview: Row | null, t: (zh: string, en: string) => string): string[] {
  if (!preview || preview.can_submit) return [];
  const reasons = [...uploadIssueMessages(preview.errors), ...uploadIssueMessages(preview.issues)];
  for (const target of preview.targets || []) {
    if (target.compatible !== false) continue;
    const name =
      typeof target.name === 'string' && target.name.trim()
        ? target.name.trim()
        : t('接收站点', 'Receiving site');
    const issues = uploadIssueMessages(target.issues);
    reasons.push(
      ...(issues.length ? issues : [t('当前无法接收此批密钥。', 'Cannot receive this batch.')]).map(
        (issue) => `${name}：${issue}`,
      ),
    );
  }
  for (const row of preview.rows || []) {
    if (!['invalid', 'conflict'].includes(row.status)) continue;
    const line = Number(row.line);
    const prefix =
      Number.isSafeInteger(line) && line > 0
        ? t(`第 ${line} 行`, `Line ${line}`)
        : t('凭据问题', 'Credential issue');
    const issues = uploadRowReasons(row);
    reasons.push(
      ...(issues.length
        ? issues
        : [t('凭据未通过校验，请检查后重试。', 'Credential validation failed. Review and retry.')]
      ).map((issue) => `${prefix}：${issue}`),
    );
  }
  return [...new Set(reasons)];
}

export default function SimpleUpload() {
  const { t, user } = useApp();
  const canViewTasks = canAccessSection(user?.role, 'tasks');
  const [search] = useSearchParams();
  const options = useData('/uploads/options');
  const catalog = useData('/categories');
  const [category, setCategory] = useState(search.get('category_id') || '');
  const [credentials, setCredentials] = useState('');
  const [mode, setMode] = useState('batch');
  const [formatId, setFormatId] = useState('');
  const [apiBaseUrl, setApiBaseUrl] = useState('');
  const [advanced, setAdvanced] = useState<UploadAdvancedValues>(emptyUploadAdvanced);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [batchToken, setBatchToken] = useState(nonce);
  const [label, setLabel] = useState('');
  const [labelError, setLabelError] = useState('');
  const [labelLoading, setLabelLoading] = useState(false);
  const [labelRevision, setLabelRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState('');
  const [error, setError] = useState('');
  const [preview, setPreview] = useState<Row | null>(null);
  const [task, setTask] = useState('');
  const [showTask, setShowTask] = useState(false);
  const [submittedCount, setSubmittedCount] = useState(0);
  const [submittedTag, setSubmittedTag] = useState('');
  const [submittedPlan, setSubmittedPlan] = useState<Row | null>(null);
  const key = useRef(nonce());
  const pending = useRef<Row | null>(null);
  const pendingCount = useRef(0);
  const pendingPlan = useRef<Row | null>(null);
  const running = useRef(false);
  const appliedCategory = useRef('');
  const feedback = useRef<HTMLDivElement>(null);
  const categories = catalogRows(items(options.data), 'category_name');
  const selected = categories.find((c) => c.category_id === category);
  const selectedCatalog = items(catalog.data).find((c) => c.id === category);
  const isAws = categoryFamily(selected) === 'AWS';
  const isGoogle = categoryFamily(selected) === 'Google';
  const hasVariants = !!selected?.variants?.length;
  const availableFormats: Row[] = hasVariants
    ? [
        ...(selected?.formats || []),
        ...(selected?.variants || []).flatMap((variant: Row) => variant.formats || []),
      ]
    : selected?.formats || [];
  const rawFormats: Row[] = availableFormats.length
    ? availableFormats
    : (selectedCatalog?.formats || []).filter((f: Row) => f.enabled);
  const categoryFormats = [...new Map(rawFormats.map((format) => [format.id, format])).values()];
  const displayFormats = isAws
    ? awsUploadFormats(categoryFormats)
    : isGoogle
      ? googleUploadFormats(categoryFormats)
      : categoryFormats;
  const selectedFormat =
    displayFormats.find((f) => f.id === (formatId || firstReadyFormat(selected) || selected?.format_id)) ||
    (!selected?.format_id ? displayFormats[0] : undefined);
  const selectedVariant: Row | undefined = formatVariant(selected, selectedFormat?.id || '');
  const uploadDefaults = selectedVariant?.defaults || selected?.defaults;
  const selectionReady = !!selectedFormat && formatReady(selected, selectedFormat.id);
  const isAwsClaude = isAws && awsCredentialKind(selectedFormat) === 'aws_claude';
  const isBedrock = isAws && awsCredentialKind(selectedFormat) === 'aws_bedrock';
  const defaultRegion = (selectedVariant || selected)?.default_region || 'us-east-1';
  const baseUrlRequired = isAwsClaude && (selectedVariant || selected)?.api_base_url_required !== false;
  const baseUrlError = isAwsClaude ? apiAwsUrlError(apiBaseUrl, baseUrlRequired) : '';
  const autoCredentials = selectedFormat?.input_mode === 'auto';
  const jsonCredentials = selectedFormat
    ? !!selectedFormat.json ||
      selectedFormat.input_mode === 'json' ||
      (autoCredentials && /^[\s]*[\[{]/.test(credentials))
    : !!selected?.credential_json;
  const summary = credentialSummary(credentials, autoCredentials ? 'auto' : jsonCredentials);
  const lineCount = summary.count;
  const modelScope = hasVariants ? selectedVariant : selected;
  const activeModels: string[] = modelScope?.models || [];
  const sourceModels: string[] = [
    ...new Set<string>(activeModels.length ? activeModels : modelScope?.configured_models || []),
  ];
  const modelChoices = uploadModelChoices(sourceModels);
  const hasReceivingModels = activeModels.length > 0;
  const configuredModelsOnly = !activeModels.length && sourceModels.length > 0;
  const configuredModelsNotice = configuredModelsOnly
    ? t(
        '模型已配置，当前暂无启用的接收模板。',
        'Models are configured, but no receiving template is currently enabled.',
      )
    : '';
  const emptyModelMessage =
    sourceModels.length && !modelChoices.length
      ? t(
          '当前模板未配置这 9 个 Claude 模型，请联系管理员。',
          'The requested nine Claude models are not configured in these templates. Contact an administrator.',
        )
      : '';
  const unavailableMessage =
    configuredModelsNotice ||
    emptyModelMessage ||
    t(
      '当前暂无可用的接收模板，请联系管理员。',
      'No receiving template is currently available. Contact an administrator.',
    );
  const advancedError = uploadAdvancedError(advanced, modelChoices);
  const hasAdvanced =
    advanced.models !== null ||
    advanced.inventory ||
    !!advanced.remarks ||
    !!advanced.proxies ||
    advanced.accountFields.length > 0;
  const closeAdvanced = useCallback(() => setAdvancedOpen(false), []);
  const blockingReasons = [...new Set([...uploadIssueMessages(error), ...uploadBlockingReasons(preview, t)])];

  const invalidate = () => {
    if (pending.current) setBatchToken(nonce());
    key.current = nonce();
    pending.current = null;
    pendingCount.current = 0;
    pendingPlan.current = null;
    setError('');
    setPreview(null);
  };
  const chooseCategory = (value: string) => {
    const next = categories.find((c) => c.category_id === value);
    if (value === category || !categoryReady(next) || options.loading || busy) return;
    invalidate();
    setCategory(value);
    setFormatId(firstReadyFormat(next));
    if (
      ['AWS', 'Azure', 'Google'].includes(categoryFamily(selected)) ||
      ['AWS', 'Azure', 'Google'].includes(categoryFamily(next))
    )
      setCredentials('');
    setApiBaseUrl('');
    setAdvanced(emptyUploadAdvanced());
    setBatchToken(nonce());
    setMode(next?.defaults?.default_upload_mode || next?.default_upload_mode || 'batch');
  };
  const chooseFormat = (format: Row) => {
    if (selectedFormat?.id === format.id || !formatReady(selected, format.id) || options.loading || busy)
      return;
    invalidate();
    setFormatId(format.id);
    if (hasVariants) {
      const variant = formatVariant(selected, format.id);
      setMode(variant?.defaults?.default_upload_mode || variant?.default_upload_mode || 'batch');
      setCredentials('');
      setApiBaseUrl('');
      setAdvanced(emptyUploadAdvanced());
      setBatchToken(nonce());
    }
  };
  useEffect(() => {
    if (!options.data || options.loading || options.error || busy || pending.current) return;
    const current = categories.find((item) => item.category_id === category);
    const next = categoryReady(current) ? current : categories.find(categoryReady);
    const nextCategory = next?.category_id || '';
    const currentFormat = formatId || firstReadyFormat(next);
    const nextFormat = formatReady(next, currentFormat) ? currentFormat : firstReadyFormat(next);
    if (category === nextCategory && formatId === nextFormat) return;
    const selectionChanged = category !== nextCategory || (formatId && formatId !== nextFormat);
    setCategory(nextCategory);
    setFormatId(nextFormat);
    if (selectionChanged) {
      invalidate();
      setCredentials('');
      setApiBaseUrl('');
      setAdvanced(emptyUploadAdvanced());
      setAdvancedOpen(false);
      setBatchToken(nonce());
    }
    const defaults = formatVariant(next, nextFormat)?.defaults || next?.defaults;
    setMode(defaults?.default_upload_mode || next?.default_upload_mode || 'batch');
  }, [category, formatId, options.data, options.loading, options.error, busy]);
  useEffect(() => {
    if (error || (preview && !preview.can_submit)) {
      feedback.current?.scrollIntoView({ block: 'nearest' });
    }
  }, [error, preview]);
  useEffect(() => {
    if (!selected || appliedCategory.current === category) return;
    appliedCategory.current = category;
    setMode(uploadDefaults?.default_upload_mode || selected.default_upload_mode || 'batch');
  }, [category, selected]);
  useEffect(() => {
    if (!category || !selectedFormat?.id) {
      setLabel('');
      setLabelError('');
      setLabelLoading(false);
      return;
    }
    let cancelled = false;
    setLabel('');
    setLabelError('');
    setLabelLoading(true);
    const query = new URLSearchParams({
      category_id: category,
      format_id: selectedFormat.id,
      batch_token: batchToken,
    });
    api('/uploads/label?' + query.toString())
      .then((result) => {
        if (!result.group_tag) throw new Error('自动标签生成失败，请重试。');
        if (!cancelled) setLabel(result.group_tag);
      })
      .catch((reason) => {
        if (!cancelled)
          setLabelError(reason instanceof Error ? reason.message : '自动标签生成失败，请重试。');
      })
      .finally(() => {
        if (!cancelled) setLabelLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [category, selectedFormat?.id, batchToken, labelRevision]);
  const refresh = () => {
    if (running.current) return;
    options.refresh();
    catalog.refresh();
    setLabelRevision((n) => n + 1);
    setError('');
    setPreview(null);
  };
  const submit = async () => {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setError('');
    try {
      if (!pending.current) {
        setPreview(null);
        if (!selected) throw new Error(t('请选择渠道分类。', 'Choose a category.'));
        if (advancedError) throw new Error(advancedError);
        if (!selectionReady || !hasReceivingModels || !modelChoices.length)
          throw new Error(unavailableMessage);
        if (baseUrlError) throw new Error(baseUrlError);
        if (!label)
          throw new Error(t('请等待自动标签生成，或点击重试。', 'Wait for the generated label or retry.'));
        setPhase(t('正在检查密钥…', 'Checking keys…'));
        const body = {
          category_id: category,
          batch_token: batchToken,
          credentials,
          upload_mode: mode,
          ...(selectedFormat?.id ? { format_id: selectedFormat.id } : {}),
          ...(isAwsClaude && apiBaseUrl.trim() ? { api_base_url: apiBaseUrl.trim() } : {}),
          models: advanced.models ?? modelChoices,
          ...(advanced.inventory ? { inventory: true } : {}),
          ...(advanced.remarks ? { remarks: advanced.remarks } : {}),
          ...(advanced.proxies ? { proxies: advanced.proxies } : {}),
          ...(advanced.accountFields.length
            ? {
                account_info: Object.fromEntries(
                  advanced.accountFields.map((name) => [name, advanced.accountInfo[name] ?? null]),
                ),
              }
            : {}),
        };
        const result = await api('/uploads/simple-preview', 'POST', body);
        setPreview(result);
        if (!result.can_submit) return;
        if (!result.configuration_revision || result.group_tag !== label)
          throw new Error(
            t('分发配置或标签已变化，请刷新后重试。', 'Settings or the label changed. Refresh and retry.'),
          );
        pending.current = {
          ...body,
          configuration_revision: result.configuration_revision,
          idempotency_key: key.current,
        };
        pendingCount.current = result.valid_count ?? lineCount ?? 0;
        pendingPlan.current = distributionPlan(result);
      }
      setPhase(t('正在提交…', 'Submitting…'));
      const result = await api('/uploads/simple-submit', 'POST', pending.current);
      setTask(result.id);
      setSubmittedTag(result.group_tag || label);
      setSubmittedCount(pendingCount.current);
      setSubmittedPlan(pendingPlan.current);
      setShowTask(false);
      setCredentials('');
      setAdvanced(emptyUploadAdvanced());
      pending.current = null;
      pendingCount.current = 0;
      pendingPlan.current = null;
      key.current = nonce();
      setPreview(null);
      setBatchToken(nonce());
    } catch (reason) {
      if (reason instanceof ApiError && (reason.status === 409 || reason.status === 422)) {
        pending.current = null;
        key.current = nonce();
        setPreview(reason.detail?.preview || null);
        options.refresh();
        setLabelRevision((n) => n + 1);
      }
      setError(
        reason instanceof TypeError && /fetch|network|load failed/i.test(reason.message)
          ? pending.current
            ? t(
                '网络请求失败，暂时无法确认提交结果。请检查网络后点击“重试提交”，系统会使用同一批次重试。',
                'The network request failed and the submission result is unconfirmed. Check your connection and retry the same batch.',
              )
            : t(
                '网络请求失败，尚未提交分发任务。请检查网络后重试。',
                'The network request failed before distribution was submitted. Check your connection and retry.',
              )
          : reason instanceof Error
            ? reason.message
            : t('提交失败，请重试。', 'Submission failed. Retry.'),
      );
    } finally {
      running.current = false;
      setBusy(false);
      setPhase('');
    }
  };

  return (
    <Page
      title={t('上传 API 密钥', 'Upload API keys')}
      subtitle={t(
        '选择分类并粘贴密钥即可提交。系统自动生成标签，并按站点模板完成分发。',
        'Choose a category and paste keys. Labels and distribution are handled automatically.',
      )}
      actions={
        <>
          <Refresh onClick={refresh} loading={options.loading || busy} />
          {canViewTasks && (
            <Link className="button secondary" to="/settings/tasks">
              <ListTodo size={16} />
              {t('上传任务', 'Upload tasks')}
            </Link>
          )}
        </>
      }
    >
      <DataState loading={options.loading && !options.data} error={options.error}>
        {task && (
          <div className="simple-upload-success upload-workbench-success" role="status">
            <CheckCircle2 size={21} />
            <div>
              <strong>{t('上传已提交，后台正在处理', 'Submitted for background processing')}</strong>
              <p>
                {t('已接收 ' + submittedCount + ' 条凭据。', submittedCount + ' credentials received.')}{' '}
                <span className="mono">{submittedTag}</span>
              </p>
              {submittedPlan && (
                <p>
                  {t(
                    `计划归入 ${submittedPlan.channel_count} 个本地渠道，新增 ${submittedPlan.remote_channel_count} 个远端渠道；处理结果可在渠道详情逐行查看。`,
                    `Planned: ${submittedPlan.channel_count} local channels and ${submittedPlan.remote_channel_count} new remote channels. View each result in channel details.`,
                  )}
                </p>
              )}
            </div>
            {canViewTasks ? (
              <button type="button" className="button secondary" onClick={() => setShowTask(true)}>
                {t('查看进度', 'View progress')}
                <ArrowRight size={15} />
              </button>
            ) : (
              <Link className="button secondary" to="/channels">
                {t('查看我的渠道', 'View my channels')}
                <ArrowRight size={15} />
              </Link>
            )}
          </div>
        )}
        {!categories.length && options.data ? (
          <div className="template-empty">
            <Settings2 size={28} />
            <h2>{t('暂无上传分类', 'No upload categories')}</h2>
            <p>{t('请联系管理员配置上传模板。', 'Contact your administrator to configure templates.')}</p>
          </div>
        ) : (
          <form
            className="upload-workbench"
            onSubmit={(e) => {
              e.preventDefault();
              void submit();
            }}
          >
            <fieldset disabled={busy} className="template-fieldset upload-workbench-fields">
              <section className="panel upload-category-panel">
                <div className="upload-step-heading">
                  <span className="upload-step-number">1</span>
                  <div>
                    <h2 id="upload-category-title">{t('选择渠道分类', 'Choose a channel category')}</h2>
                    <p>{t('选择密钥对应的服务渠道', 'Choose the service for your keys')}</p>
                  </div>
                  {selected && <span className="upload-category-current">{selected.category_name}</span>}
                </div>
                <div
                  className="upload-provider-grid"
                  role="radiogroup"
                  aria-labelledby="upload-category-title"
                >
                  {categories.map((c) => (
                    <Tooltip
                      key={c.category_id}
                      content={
                        !categoryReady(c)
                          ? t('暂无可用接收模板', 'No available receiving template')
                          : undefined
                      }
                      triggerClassName="upload-provider-tooltip"
                    >
                      <button
                        type="button"
                        role="radio"
                        aria-label={c.category_name}
                        aria-checked={category === c.category_id && categoryReady(c)}
                        disabled={!categoryReady(c) || options.loading}
                        className={
                          'upload-provider-card ' +
                          (category === c.category_id && categoryReady(c) ? 'selected' : '')
                        }
                        style={
                          {
                            '--provider-color': categoryAppearance(categoryFamily(c))?.color,
                          } as CSSProperties
                        }
                        onClick={() => chooseCategory(c.category_id)}
                      >
                        <span className="upload-provider-badge" aria-hidden="true">
                          {c.category_name.slice(0, 1)}
                        </span>
                        <strong>{c.category_name}</strong>
                        <small>{categoryAppearance(categoryFamily(c))?.description}</small>
                      </button>
                    </Tooltip>
                  ))}
                </div>
                {options.data && !options.loading && !categories.some(categoryReady) && (
                  <p className="upload-receiving-notice" role="status">
                    {t(
                      '暂无可用的接收模板，请联系管理员启用。',
                      'No receiving template is available. Contact an administrator to enable one.',
                    )}
                  </p>
                )}
                {selected && (
                  <div
                    className="upload-format-pills"
                    role="group"
                    aria-label={t('密钥类型', 'Credential type')}
                  >
                    {displayFormats.map((f) => (
                      <Tooltip
                        key={f.id}
                        content={
                          !formatReady(selected, f.id)
                            ? t('等待管理员启用接收模板', 'Awaiting an enabled template')
                            : undefined
                        }
                      >
                        <button
                          type="button"
                          disabled={!formatReady(selected, f.id) || options.loading}
                          aria-pressed={selectedFormat?.id === f.id && formatReady(selected, f.id)}
                          className={
                            'upload-format-pill ' +
                            (selectedFormat?.id === f.id && formatReady(selected, f.id) ? 'selected' : '')
                          }
                          onClick={() => chooseFormat(f)}
                        >
                          {isGoogle ? googleUploadType(f) : formatLabel(f, displayFormats)}
                        </button>
                      </Tooltip>
                    ))}
                    {!displayFormats.length && (
                      <span className="muted">
                        {t('此分类尚未配置可用密钥格式', 'No credential format configured for this category')}
                      </span>
                    )}
                  </div>
                )}
              </section>
              <div className="upload-workbench-columns">
                <section className="panel upload-settings-panel">
                  <div className="upload-step-heading">
                    <span className="upload-step-number">2</span>
                    <div>
                      <h2>{t('上传设置', 'Upload settings')}</h2>
                      <p>{t('确认自动标签与可选配置', 'Review your label and optional settings')}</p>
                    </div>
                  </div>
                  <label className="upload-label-heading" htmlFor="upload-auto-label">
                    {t('自动生成标签', 'Generated label')}
                  </label>
                  <div className="upload-auto-label">
                    <Tag size={16} aria-hidden="true" />
                    <Tooltip content={label} mono>
                      <input
                        id="upload-auto-label"
                        value={label}
                        readOnly
                        aria-busy={labelLoading}
                        placeholder={
                          labelLoading
                            ? t('正在生成…', 'Generating…')
                            : t('选择分类后自动生成', 'Choose a category to generate a label')
                        }
                      />
                    </Tooltip>
                  </div>
                  {labelError && (
                    <div className="upload-label-error" role="alert">
                      <span>{labelError}</span>
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => setLabelRevision((n) => n + 1)}
                      >
                        {t('重试', 'Retry')}
                      </button>
                    </div>
                  )}
                  <button
                    type="button"
                    className="upload-advanced-button"
                    onClick={() => setAdvancedOpen(true)}
                    disabled={!selected}
                  >
                    <div>
                      <strong>{t('高级选项', 'Advanced options')}</strong>
                      <small>
                        {t('模型范围 · 号况 · 备注 · 代理', 'Models · Account · Remarks · Proxy')}
                      </small>
                    </div>
                    <span className={'tag ' + (hasAdvanced ? 'blue' : '')}>
                      {hasAdvanced ? t('已设置', 'Configured') : t('可选', 'Optional')}
                    </span>
                    <ChevronRight size={15} />
                  </button>
                  {hasAdvanced && (
                    <div className="upload-applied-options">
                      {advanced.models !== null && (
                        <p>{t('已选模型：', 'Selected models: ') + advanced.models.length}</p>
                      )}
                      {advanced.inventory && (
                        <p>{t('入库存：分发为停用渠道', 'Inventory: distribute as disabled channels')}</p>
                      )}
                      {advanced.remarks && <p>{t('已填写备注', 'Remarks configured')}</p>}
                      {advanced.proxies && <p>{t('已填写代理', 'Proxy configured')}</p>}
                      {advanced.accountFields.length > 0 && (
                        <p>{t('已填写号况', 'Account details configured')}</p>
                      )}
                    </div>
                  )}
                  {advancedError && !pending.current && (
                    <p className="danger-text" role="alert">
                      {advancedError}
                    </p>
                  )}
                </section>
                <section className="panel upload-credentials-panel">
                  <div className="upload-step-heading">
                    <span className="upload-step-number">3</span>
                    <div>
                      <h2>
                        <label htmlFor="simple-credentials">{t('填写密钥', 'Enter keys')}</label>{' '}
                        <span className="upload-count-badge">
                          {lineCount === null
                            ? t('待校验', 'Needs validation')
                            : t('已识别 ' + lineCount + ' 条', lineCount + ' recognized')}
                        </span>
                      </h2>
                      <p>
                        {t(
                          '批量粘贴密钥，系统会自动去重',
                          'Paste keys in bulk. Duplicates are removed automatically.',
                        )}
                      </p>
                    </div>
                    <div
                      className="segmented small upload-mode-switch"
                      aria-label={t('上传模式', 'Upload mode')}
                    >
                      <button
                        type="button"
                        className={mode === 'batch' ? 'selected' : ''}
                        aria-pressed={mode === 'batch'}
                        onClick={() => {
                          setMode('batch');
                          invalidate();
                        }}
                      >
                        {t('批量上传', 'Batch')}
                      </button>
                      <button
                        type="button"
                        className={mode === 'single' ? 'selected' : ''}
                        aria-pressed={mode === 'single'}
                        onClick={() => {
                          setMode('single');
                          invalidate();
                        }}
                      >
                        {t('单个上传', 'Single')}
                      </button>
                    </div>
                  </div>
                  {isAwsClaude && (
                    <div className="upload-api-address">
                      <label htmlFor="upload-api-base-url">API 地址（Base URL）</label>
                      <input
                        id="upload-api-base-url"
                        type="url"
                        required={baseUrlRequired}
                        value={apiBaseUrl}
                        maxLength={1000}
                        placeholder={
                          baseUrlRequired
                            ? 'https://<实际租户>.api.aws'
                            : t('留空使用模板中的地址', 'Leave empty to use template origins')
                        }
                        autoComplete="off"
                        spellCheck={false}
                        aria-describedby="upload-api-base-url-hint"
                        aria-invalid={!!apiBaseUrl && !!baseUrlError}
                        onChange={(event) => {
                          setApiBaseUrl(event.target.value);
                          invalidate();
                        }}
                      />
                      <p id="upload-api-base-url-hint">
                        {baseUrlRequired
                          ? t(
                              '仅支持 Anthropic 兼容租户代理。本批密钥共用此地址，只填写 HTTPS 域名，不添加路径。',
                              'Use an Anthropic-compatible tenant proxy. All keys share this HTTPS origin, without a path.',
                            )
                          : t(
                              '留空使用各站点模板中的地址；填写后，本批密钥共用此地址。仅支持 Anthropic 兼容租户代理的 HTTPS 域名。',
                              'Leave empty to inherit each template origin, or set one HTTPS Anthropic-compatible tenant proxy origin for this batch.',
                            )}
                      </p>
                      {!!apiBaseUrl && baseUrlError && (
                        <p className="danger-text" role="alert">
                          {baseUrlError}
                        </p>
                      )}
                    </div>
                  )}
                  <div className="upload-key-guide">
                    <Info size={18} />
                    <div>
                      <strong>
                        {autoCredentials
                          ? t(
                              '填写 API 密钥或完整 JSON 凭据',
                              'Enter an API key or complete JSON credentials',
                            )
                          : jsonCredentials
                            ? t('填写完整 JSON 凭据', 'Enter complete JSON credentials')
                            : t('每行一个密钥', 'One credential per line')}
                      </strong>
                      <p>
                        {selectedFormat?.help ||
                          t(
                            '按所选密钥格式填写；接收站点由后台自动安排。',
                            'Use the selected credential format. Destinations are selected automatically.',
                          )}
                      </p>
                      {isBedrock && (
                        <p>
                          {t(
                            `Region 可省略：临时 API Key 优先识别自带地区，其余自动补全 ${defaultRegion}；需要其他地区时请在密钥末尾填写 |Region。`,
                            `Region is optional: signed temporary API keys use their embedded region; other keys default to ${defaultRegion}. Append |Region to specify another region.`,
                          )}
                        </p>
                      )}
                    </div>
                  </div>
                  <textarea
                    id="simple-credentials"
                    className="credential-input upload-workbench-key"
                    required
                    rows={mode === 'batch' || jsonCredentials ? 12 : 6}
                    maxLength={200000}
                    spellCheck={false}
                    autoComplete="off"
                    autoCapitalize="none"
                    value={credentials}
                    onChange={(e) => {
                      setCredentials(e.target.value);
                      invalidate();
                    }}
                    placeholder={
                      selectedFormat?.placeholder ||
                      (jsonCredentials
                        ? t(
                            '粘贴完整 JSON 对象；批量支持数组或 JSONL',
                            'Paste a JSON object, an array or JSONL',
                          )
                        : t('在这里粘贴密钥，每行一条', 'Paste your keys here, one per line'))
                    }
                  />
                  <div className="upload-credential-summary">
                    <span>
                      {lineCount === null
                        ? t('JSON 将在提交前校验', 'JSON will be validated before submission')
                        : t(
                            '共 ' +
                              lineCount +
                              ' 条 · ' +
                              (lineCount - summary.duplicates) +
                              ' 条不同 · ' +
                              summary.duplicates +
                              ' 条重复',
                            lineCount +
                              ' total · ' +
                              (lineCount - summary.duplicates) +
                              ' unique · ' +
                              summary.duplicates +
                              ' duplicates',
                          )}
                    </span>
                    {options.data?.upload_limit && (
                      <span>
                        {t(
                          '每次最多 ' + options.data.upload_limit + ' 条',
                          'Up to ' + options.data.upload_limit + ' per upload',
                        )}
                      </span>
                    )}
                  </div>
                  {mode === 'single' && lineCount !== null && lineCount > 1 && (
                    <p className="danger-text" role="alert">
                      {t(
                        '单个上传只接受一条凭据，请切换批量上传。',
                        'Switch to batch mode to upload multiple credentials.',
                      )}
                    </p>
                  )}
                </section>
              </div>
              {(error || (preview && !preview.can_submit)) && (
                <div className="upload-feedback">
                  <div ref={feedback} role="alert">
                    <Notice kind="error">
                      <strong>
                        {t('暂时无法分发，请处理以下问题后重试。', 'Resolve these issues before retrying.')}
                      </strong>
                      <ul className="upload-blocking-reasons">
                        {(blockingReasons.length
                          ? blockingReasons
                          : [
                              t(
                                '未返回具体原因，请刷新后重试；仍失败时请联系管理员。',
                                'No specific reason was returned. Refresh and retry, or contact an administrator.',
                              ),
                            ]
                        ).map((issue) => (
                          <li key={issue}>{issue}</li>
                        ))}
                      </ul>
                    </Notice>
                  </div>
                  {preview && !preview.can_submit && !!preview.rows?.length && (
                    <section
                      className="panel upload-preview-errors"
                      aria-label={t('逐行检查结果', 'Per-line validation results')}
                    >
                      <Table
                        rows={preview.rows}
                        columns={[
                          { label: t('行', 'Line'), key: 'line' },
                          { label: t('密钥（脱敏）', 'Masked key'), key: 'key_hint' },
                          {
                            label: t('结果', 'Result'),
                            render: (row) => (
                              <div>
                                <Status value={row.status} />
                                {uploadRowReasons(row).map((message) => (
                                  <small key={message} className="block wrap-cell">
                                    {message}
                                  </small>
                                ))}
                              </div>
                            ),
                          },
                        ]}
                      />
                    </section>
                  )}
                </div>
              )}
            </fieldset>
            <section className="panel upload-submit-panel">
              {(!selectionReady || !hasReceivingModels || !modelChoices.length) && !pending.current && (
                <p className="upload-receiving-notice" role="status">
                  {unavailableMessage}
                </p>
              )}
              <button
                type="submit"
                className="button upload-submit-button"
                disabled={
                  busy ||
                  (!pending.current &&
                    (options.loading ||
                      !!options.error ||
                      !selected ||
                      !selectionReady ||
                      !hasReceivingModels ||
                      !modelChoices.length ||
                      !credentials.trim() ||
                      !!advancedError ||
                      !!baseUrlError ||
                      !label ||
                      labelLoading ||
                      (mode === 'single' && lineCount !== null && lineCount > 1)))
                }
              >
                {busy ? <LoaderCircle size={18} className="spin" /> : <UploadCloud size={18} />}
                {busy
                  ? phase
                  : pending.current
                    ? t('重试提交', 'Retry submission')
                    : mode === 'batch'
                      ? t('批量提交', 'Submit batch')
                      : t('提交密钥', 'Submit key')}
              </button>
            </section>
          </form>
        )}
      </DataState>
      {advancedOpen && (
        <UploadAdvancedOptions
          value={advanced}
          models={modelChoices}
          modelNotice={configuredModelsNotice}
          emptyModelMessage={emptyModelMessage}
          onClose={closeAdvanced}
          onApply={(value) => {
            setAdvanced(value);
            setAdvancedOpen(false);
            invalidate();
          }}
        />
      )}
      {showTask && task && <TaskModal id={task} onClose={() => setShowTask(false)} />}
    </Page>
  );
}
