import { useEffect, useId, useMemo, useRef, useState } from 'react';
import {
  Archive,
  Check,
  ChevronDown,
  MessageSquare,
  Network,
  Search,
  SlidersHorizontal,
  X,
} from 'lucide-react';
import { Field, Modal, useApp } from '../core';
import type { Row } from '../core';
import { AccountInfoFields, accountError, emptyAccountInfo } from './UploadConfiguration';
import './upload-advanced-options.css';
import { sortModelsByReleaseDate } from '../model-release-order';
import { claudeDisplayModels } from '../claude-models';

export function uploadModelChoices(models: string[]): string[] {
  const supplied = new Set(models);
  return [
    ...claudeDisplayModels.filter((model) => supplied.has(model)),
    ...[...supplied].filter((model) => !/(?:^|[/.])claude(?:[-.]|$)/i.test(model)),
  ];
}

export type UploadAdvancedValues = {
  models: string[] | null;
  inventory: boolean;
  remarks: string;
  proxies: string;
  accountInfo: Row;
  accountFields: string[];
};

export function emptyUploadAdvanced(): UploadAdvancedValues {
  return {
    models: null,
    inventory: false,
    remarks: '',
    proxies: '',
    accountInfo: emptyAccountInfo(),
    accountFields: [],
  };
}

function copyValues(value: UploadAdvancedValues): UploadAdvancedValues {
  return {
    ...value,
    models: value.models === null ? null : [...value.models],
    accountInfo: { ...emptyAccountInfo(), ...value.accountInfo },
    accountFields: [...value.accountFields],
  };
}

export function uploadAdvancedError(value: UploadAdvancedValues, models: string[]): string {
  if ((value.models ?? models).length > 200) return '最多选择 200 个模型，请在高级选项中缩小范围。';
  if (value.models !== null) {
    if (!value.models.length) return '请至少选择一个模型，或点击全选使用当前显示的全部模型。';
    if (
      value.models.some(
        (model) =>
          typeof model !== 'string' ||
          !model.trim() ||
          Array.from(model).length > 200 ||
          /[,\n\r]/.test(model),
      )
    )
      return '模型名称不能为空、过长或包含逗号、换行。';
    const allowed = new Set(models);
    if (value.models.some((model) => !allowed.has(model))) return '所选模型已不在模板范围内，请重新选择。';
  }
  return accountError(value.accountInfo);
}

