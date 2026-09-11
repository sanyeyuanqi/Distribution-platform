import Select from '../components/Select';
import ChannelCategoryPicker from '../components/ChannelCategoryPicker';
import type { ChannelCategoryOption } from '../components/ChannelCategoryPicker';
import Tooltip from '../components/Tooltip';
import PullToRefresh from '../components/PullToRefresh';
import RoutingGroupPicker, {
  routingGroupError,
  routingGroupMaxLength,
  routingGroups,
} from '../components/RoutingGroupPicker';
import { useCallback, useState } from 'react';
import ModelPicker, {
  normalizeModelIds,
  modelSelectionError,
  modelRequirementSelectionError,
  modelRequirementValue,
} from '../components/ModelPicker';
import ModelMappingEditor, {
  initialModelMapping,
  modelMappingResult,
  pruneModelMapping,
  sameModelMapping,
  type ModelMappingDraft,
} from '../components/ModelMappingEditor';
import { Link, useSearchParams } from 'react-router-dom';
import {
  ArrowRight,
  CheckCircle2,
  Edit3,
  Globe2,
  Layers3,
  LoaderCircle,
  Plus,
  Power,
  RefreshCw,
  RotateCcw,
  Trash2,
} from 'lucide-react';
import {
  api,
  Confirm,
  DataState,
  Field,
  items,
  Modal,
  Notice,
  Page,
  Refresh,
  Table,
  useAction,
  useApp,
  useData,
} from '../core';
import type { Row } from '../core';
import { catalogRows, formatLabel, googleUploadFormats, uploadServiceVariant } from '../catalog';
import { awsUploadFormats } from '../aws-upload';
import { siteVersionInfo } from '../site-verification';
import type { Site } from '../site-verification';
import { templateModelCandidates } from '../template-model-candidates';
import { sortModelsByReleaseDate } from '../model-release-order';

function formatServiceVariant(category: Row, format: Row): string {
  return typeof format.service_variant === 'string'
    ? format.service_variant
    : uploadServiceVariant(category, format);
}

function siteVerification(site: Row | undefined, row?: Row) {
  const error = row?.site_verification_error || site?.verification_error;
  const status = error
    ? 'failed'
    : row?.site_verification_status || (site?.verified_at ? 'verified' : 'unverified');
  return { status, message: typeof error === 'string' ? error : error?.message || '' };
}

function TemplateSiteVersion({
  site,
  row,
  t,
}: {
  site: Site | undefined;
  row: Row;
  t: (zh: string, en: string) => string;
}) {
  const info = siteVerification(site, row).status === 'verified' ? siteVersionInfo(site, t) : null;
  return (
    <Tooltip content={info?.fingerprint} mono>
      <span className="template-site-version">{info?.summary || t('待验证', 'Unverified')}</span>
    </Tooltip>
  );
}

function templateDisplayIssues(row: Row, site: Row | undefined, t: (zh: string, en: string) => string) {
  const verification = siteVerification(site, row);
  const primary =
    verification.status === 'failed'
      ? t('站点验证失败', 'Site verification failed') +
        (verification.message ? `：${verification.message}` : '')
      : verification.status === 'unverified'
        ? t('站点尚未验证，请先验证站点', 'Verify the site before checking compatibility')
        : '';
  const issues = (row.issues || [])
    .filter(
      (issue: string) =>
        !primary || (!issue.startsWith('站点验证失败') && issue !== '站点尚未验证，请先验证站点'),
    )
    .map((issue: string) => {
      if (issue === '站点已停用或归档' && site)
        return site.archived
          ? t('站点已归档', 'Site archived')
          : t('站点分发已关闭', 'Site distribution disabled');
      return issue;
    });
  return [...new Set<string>([...(primary ? [primary] : []), ...issues])];
}

function TemplateIssue({ issue, t }: { issue: string; t: (zh: string, en: string) => string }) {
  if (issue.length <= 90) return <small>{issue}</small>;
  return (
    <details className="template-issue-details">
      <summary>
        <span className="template-issue-preview">{issue.slice(0, 90)}…</span>
        <span className="template-issue-expand">{t('查看完整说明', 'Show full details')}</span>
        <span className="template-issue-collapse">{t('收起说明', 'Hide details')}</span>
      </summary>
      <small>{issue}</small>
    </details>
  );
}

