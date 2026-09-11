import { useId, useState } from 'react';
import { Search, Trash2 } from 'lucide-react';
import { modelReleaseDate, sortModelsByReleaseDate } from '../model-release-order';
import './model-picker.css';

type Translate = (zh: string, en: string) => string;
export const MAX_TEMPLATE_MODELS = 200;

export function normalizeModelIds(models: string[]): string[] {
  return [...new Set(models.map((model) => model.trim()))];
}

export function modelIdError(model: string, t: Translate): string {
  if (!model.trim()) return t('模型 ID 不能为空。', 'A model ID is required.');
  if (Array.from(model).length > 200 || /[,\r\n]/.test(model))
    return t(
      '模型 ID 不能超过 200 个字符，也不能包含英文逗号或换行。',
      'Model IDs must be at most 200 characters without commas or newlines.',
    );
  return '';
}

export function modelSelectionError(models: string[], t: Translate): string {
  if (models.length > MAX_TEMPLATE_MODELS)
    return t('每份模板最多选择 200 个模型。', 'Select at most 200 models per template.');
  return models.map((model) => modelIdError(model, t)).find(Boolean) || '';
}

type DemandUnit = 'RPM' | 'TPM';
const DEMAND_MAX = { RPM: 1_000_000, TPM: 1_000_000_000 };

export function modelRequirementError(value: string, unit: DemandUnit, t: Translate): string {
  if (!value.trim()) return '';
  if (!/^\d+$/.test(value) || Number(value) < 1 || Number(value) > DEMAND_MAX[unit]) {
    const maximum = DEMAND_MAX[unit].toLocaleString('en-US');
    return t(
      `需求量须为 1–${maximum} 的整数 ${unit}。`,
      `Demand must be an integer from 1 to ${maximum} ${unit}.`,
    );
  }
  return '';
}

export function modelRequirementValue(
  requirements: Record<string, string> | undefined,
  model: string,
): string {
  return typeof requirements?.[model] === 'string' ? requirements[model] : '';
}

export function modelRequirementSelectionError(
  models: string[],
  requirements: Record<string, string>,
  unit: DemandUnit,
  t: Translate,
): string {
  for (const model of models) {
    const error = modelRequirementError(modelRequirementValue(requirements, model), unit, t);
    if (error) return `${model}: ${error}`;
  }
  return '';
}