export default function UploadAdvancedOptions({
  value,
  models,
  modelNotice,
  emptyModelMessage,
  onApply,
  onClose,
}: {
  value: UploadAdvancedValues;
  models: string[];
  modelNotice?: string;
  emptyModelMessage?: string;
  onApply: (value: UploadAdvancedValues) => void;
  onClose: () => void;
}) {
  const { t } = useApp();
  const id = useId();
  const [draft, setDraft] = useState(() => copyValues(value));
  const [accountOpen, setAccountOpen] = useState(value.accountFields.length > 0);
  const [modelSearch, setModelSearch] = useState('');
  const selectAllRef = useRef<HTMLInputElement>(null);
  const availableModels = useMemo(() => [...new Set(models)], [models]);
  const matchingModels = useMemo(() => {
    const query = modelSearch.trim().toLocaleLowerCase();
    return sortModelsByReleaseDate(
      query ? availableModels.filter((model) => model.toLocaleLowerCase().includes(query)) : availableModels,
    );
  }, [availableModels, modelSearch]);
  const selectedModels = draft.models ?? availableModels;
  const selected = new Set(selectedModels);
  const allModelsSelected =
    availableModels.length > 0 && availableModels.every((model) => selected.has(model));
  const someModelsSelected = availableModels.some((model) => selected.has(model));
  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = someModelsSelected && !allModelsSelected;
  }, [allModelsSelected, someModelsSelected]);
  const error = uploadAdvancedError(draft, availableModels);
  const errorTranslations: Record<string, string> = {
    '请至少选择一个模型，或点击全选使用当前显示的全部模型。':
      'Select at least one model, or select all to use every listed model.',
    '最多选择 200 个模型，请在高级选项中缩小范围。':
      'Choose at most 200 models. Narrow the model scope in Advanced options.',
    '模型名称不能为空、过长或包含逗号、换行。':
      'Model names cannot be empty, too long, or contain commas or line breaks.',
    '所选模型已不在模板范围内，请重新选择。':
      'The selected models are no longer available in the templates. Select them again.',
    '余额须为非负数或留空。': 'Balance must be nonnegative or left blank.',
    '号况 RPM / TPM 须为非负整数或留空。': 'Account RPM / TPM must be nonnegative integers or left blank.',
  };
  const changeModel = (model: string) => {
    const next = new Set(selectedModels);
    if (next.has(model)) next.delete(model);
    else next.add(model);
    const allSelected =
      next.size === availableModels.length && availableModels.every((name) => next.has(name));
    setDraft((current) => ({ ...current, models: allSelected ? null : [...next] }));
  };
  const apply = () => {
    if (error) return;
    const next = copyValues(draft);
    if (
      next.models !== null &&
      next.models.length === availableModels.length &&
      availableModels.every((model) => next.models!.includes(model))
    )
      next.models = null;
    next.accountFields = [...new Set(next.accountFields)];
    onApply(next);
  };

  return (
    <div className="upload-advanced-options">
      <Modal
        wide
        title={t('高级选项', 'Advanced options')}
        subtitle={t('模型范围 · 号况 · 备注 · 代理', 'Models · Account details · Remarks · Proxies')}
        onClose={onClose}
        footer={
          <div className="upload-advanced-actions">
            <button type="button" className="button secondary" onClick={onClose}>
              {t('取消', 'Cancel')}
            </button>
            <button
              type="button"
              className="button primary"
              disabled={!!error}
              aria-describedby={error ? `${id}-error` : undefined}
              onClick={apply}
            >
              <Check size={15} aria-hidden="true" />
              {t('应用设置', 'Apply settings')}
            </button>
          </div>
        }
      >
        <div className="upload-advanced-content">
          <section className="upload-advanced-section" aria-labelledby={`${id}-models-title`}>
            <div className="upload-advanced-section-heading">
              <div>
                <h3 id={`${id}-models-title`}>{t('可用模型范围', 'Available model scope')}</h3>
                <p id={`${id}-models-hint`}>
                  {t(
                    '默认全选列表模型；各站点接收与模板的交集，没有共同模型的站点会跳过。',
                    'All listed models are selected by default. Each site receives its template intersection; sites with no shared models are skipped.',
                  )}
                </p>
              </div>
              <span className="upload-advanced-count" aria-live="polite">
                {t(
                  `已选 ${selected.size} / ${availableModels.length}`,
                  `${selected.size} / ${availableModels.length} selected`,
                )}
              </span>
            </div>
            {modelNotice && (
              <p className="upload-advanced-model-notice" role="status">
                {modelNotice}
              </p>
            )}
            <div className="upload-advanced-model-search">
              <Search size={16} aria-hidden="true" />
              <input
                type="search"
                aria-label={t('搜索模型', 'Search models')}
                placeholder={t('搜索模型名称', 'Search model names')}
                value={modelSearch}
                onChange={(event) => setModelSearch(event.target.value)}
                autoComplete="off"
                spellCheck={false}
                aria-controls={`${id}-models-list`}
              />
              {modelSearch && (
                <button
                  type="button"
                  aria-label={t('清空搜索', 'Clear search')}
                  onClick={() => setModelSearch('')}
                >
                  <X size={15} aria-hidden="true" />
                </button>
              )}
            </div>
            <div className="upload-advanced-model-picker">
              <div className="upload-advanced-model-actions">
                <label className="upload-advanced-model-select-all">
                  <input
                    ref={selectAllRef}
                    type="checkbox"
                    checked={allModelsSelected}
                    disabled={!availableModels.length}
                    aria-describedby={modelSearch.trim() ? `${id}-models-filter-hint` : undefined}
                    onChange={(event) =>
                      setDraft((current) => ({ ...current, models: event.target.checked ? null : [] }))
                    }
                  />
                  <span>{t('全选', 'Select all')}</span>
                </label>
                <span className="upload-advanced-model-match-count" aria-live="polite">
                  {modelSearch.trim()
                    ? t(
                        `匹配 ${matchingModels.length} / ${availableModels.length}`,
                        `${matchingModels.length} / ${availableModels.length} matches`,
                      )
                    : t(`共 ${availableModels.length} 个模型`, `${availableModels.length} models`)}
                </span>
              </div>
              <div
                id={`${id}-models-list`}
                className="upload-advanced-models"
                data-short-list={availableModels.length <= 9 || undefined}
                role="group"
                aria-labelledby={`${id}-models-title`}
                aria-describedby={`${id}-models-hint`}
              >
                {matchingModels.length ? (
                  matchingModels.map((model) => (
                    <label
                      key={model}
                      className={`upload-advanced-model${selected.has(model) ? ' is-selected' : ''}`}
                    >
                      <input
                        type="checkbox"
                        checked={selected.has(model)}
                        onChange={() => changeModel(model)}
                      />
                      <span>{model}</span>
                    </label>
                  ))
                ) : (
                  <p className="upload-advanced-empty">
                    {availableModels.length
                      ? t('没有匹配的模型，请调整搜索内容。', 'No models match. Try another search.')
                      : emptyModelMessage ||
                        t(
                          '此分类暂未配置模型，请联系管理员设置模板。',
                          'This category has no models yet. Ask an administrator to configure its templates.',
                        )}
                  </p>
                )}
              </div>
            </div>
            {modelSearch.trim() && (
              <p id={`${id}-models-filter-hint`} className="upload-advanced-model-filter-hint">
                {t(
                  '搜索仅筛选列表；全选包含未显示的模型。',
                  'Search only filters this list. Select all includes hidden models.',
                )}
              </p>
            )}
          </section>

          <label className="upload-advanced-inventory">
            <input
              type="checkbox"
              checked={draft.inventory}
              aria-describedby={`${id}-inventory-hint`}
              onChange={(event) => setDraft((current) => ({ ...current, inventory: event.target.checked }))}
            />
            <span className="upload-advanced-inventory-copy">
              <strong>
                <Archive size={16} aria-hidden="true" />
                {t('入库存（备用，暂不上线）', 'Keep in inventory (standby, inactive)')}
              </strong>
              <small id={`${id}-inventory-hint`}>
                {t(
                  '按模板分发为停用渠道，暂不参与调用',
                  'Distribute using templates as disabled channels, without serving requests.',
                )}
              </small>
            </span>
          </label>

          <section className="upload-advanced-account">
            <button
              type="button"
              className="upload-advanced-account-toggle"
              aria-expanded={accountOpen}
              aria-controls={`${id}-account-fields`}
              onClick={() => setAccountOpen((open) => !open)}
            >
              <span>
                <SlidersHorizontal size={16} aria-hidden="true" />
                {t('号况申报（可选）', 'Account details (optional)')}
              </span>
              <ChevronDown className={accountOpen ? 'is-open' : ''} size={17} aria-hidden="true" />
            </button>
            <div id={`${id}-account-fields`} className="upload-advanced-account-body" hidden={!accountOpen}>
              <p>
                {t(
                  '只应用你修改的字段，其余使用各站点模板设置。',
                  'Only fields you change override each destination template.',
                )}
              </p>
              <AccountInfoFields
                value={draft.accountInfo}
                onChange={(name, fieldValue) =>
                  setDraft((current) => ({
                    ...current,
                    accountInfo: { ...current.accountInfo, [name]: fieldValue },
                    accountFields: [...new Set([...current.accountFields, name])],
                  }))
                }
              />
            </div>
          </section>

          <div className="upload-advanced-text-fields">
            <section aria-labelledby={`${id}-remarks-title`}>
              <h3 id={`${id}-remarks-title`}>
                <MessageSquare size={16} aria-hidden="true" />
                {t('备注', 'Remarks')}
              </h3>
              <Field
                label={t('备注内容（可选）', 'Remarks (optional)')}
                hint={t(
                  '一行备注供本批共用，多行按密钥逐行对应；留空使用各站点模板备注。',
                  'One line applies to every key; multiple lines match keys in order. Leave blank to use each template’s remark.',
                )}
              >
                <textarea
                  rows={4}
                  maxLength={100000}
                  value={draft.remarks}
                  onChange={(event) => setDraft((current) => ({ ...current, remarks: event.target.value }))}
                  placeholder={t(
                    '第 1 条密钥的备注\n第 2 条密钥的备注',
                    'Remark for key 1\nRemark for key 2',
                  )}
                />
              </Field>
            </section>
            <section aria-labelledby={`${id}-proxies-title`}>
              <h3 id={`${id}-proxies-title`}>
                <Network size={16} aria-hidden="true" />
                {t('代理', 'Proxies')}
              </h3>
              <Field
                label={t('代理地址（可选）', 'Proxy addresses (optional)')}
                hint={t(
                  '一个地址供本批共用，多个地址按密钥逐行对应；留空不使用代理。',
                  'One address applies to every key; multiple lines match keys in order. Leave blank to use no proxy.',
                )}
              >
                <textarea
                  rows={4}
                  maxLength={100000}
                  value={draft.proxies}
                  autoComplete="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  onChange={(event) => setDraft((current) => ({ ...current, proxies: event.target.value }))}
                  placeholder="https://user:password@proxy.example:443"
                />
              </Field>
            </section>
          </div>
          {error && (
            <p id={`${id}-error`} className="upload-advanced-error" role="alert">
              {t(error, errorTranslations[error] || error)}
            </p>
          )}
        </div>
      </Modal>
    </div>
  );
}