function TemplateEditor({
  initial,
  sites,
  categories,
  formats,
  templates,
  onClose,
  onSaved,
}: {
  initial: Row;
  sites: Row[];
  categories: Row[];
  formats: Row[];
  templates: Row[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t, notify } = useApp();
  const initialSite = sites.find((s) => s.id === initial.site_id);
  const categoryFormatOptions = (categoryId: string) => {
    const available = formats.filter((format) => format.category_id === categoryId);
    const family = categories.find((item) => item.id === categoryId)?.family;
    if (family === 'Azure')
      return available.filter((format) => ['azure_gpt', 'azure_claude'].includes(format.schema_config?.type));
    if (family === 'Google') return googleUploadFormats(available);
    return family === 'AWS' ? awsUploadFormats(available) : available;
  };
  const initialFormats = categoryFormatOptions(initial.category_id).filter(
    (format) =>
      initial.id ||
      initial.service_variant === undefined ||
      formatServiceVariant(
        categories.find((category) => category.id === initial.category_id) || {},
        format,
      ) === initial.service_variant,
  );
  const initialFormat =
    (initial.id && initial.format_id
      ? formats.find((format) => format.id === initial.format_id) || {
          id: initial.format_id,
          category_id: initial.category_id,
          name: initial.format_name || '',
          enabled: true,
          schema_config: { type: initial.variant || '' },
        }
      : undefined) ||
    initialFormats.find((f) => f.id === initial.format_id) ||
    initialFormats.find((f) => f.code === initial.format_code) ||
    initialFormats.find((f) => f.enabled) ||
    initialFormats[0] ||
    null;
  const [form, setForm] = useState<Row>({
    site_id: initial.site_id || '',
    category_id: initial.category_id || '',
    format_id: initialFormat?.id || '',
    name: initial.name || '',
    routing_group: routingGroups(initial.routing_group || initialSite?.routing_group || 'default').join(','),
    remark: initial.remark || '',
  });
  const initialCategory = categories.find((category) => category.id === initial.category_id);
  const [selectedService, setSelectedService] = useState<ChannelCategoryOption | null>(() =>
    initial.category_id && initialFormat
      ? {
          category_id: initial.category_id,
          variant: initial.service_variant ?? formatServiceVariant(initialCategory || {}, initialFormat),
          name:
            initial.service_name ||
            initialFormat.service_name ||
            initialCategory?.name ||
            initial.category_name ||
            '',
          name_en:
            initial.service_name_en ||
            initialFormat.service_name_en ||
            initial.service_name ||
            initialFormat.service_name ||
            initialCategory?.name ||
            initial.category_name ||
            '',
          channel_count: 0,
          verified_usage_by_unit: {},
          data_status: 'empty',
        }
      : null,
  );
  const [models, setModels] = useState<string[]>(() =>
    normalizeModelIds(initial.models ?? (!initial.id ? initialFormat?.default_models : undefined) ?? []),
  );
  const [rpmRequirements, setRpmRequirements] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      models
        .filter((model) => Object.prototype.hasOwnProperty.call(initial.model_rpm_requirements || {}, model))
        .map((model) => [model, String(initial.model_rpm_requirements[model])]),
    ),
  );
  const [tpmRequirements, setTpmRequirements] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      models
        .filter((model) => Object.prototype.hasOwnProperty.call(initial.model_tpm_requirements || {}, model))
        .map((model) => [model, String(initial.model_tpm_requirements[model])]),
    ),
  );
  const [channelStatus, setChannelStatus] = useState(initial.channel_config?.status ?? 2);
  const [mappingDraft, setMappingDraft] = useState<ModelMappingDraft>(() =>
    initialModelMapping(initial.model_mapping || {}),
  );
  const [appliedMapping, setAppliedMapping] = useState<Record<string, string>>(initial.model_mapping || {});
  const [autoMappingModels, setAutoMappingModels] = useState<string[]>([]);
  const [mappingApplyError, setMappingApplyError] = useState('');
  const mappingResult = modelMappingResult(mappingDraft, t);
  const mappingPending = !mappingResult.error && !sameModelMapping(mappingResult.mapping, appliedMapping);
  const modelsError = modelSelectionError(models, t);
  const demandError =
    modelRequirementSelectionError(models, rpmRequirements, 'RPM', t) ||
    modelRequirementSelectionError(models, tpmRequirements, 'TPM', t);
  const [error, setError] = useState('');
  const { run, busy } = useAction();
  const close = useCallback(() => {
    if (!busy) onClose();
  }, [busy, onClose]);
  const site = sites.find((s) => s.id === form.site_id);
  const verification = siteVerification(site);
  const siteVerified = verification.status === 'verified';
  const categoryFormats = categoryFormatOptions(form.category_id);
  const serviceFormats = selectedService
    ? categoryFormats.filter(
        (format) =>
          formatServiceVariant(
            categories.find((category) => category.id === form.category_id) || {},
            format,
          ) === selectedService.variant,
      )
    : [];
  const selectedFormat = initial.id ? initialFormat : serviceFormats.find((f) => f.id === form.format_id);
  const siteAllowedModels = normalizeModelIds(siteVerified ? site?.template_options?.models || [] : []);
  const modelChoices = templateModelCandidates({
    category: categories.find((item) => item.id === form.category_id),
    format: selectedFormat || undefined,
    variant: initial.id ? initial.variant || '' : '',
    templates: templates.filter((template) => template.id !== initial.id),
    formats,
    sites,
  });
  const groupChoices: string[] = siteVerified ? site?.template_options?.groups || [] : [];
  const selectedGroups = routingGroups(form.routing_group);
  const groupMaxLength = routingGroupMaxLength(site?.template_options?.routing_group_max_length);
  const groupError = routingGroupError(selectedGroups, t, groupMaxLength);
  const invalidGroups = siteVerified ? selectedGroups.filter((group) => !groupChoices.includes(group)) : [];
  const invalidModels =
    !siteVerified || site?.template_options?.allow_custom_models === true
      ? []
      : models.filter((model) => !siteAllowedModels.includes(model));
  const ready = form.site_id && form.category_id && selectedFormat && !groupError;
  const set = (name: string, value: any) => {
    setForm((v) => ({ ...v, [name]: value }));
    setError('');
  };
  const changeType = (value: string, categoryId = form.category_id) => {
    const format = formats.find((item) => item.id === value);
    setForm((v) => ({
      ...v,
      category_id: format?.category_id || categoryId,
      format_id: format?.id || '',
    }));
    setModels(normalizeModelIds(format?.default_models || []));
    setRpmRequirements({});
    setTpmRequirements({});
    setMappingDraft(initialModelMapping());
    setAppliedMapping({});
    setAutoMappingModels([]);
    setMappingApplyError('');
    setError('');
  };
  const applyMapping = () => {
    if (busy || mappingResult.error) return;
    const sources = Object.keys(mappingResult.mapping);
    const removedAuto = autoMappingModels.filter((model) => !sources.includes(model));
    const nextModels = normalizeModelIds([
      ...models.filter((model) => !removedAuto.includes(model)),
      ...sources,
    ]);
    const invalid = modelSelectionError(nextModels, t);
    if (invalid) {
      setMappingApplyError(invalid);
      return;
    }
    setAutoMappingModels([
      ...new Set([
        ...autoMappingModels.filter((model) => sources.includes(model)),
        ...sources.filter((model) => !models.includes(model)),
      ]),
    ]);
    setModels(nextModels);
    setRpmRequirements((current) =>
      Object.fromEntries(Object.entries(current).filter(([model]) => nextModels.includes(model))),
    );
    setTpmRequirements((current) =>
      Object.fromEntries(Object.entries(current).filter(([model]) => nextModels.includes(model))),
    );
    setAppliedMapping(mappingResult.mapping);
    setMappingDraft(
      mappingDraft.mode === 'visual'
        ? initialModelMapping(mappingResult.mapping)
        : { mode: 'json', text: JSON.stringify(mappingResult.mapping, null, 2) },
    );
    setMappingApplyError('');
    setError('');
    notify(
      sources.some((model) => !models.includes(model))
        ? t('映射已应用，新模型已加入接收模型', 'Mapping applied. New models were added to Accepted models.')
        : t('映射已应用', 'Mapping applied'),
    );
  };
  const save = (enabled: boolean) =>
    run(async () => {
      setError('');
      try {
        if (modelsError) throw new Error(modelsError);
        if (demandError) throw new Error(demandError);
        if (mappingResult.error) throw new Error(mappingResult.error);
        if (mappingPending)
          throw new Error(t('请先应用映射，再保存模板。', 'Apply the mapping before saving the template.'));
        if (groupError) throw new Error(groupError);
        if (enabled && !siteVerified)
          throw new Error(
            t('请先完成站点验证，再启用模板。', 'Verify the site before enabling the template.'),
          );
        if (enabled && invalidGroups.length)
          throw new Error(
            t(
              '部分分组未通过本站验证，请移除或重新验证站点。',
              'Remove unverified groups or reverify the site before enabling.',
            ),
          );
        if (enabled && !models.length)
          throw new Error(t('启用模板前至少配置一个模型。', 'Configure at least one model before enabling.'));
        if (enabled && invalidModels.length)
          throw new Error(
            t(
              '部分模型未通过本站验证，请先修正或保存草稿。',
              'Some models are not verified for this site. Correct them or save a draft.',
            ),
          );
        const body: Row = {
          ...form,
          models,
          model_mapping: appliedMapping,
          model_rpm_requirements: Object.fromEntries(
            models
              .filter((model) => modelRequirementValue(rpmRequirements, model).trim() !== '')
              .map((model) => [model, Number(modelRequirementValue(rpmRequirements, model))]),
          ),
          model_tpm_requirements: Object.fromEntries(
            models
              .filter((model) => modelRequirementValue(tpmRequirements, model).trim() !== '')
              .map((model) => [model, Number(modelRequirementValue(tpmRequirements, model))]),
          ),
          enabled,
          name: form.name.trim(),
          routing_group: selectedGroups.join(','),
          channel_config: { status: channelStatus },
        };
        if (initial.id) {
          delete body.site_id;
          delete body.category_id;
          delete body.format_id;
        }
        await api(
          `/upload-templates${initial.id ? `/${initial.id}` : ''}`,
          initial.id ? 'PATCH' : 'POST',
          body,
        );
        notify(
          enabled
            ? t('分发模板已启用', 'Distribution template enabled')
            : t('模板已保存为停用草稿', 'Template saved as a disabled draft'),
        );
        onSaved();
        onClose();
      } catch (e) {
        setError(e instanceof Error ? e.message : t('保存失败', 'Save failed'));
      }
    });
  return (
    <Modal
      wide
      title={
        initial.id
          ? t('编辑分发模板', 'Edit distribution template')
          : t('新建分发模板', 'Create distribution template')
      }
      subtitle={t(
        '选择渠道分类后自动匹配凭据格式，再配置模型和渠道分组。',
        'Select a channel category to match its credential format, then configure models and routing groups.',
      )}
      onClose={close}
      footer={
        <>
          <button
            type="button"
            role="switch"
            className="template-channel-status-switch"
            aria-label={t('创建后的渠道状态', 'Initial channel status')}
            aria-checked={channelStatus === 1}
            disabled={busy}
            onClick={() => setChannelStatus((status: number) => (status === 1 ? 2 : 1))}
          >
            <span>{t('创建后的渠道状态', 'Initial channel status')}</span>
            <span className="template-channel-status-track" aria-hidden="true" />
            <span className="template-channel-status-value" aria-hidden="true">
              {channelStatus === 1 ? t('启用', 'Enabled') : t('停用', 'Disabled')}
            </span>
          </button>
          <div className="actions">
            <button
              className="button secondary"
              disabled={
                busy || !ready || !!modelsError || !!demandError || !!mappingResult.error || mappingPending
              }
              onClick={() => void save(false)}
            >
              {busy ? <LoaderCircle className="spin" size={15} /> : null}
              {t('保存草稿', 'Save draft')}
            </button>
            <button
              className="button"
              disabled={
                busy ||
                !ready ||
                !siteVerified ||
                !selectedFormat?.enabled ||
                !!modelsError ||
                !!demandError ||
                !!mappingResult.error ||
                mappingPending ||
                !models.length ||
                !!invalidModels.length ||
                !!invalidGroups.length
              }
              onClick={() => void save(true)}
            >
              {busy ? <LoaderCircle className="spin" size={15} /> : <CheckCircle2 size={15} />}
              {t('保存并启用', 'Save & enable')}
            </button>
          </div>
        </>
      }
    >
      <fieldset className="template-fieldset template-editor" disabled={busy}>
        {error && (
          <div role="alert">
            <Notice kind="error">{error}</Notice>
          </div>
        )}
        <div className="form-grid">
          <Field
            label={t('目标站点', 'Destination site')}
            hint={initial.id ? t('已创建模板的站点固定。', 'The site is fixed after creation.') : undefined}
          >
            <Select
              value={form.site_id}
              disabled={!!initial.id}
              onChange={(e) => {
                const next = sites.find((s) => s.id === e.target.value);
                setForm((v) => ({
                  ...v,
                  site_id: e.target.value,
                  routing_group: v.routing_group || next?.routing_group || 'default',
                }));
                setError('');
              }}
            >
              <option value="">{t('选择目标站点', 'Choose a site')}</option>
              {sites
                .filter((s) => !s.archived || s.id === initial.site_id)
                .map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                    {!s.enabled ? t(' · 分发已停用', ' · Distribution disabled') : ''}
                  </option>
                ))}
            </Select>
          </Field>
          <Field label={t('模板名称（可选）', 'Template name (optional)')}>
            <input
              maxLength={120}
              value={form.name}
              onChange={(e) => set('name', e.target.value)}
              placeholder={t('例如：OpenAI 标准接收', 'e.g. OpenAI standard intake')}
            />
          </Field>
          <Field label={t('渠道分类', 'Channel category')}>
            <ChannelCategoryPicker
              value={selectedService}
              refreshVersion={0}
              categoryIds={categories.map((category) => category.id)}
              required
              showUsage={false}
              placeholder={t('选择渠道分类', 'Select a channel category')}
              disabled={busy || !!initial.id}
              onChange={(service) => {
                if (
                  !service ||
                  (selectedService?.category_id === service.category_id &&
                    selectedService.variant === service.variant)
                )
                  return;
                const available = categoryFormatOptions(service.category_id).filter(
                  (format) =>
                    formatServiceVariant(
                      categories.find((category) => category.id === service.category_id) || {},
                      format,
                    ) === service.variant,
                );
                setSelectedService(service);
                changeType(
                  (available.find((format) => format.enabled) || available[0])?.id || '',
                  service.category_id,
                );
              }}
            />
          </Field>
          <Field
            label={t('凭据格式', 'Credential format')}
            hint={
              initial.id
                ? t(
                    '已创建模板的分类和凭据格式固定。',
                    'The category and credential format are fixed after creation.',
                  )
                : !selectedService
                  ? undefined
                  : serviceFormats.length > 1
                    ? t(
                        '已自动匹配，可切换此分类下的其他凭据格式。',
                        'Matched automatically. You can choose another format within this category.',
                      )
                    : selectedFormat
                      ? t('已根据渠道分类自动匹配。', 'Matched automatically to the channel category.')
                      : undefined
            }
          >
            {!initial.id && serviceFormats.length > 1 ? (
              <Select
                value={form.format_id}
                aria-label={t('凭据格式', 'Credential format')}
                disabled={busy}
                onChange={(event) => changeType(event.target.value)}
              >
                {serviceFormats.map((format) => (
                  <option key={format.id} value={format.id}>
                    {formatLabel(format, serviceFormats)}
                    {!categories.find((category) => category.id === form.category_id)?.active ||
                    !format.enabled
                      ? t(' · 已停用', ' · Disabled')
                      : ''}
                  </option>
                ))}
              </Select>
            ) : (
              <input
                className="template-credential-value"
                aria-label={t('凭据格式', 'Credential format')}
                readOnly
                value={
                  selectedFormat
                    ? formatLabel(selectedFormat, [selectedFormat]) ||
                      t('已保存凭据格式', 'Saved credential format')
                    : ''
                }
                placeholder={
                  selectedService
                    ? t('暂无匹配的凭据格式', 'No matching credential format')
                    : t('选择渠道分类后自动匹配', 'Select a channel category to match a format')
                }
              />
            )}
          </Field>
          {selectedFormat && (selectedFormat.placeholder || selectedFormat.help) && (
            <div
              className="template-credential-preview"
              role="status"
              aria-live="polite"
              aria-label={t('凭据填写示例', 'Credential example')}
            >
              <strong>{t('凭据填写示例', 'Credential example')}</strong>
              {selectedFormat.placeholder && <pre>{selectedFormat.placeholder}</pre>}
              {selectedFormat.help && <p>{selectedFormat.help}</p>}
            </div>
          )}
        </div>
        {!initial.id && selectedService && !serviceFormats.length && (
          <Notice>
            {t(
              '此分类暂无可选凭据格式，请选择其他分类。',
              'No credential formats are available for this category. Choose another category.',
            )}
          </Notice>
        )}
        {selectedFormat && !selectedFormat.enabled && (
          <Notice kind="warning">
            {t(
              '此凭据格式尚未开放上传。可以保存分类模板草稿，完成适配后再启用。',
              'This credential format is not available for uploads yet. Save a draft and enable it after integration is complete.',
            )}
          </Notice>
        )}
        {site && !siteVerified && (
          <Notice kind="warning">
            {verification.message && (
              <span>
                {t('站点验证失败：', 'Site verification failed: ')}
                {verification.message}{' '}
              </span>
            )}
            {t(
              '请先在站点管理完成连接验证，再核对模型和渠道分组。当前配置可以保存为草稿。',
              'Verify this site in Sites before checking models and channel groups. You can save a draft now.',
            )}{' '}
            <Link className="text-button" to="/sites">
              {t('前往站点管理', 'Open Sites')}
              <ArrowRight size={13} />
            </Link>
          </Notice>
        )}
        {site && !site.enabled && (
          <Notice>
            {t(
              '此站点的分发开关已停用。保存模板后，仍需在站点管理启用分发，用户才能上传到这里。',
              'Distribution is disabled for this site. Enable it in Sites when this template is ready to receive uploads.',
            )}
          </Notice>
        )}
        <div className="template-model-field">
          <div className="template-model-heading">
            <div>
              <h3>{t('接收模型', 'Accepted models')}</h3>
              <p>
                {t(
                  '按上传类型选择 Key 模型。各站点仅分发所选模型与上传密钥模型范围的交集，没有共同模型的站点会跳过。',
                  'Choose key models for this upload type. Each site receives the intersection with the uploaded key’s model scope; sites with no shared models are skipped.',
                )}
              </p>
            </div>
          </div>
          <ModelPicker
            key={form.format_id}
            value={models}
            choices={normalizeModelIds([...modelChoices, ...(selectedFormat?.default_models || [])])}
            disabled={busy}
            appliedMapping={appliedMapping}
            rpmRequirements={rpmRequirements}
            onRpmRequirementsChange={(value) => {
              setRpmRequirements(value);
              setError('');
            }}
            tpmRequirements={tpmRequirements}
            onTpmRequirementsChange={(value) => {
              setTpmRequirements(value);
              setError('');
            }}
            onChange={(value) => {
              setMappingDraft((current) =>
                pruneModelMapping(
                  current,
                  models.filter((model) => !value.includes(model)),
                ),
              );
              setAppliedMapping((current) =>
                Object.fromEntries(Object.entries(current).filter(([model]) => value.includes(model))),
              );
              setAutoMappingModels((current) => current.filter((model) => value.includes(model)));
              setMappingApplyError('');
              setModels(value);
              setError('');
            }}
            t={t}
          />
          <p className="template-model-hint">
            {!siteVerified
              ? t(
                  '站点验证完成后再核对模型支持情况。',
                  'Model support can be checked after site verification.',
                )
              : site?.template_options?.allow_custom_models === true
                ? t(
                    '已选模型可包含尚未列入站点目录的实际 API 模型 ID。',
                    'Selected models may include actual API model IDs not yet listed in the site catalog.',
                  )
                : t('模型 ID 须与本站支持的名称一致。', 'Use model IDs supported by this site.')}
          </p>
          {!modelsError && invalidModels.length > 0 && (
            <p className="template-model-warning">
              {t(
                '以下 ' + invalidModels.length + ' 个模型未通过本站验证，可先保存草稿：',
                invalidModels.length + ' models are not verified for this site. You can save a draft: ',
              )}
              {sortModelsByReleaseDate(invalidModels).slice(0, 6).join('、')}
              {invalidModels.length > 6 ? '…' : ''}
            </p>
          )}
          {!modelsError && !models.length && (
            <p className="muted">
              {t(
                '未选模型时可保存草稿，启用模板前至少选择一个有效模型。',
                'Save an empty selection as a draft. Select at least one valid model before enabling.',
              )}
            </p>
          )}
          <ModelMappingEditor
            value={mappingDraft}
            models={models}
            disabled={busy}
            pending={mappingPending}
            applyError={mappingApplyError}
            onApply={applyMapping}
            onChange={(value) => {
              setMappingDraft(value);
              setMappingApplyError('');
              setError('');
            }}
            t={t}
          />
        </div>
        <div className="form-grid">
          <RoutingGroupPicker
            value={form.routing_group}
            choices={groupChoices}
            verificationKnown={siteVerified}
            maxLength={groupMaxLength}
            disabled={busy}
            onChange={(value) => set('routing_group', value)}
            t={t}
          />
          <Field
            label={t('默认备注（可选）', 'Default remark (optional)')}
            hint={t('用户未填写逐行备注时自动使用。', 'Used when a user leaves the per-key remark empty.')}
          >
            <textarea
              maxLength={4000}
              rows={3}
              value={form.remark}
              onChange={(e) => set('remark', e.target.value)}
              placeholder={t('上传后自动带入的备注', 'Applied automatically on upload')}
            />
          </Field>
        </div>
      </fieldset>
    </Modal>
  );
}