export default function ModelPicker({
  value,
  choices,
  disabled,
  onChange,
  rpmRequirements,
  onRpmRequirementsChange,
  tpmRequirements,
  onTpmRequirementsChange,
  appliedMapping,
  t,
}: {
  value: string[];
  choices: string[];
  disabled: boolean;
  onChange: (value: string[]) => void;
  rpmRequirements?: Record<string, string>;
  onRpmRequirementsChange?: (value: Record<string, string>) => void;
  tpmRequirements?: Record<string, string>;
  onTpmRequirementsChange?: (value: Record<string, string>) => void;
  appliedMapping?: Record<string, string>;
  t: Translate;
}) {
  const id = useId();
  const [search, setSearch] = useState('');
  const [message, setMessage] = useState('');
  const candidates = sortModelsByReleaseDate([...choices, ...value]);
  const query = search.trim().toLocaleLowerCase();
  const visible = candidates.filter((model) => model.toLocaleLowerCase().includes(query));
  const undatedCount = visible.filter((model) => !modelReleaseDate(model)).length;
  const selectable = visible.filter((model) => !value.includes(model) && !modelIdError(model, t));
  const error = modelSelectionError(value, t);
  const demandFields = [
    { unit: 'RPM', requirements: rpmRequirements, onChange: onRpmRequirementsChange },
    { unit: 'TPM', requirements: tpmRequirements, onChange: onTpmRequirementsChange },
  ] as const;
  const demandError =
    demandFields
      .map((field) =>
        field.onChange ? modelRequirementSelectionError(value, field.requirements || {}, field.unit, t) : '',
      )
      .find(Boolean) || '';
  const remaining = Math.max(0, MAX_TEMPLATE_MODELS - value.length);
  const change = (next: string[]) => {
    if (disabled) return;
    onChange(next);
    for (const field of demandFields)
      field.onChange?.(
        Object.fromEntries(
          next
            .map((model) => [model, modelRequirementValue(field.requirements, model)])
            .filter(([, demand]) => demand !== ''),
        ),
      );
    setMessage('');
  };
  return (
    <div className="model-picker">
      <div className="model-picker-toolbar">
        <label className="model-picker-search">
          <Search size={16} aria-hidden="true" />
          <input
            type="search"
            aria-label={t('搜索模型', 'Search models')}
            placeholder={t('搜索模型', 'Search models')}
            value={search}
            disabled={disabled}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <div className="model-picker-tools">
          <button
            type="button"
            className="text-button"
            disabled={disabled || !selectable.length || !remaining}
            onClick={() => {
              if (disabled) return;
              const next = selectable.slice(0, remaining);
              change([...value, ...next]);
              if (next.length < selectable.length)
                setMessage(
                  t(
                    `最多选择 200 个模型，另有 ${selectable.length - next.length} 个搜索结果未选中。`,
                    `The limit is 200 models; ${selectable.length - next.length} other results remain unselected.`,
                  ),
                );
            }}
          >
            {query ? t('全选搜索结果', 'Select search results') : t('全选模型', 'Select all models')}
          </button>
          <button
            type="button"
            className="text-button"
            disabled={disabled || !value.length}
            onClick={() => change([])}
          >
            {t('清空', 'Clear')}
          </button>
        </div>
      </div>
      <div className="model-picker-count" aria-live="polite">
        <strong>{t(`已选 ${value.length} / 200`, `${value.length} / 200 selected`)}</strong>
        <span>{t(`${visible.length} 个选项`, `${visible.length} options`)}</span>
      </div>
      <p className="model-picker-order-hint">
        {t('按发布时间从新到旧排序。', 'Sorted by release date, newest first.')}
        {undatedCount > 0 &&
          t(
            ` ${undatedCount} 个模型的发布时间未确认，排在末尾。`,
            ` ${undatedCount} models with unverified release dates are listed last.`,
          )}
      </p>
      {(onRpmRequirementsChange || onTpmRequirementsChange) && (
        <p className="model-picker-demand-hint">
          {onRpmRequirementsChange && t('RPM：每分钟请求量。', 'RPM: requests per minute. ')}
          {onTpmRequirementsChange && t('TPM：每分钟 Token 数。', 'TPM: tokens per minute. ')}
          {t(
            '用于记录模板需求，留空表示未设置。',
            'These record template demand. Leave blank if unspecified.',
          )}
        </p>
      )}
      <div
        className="model-picker-options"
        role="group"
        aria-label={t('接收模型选项', 'Accepted model options')}
      >
        {visible.map((model, index) => {
          const checked = value.includes(model);
          const limited = !checked && (!remaining || !!modelIdError(model, t));
          return (
            <div className="model-picker-option" data-selected={checked || undefined} key={model}>
              <div className="model-picker-option-heading">
                <label className="model-picker-choice">
                  <input
                    type="checkbox"
                    aria-label={model || t('空模型 ID', 'Empty model ID')}
                    checked={checked}
                    disabled={disabled || limited}
                    onChange={() =>
                      change(checked ? value.filter((selected) => selected !== model) : [...value, model])
                    }
                  />
                  <span>{model || t('空模型 ID（请移除）', 'Empty model ID (remove this entry)')}</span>
                </label>
                {checked && (
                  <button
                    type="button"
                    className="icon-button danger model-picker-remove"
                    aria-label={t(`删除模型 ${model}`, `Delete model ${model}`)}
                    disabled={disabled}
                    onClick={() => change(value.filter((selected) => selected !== model))}
                  >
                    <Trash2 size={14} />
                  </button>
                )}
              </div>
              {checked && typeof appliedMapping?.[model] === 'string' && (
                <small className="model-picker-mapping-target">
                  {t('映射到', 'Maps to')} <span>{appliedMapping[model]}</span>
                </small>
              )}
              {checked && (onRpmRequirementsChange || onTpmRequirementsChange) && (
                <div className="model-picker-demands">
                  {demandFields.map((field) => {
                    if (!field.onChange) return null;
                    const demand = modelRequirementValue(field.requirements, model);
                    const fieldError = modelRequirementError(demand, field.unit, t);
                    const fieldId = `${id}-${field.unit}-${index}`;
                    const label = t(`需求量（${field.unit}）`, `Demand (${field.unit})`);
                    return (
                      <div className="model-picker-demand" key={field.unit}>
                        <label htmlFor={fieldId}>{label}</label>
                        <input
                          id={fieldId}
                          type="number"
                          min={1}
                          max={DEMAND_MAX[field.unit]}
                          step={1}
                          value={demand === 'invalid' ? '' : demand}
                          disabled={disabled}
                          aria-label={`${model} ${label}`}
                          aria-invalid={!!fieldError}
                          aria-describedby={fieldError ? `${fieldId}-error` : undefined}
                          placeholder={t('可选', 'Optional')}
                          onChange={(event) => {
                            if (disabled) return;
                            const input = event.currentTarget;
                            field.onChange?.({
                              ...field.requirements,
                              [model]: input.validity.badInput ? 'invalid' : input.value,
                            });
                          }}
                        />
                        {fieldError && (
                          <small id={`${fieldId}-error`} className="danger-text">
                            {fieldError}
                          </small>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
        {!visible.length && (
          <p className="model-picker-empty">
            {query
              ? t('没有匹配的模型，请调整搜索。', 'No matches. Change the search.')
              : t('当前上传类型暂无已配置模型。', 'No models are configured for this upload type yet.')}
          </p>
        )}
      </div>
      <div className="model-picker-status" id={`${id}-status`} aria-live="polite">
        {(error || demandError) && (
          <p className="danger-text" role="alert">
            {error || demandError}
          </p>
        )}
        {message && <p>{message}</p>}
        {value.length >= MAX_TEMPLATE_MODELS && !error && (
          <p>
            {t(
              '已达到 200 个模型上限，仍可取消选择。',
              'The 200-model limit is reached. You can still deselect models.',
            )}
          </p>
        )}
      </div>
    </div>
  );
}