export default function UploadTemplates() {
  const { t, notify } = useApp();
  const [search, setSearch] = useSearchParams();
  const siteFilter = search.get('site_id') || '';
  const requestedCategory = search.get('category_id') || '';
  const requestedVariant = search.get('variant');
  const templates = useData('/upload-templates');
  const sites = useData('/sites');
  const categories = useData('/categories');
  const formats = useData('/formats');
  const visibleCategories = catalogRows(items(categories.data));
  const categoryIds = new Set(visibleCategories.map((category) => category.id));
  const visibleFormats = items(formats.data).filter((format) => categoryIds.has(format.category_id));
  const categoryFilter = categoryIds.has(requestedCategory) ? requestedCategory : '';
  const variantFilter = categoryFilter ? requestedVariant : null;
  const [categoryRevision, setCategoryRevision] = useState(0);
  const [edit, setEdit] = useState<Row | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Row | null>(null);
  const [verifyingSiteId, setVerifyingSiteId] = useState('');
  const { run, busy } = useAction();
  const siteById = new Map(items(sites.data).map((site) => [site.id, site]));
  const siteTemplates = items(templates.data).filter(
    (r) => categoryIds.has(r.category_id) && (!siteFilter || r.site_id === siteFilter),
  );
  const rows = siteTemplates.filter(
    (r) =>
      (!categoryFilter || r.category_id === categoryFilter) &&
      (variantFilter === null || (r.service_variant ?? r.variant ?? '') === variantFilter),
  );
  const templateServices: ChannelCategoryOption[] = items(templates.data)
    .filter((row) => categoryIds.has(row.category_id) && row.service_name)
    .map((row) => ({
      category_id: row.category_id,
      variant: row.service_variant ?? row.variant ?? '',
      name: row.service_name,
      name_en: row.service_name_en || row.service_name,
      channel_count: 0,
      verified_usage_by_unit: {},
      data_status: 'empty',
    }));
  const categoryName = visibleCategories.find((category) => category.id === categoryFilter)?.name || '';
  const selectedService: ChannelCategoryOption | null = categoryFilter
    ? templateServices.find(
        (option) => option.category_id === categoryFilter && option.variant === variantFilter,
      ) || {
        category_id: categoryFilter,
        variant: variantFilter ?? '',
        name:
          variantFilter === null
            ? t(`${categoryName}（全部类型）`, `${categoryName} (all types)`)
            : categoryName,
        name_en: variantFilter === null ? `${categoryName} (all types)` : categoryName,
        channel_count: 0,
        verified_usage_by_unit: {},
        data_status: 'empty',
      }
    : null;
  const newTemplate = () =>
    setEdit({
      site_id: siteFilter,
      category_id: categoryFilter,
      service_variant: variantFilter ?? undefined,
    });
  const loading = templates.loading || sites.loading || categories.loading || formats.loading;
  const error = templates.error || sites.error || categories.error || formats.error;
  const refresh = () => {
    setCategoryRevision((version) => version + 1);
    templates.refresh();
    sites.refresh();
    categories.refresh();
    formats.refresh();
  };
  const verifySite = (siteId: string) =>
    run(async () => {
      setVerifyingSiteId(siteId);
      try {
        const result = await api<Site>(`/sites/${siteId}/verify`, 'POST');
        const success = siteVerification(result).status === 'verified';
        const version = siteVersionInfo(result, t);
        if (success) {
          notify(`${t('站点验证通过', 'Site verification passed')}${version ? ` · ${version.summary}` : ''}`);
        } else {
          const reason = result.verification_error?.message;
          notify(
            `${t('站点验证失败', 'Site verification failed')}：${reason || t('请在站点管理中查看详细原因。', 'See site management for details.')}`,
            true,
          );
        }
      } catch (reason) {
        notify(
          `${t('站点验证失败', 'Site verification failed')}：${reason instanceof TypeError ? t('无法连接服务，请检查网络后重试。', 'Could not connect. Check your network and retry.') : reason instanceof Error ? reason.message : t('请稍后重试。', 'Retry shortly.')}`,
          true,
        );
      } finally {
        sites.refresh();
        templates.refresh();
        setVerifyingSiteId('');
      }
    });
  const filter = (key: string, value: string) => {
    const next = new URLSearchParams(search);
    if (value) next.set(key, value);
    else next.delete(key);
    setSearch(next);
  };
  const filterService = (option: ChannelCategoryOption | null) => {
    const next = new URLSearchParams(search);
    if (option) {
      next.set('category_id', option.category_id);
      next.set('variant', option.variant);
    } else {
      next.delete('category_id');
      next.delete('variant');
    }
    setSearch(next);
  };
  const resetFilters = () => {
    const next = new URLSearchParams(search);
    next.delete('site_id');
    next.delete('category_id');
    next.delete('variant');
    setSearch(next);
  };
  return (
    <Page
      title={t('分发模板', 'Distribution templates')}
      subtitle={t(
        '先为站点配置渠道分类，用户只需粘贴密钥即可一键分发。',
        'Configure each site and category once. Users can then upload keys in one step.',
      )}
      actions={
        <>
          <Refresh onClick={refresh} loading={loading} />
          <button className="button" disabled={loading || !!error} onClick={newTemplate}>
            <Plus size={16} />
            {t('新建模板', 'Create template')}
          </button>
        </>
      }
    >
      <div className="template-filters">
        <Field label={t('站点', 'Site')}>
          <Select value={siteFilter} onChange={(e) => filter('site_id', e.target.value)}>
            <option value="">{t('全部站点', 'All sites')}</option>
            {items(sites.data).map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t('渠道分类', 'Category')}>
          <ChannelCategoryPicker
            value={selectedService}
            refreshVersion={categoryRevision}
            onChange={filterService}
            categoryIds={[...categoryIds]}
            extraOptions={templateServices}
            showUsage={false}
          />
        </Field>
        <button type="button" className="button template-filter-reset" onClick={resetFilters}>
          <RotateCcw size={16} />
          {t('重置', 'Reset')}
        </button>
        <span className="muted">
          {t(
            `${rows.length} 份模板 · ${rows.filter((r) => r.ready).length} 份可分发`,
            `${rows.length} templates · ${rows.filter((r) => r.ready).length} ready`,
          )}
        </span>
      </div>
      <PullToRefresh
        className="template-list-scroll"
        aria-label={t('分发模板列表', 'Distribution template list')}
        onRefresh={refresh}
        refreshing={loading}
        disabled={busy || !!edit}
      >
        <DataState
          loading={loading && (!templates.data || !sites.data || !categories.data || !formats.data)}
          error={error}
        >
          {!rows.length && !error ? (
            <div className="template-empty">
              <span className="template-empty-icon">
                <Layers3 size={27} />
              </span>
              <h2>{t('为这个分类准备接收站点', 'Prepare sites to receive this category')}</h2>
              <p>
                {t(
                  '新建模板，选择站点、分类和接收模型。未完成时保存草稿，准备好后再启用。',
                  'Choose a site, category and accepted models. Save a draft until it is ready.',
                )}
              </p>
              <button className="button" onClick={newTemplate}>
                <Plus size={16} />
                {t('新建模板', 'Create template')}
              </button>
            </div>
          ) : (
            <section className="panel template-table">
              <Table
                rows={rows}
                scrollLabel={t('分发模板表格', 'Distribution templates table')}
                rowClassName={(row) => (row.ready ? 'template-row-ready' : 'template-row-unavailable')}
                columns={[
                  {
                    label: 'ID',
                    className: 'template-col-id',
                    render: (r) => <span>{r.display_id ?? '—'}</span>,
                  },
                  {
                    label: t('名称', 'Name'),
                    className: 'template-col-name',
                    render: (r) => <strong>{r.name || `${r.category_name} · ${r.site_name}`}</strong>,
                  },
                  {
                    label: t('站点', 'Site'),
                    className: 'template-col-site',
                    render: (r) => (
                      <div className="identity-cell">
                        <span className="site-avatar">
                          <Globe2 size={19} />
                        </span>
                        <div>
                          <strong>{r.site_name}</strong>
                        </div>
                      </div>
                    ),
                  },
                  {
                    label: t('分类', 'Category'),
                    className: 'template-col-category',
                    render: (r) => (
                      <span>
                        {t(
                          r.service_name || r.category_name,
                          r.service_name_en || r.service_name || r.category_name,
                        )}
                      </span>
                    ),
                  },
                  {
                    label: t('站点版本', 'Site version'),
                    className: 'template-col-version',
                    render: (r) => <TemplateSiteVersion site={siteById.get(r.site_id)} row={r} t={t} />,
                  },
                  {
                    label: t('接收模型', 'Accepted models'),
                    className: 'template-col-models',
                    render: (r) => {
                      const displayModels = sortModelsByReleaseDate(r.models || []);
                      return (
                        <div className="template-table-models">
                          {displayModels.slice(0, 3).map((model: string) => (
                            <Tooltip key={model} content={model} mono>
                              <span>{model}</span>
                            </Tooltip>
                          ))}
                          {displayModels.length > 3 && (
                            <Tooltip content={displayModels.slice(3).join('\n')} mono>
                              <span>+{displayModels.length - 3}</span>
                            </Tooltip>
                          )}
                          {!displayModels.length && (
                            <span className="muted">{t('尚未配置', 'Not configured')}</span>
                          )}
                        </div>
                      );
                    },
                  },
                  {
                    label: t('渠道分组', 'Channel groups'),
                    className: 'template-col-groups',
                    render: (r) => (
                      <div className="template-table-groups">
                        {routingGroups(r.routing_group || '').map((group) => (
                          <Tooltip key={group} content={group}>
                            <span>{group}</span>
                          </Tooltip>
                        ))}
                      </div>
                    ),
                  },
                  {
                    label: t('状态', 'Status'),
                    className: 'template-col-state',
                    render: (r) => (
                      <div className="template-state">
                        <span className={`status ${r.ready ? 'green' : 'amber'}`}>
                          <i />
                          {r.ready ? t('可分发', 'Ready to distribute') : t('不可分发', 'Unavailable')}
                        </span>
                        {!r.ready &&
                          templateDisplayIssues(r, siteById.get(r.site_id), t).map((issue) => (
                            <TemplateIssue key={issue} issue={issue} t={t} />
                          ))}
                      </div>
                    ),
                  },
                  {
                    label: t('操作', 'Actions'),
                    className: 'template-col-actions table-actions-cell',
                    render: (r) => (
                      <div className="row-actions template-row-actions">
                        <button disabled={busy} onClick={() => setEdit(r)}>
                          <Edit3 size={14} />
                          {t('编辑', 'Edit')}
                        </button>
                        <button
                          disabled={busy}
                          onClick={() =>
                            run(async () => {
                              await api(`/upload-templates/${r.id}`, 'PATCH', { enabled: !r.enabled });
                              templates.refresh();
                              notify(
                                r.enabled
                                  ? t('模板已停用', 'Template disabled')
                                  : t('模板已启用', 'Template enabled'),
                              );
                            })
                          }
                        >
                          <Power size={14} />
                          {r.enabled ? t('停用', 'Disable') : t('启用', 'Enable')}
                        </button>
                        <button
                          disabled={busy || !siteById.has(r.site_id) || !!siteById.get(r.site_id)?.archived}
                          onClick={() => void verifySite(r.site_id)}
                        >
                          {verifyingSiteId === r.site_id ? (
                            <LoaderCircle className="spin" size={14} />
                          ) : (
                            <RefreshCw size={14} />
                          )}
                          {verifyingSiteId === r.site_id
                            ? t('验证中…', 'Verifying…')
                            : t('重新验证站点', 'Reverify site')}
                        </button>
                        <button
                          type="button"
                          className="danger-text"
                          disabled={busy}
                          onClick={() => setDeleteTarget(r)}
                        >
                          <Trash2 size={14} />
                          {t('删除', 'Delete')}
                        </button>
                      </div>
                    ),
                  },
                ]}
              />
            </section>
          )}
        </DataState>
      </PullToRefresh>
      {deleteTarget && (
        <Confirm
          title={t('删除分发模板', 'Delete distribution template')}
          danger
          onClose={() => setDeleteTarget(null)}
          onConfirm={async () => {
            await api(`/upload-templates/${deleteTarget.id}`, 'DELETE');
            templates.refresh();
            notify(t('分发模板已删除', 'Distribution template deleted'));
          }}
        >
          <p>
            <strong>
              #{deleteTarget.display_id}{' '}
              {deleteTarget.name || `${deleteTarget.category_name} · ${deleteTarget.site_name}`}
            </strong>
          </p>
          <p>
            {t(
              '删除后无法恢复，未开始的相关分发将停止。已创建的渠道和结算记录会保留。',
              'This cannot be undone. Pending distribution steps using this template will be stopped. Existing channels and settlement records will be kept.',
            )}
          </p>
          <p>
            {t(
              '删除后如无其他可用模板，该站点将停止新分发。',
              'If no usable templates remain, new distribution to this site will be disabled.',
            )}
          </p>
        </Confirm>
      )}
      {edit && (
        <TemplateEditor
          initial={edit}
          sites={items(sites.data)}
          categories={visibleCategories}
          formats={visibleFormats}
          templates={items(templates.data)}
          onClose={() => setEdit(null)}
          onSaved={refresh}
        />
      )}
    </Page>
  );
}
