import { useId } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { MAX_TEMPLATE_MODELS, modelIdError } from './ModelPicker';
import './model-mapping-editor.css';
import { sortModelsByReleaseDate } from '../model-release-order';

type Translate = (zh: string, en: string) => string;
type MappingRow = { source: string; target: string };
export type ModelMappingDraft = { mode: 'visual'; rows: MappingRow[] } | { mode: 'json'; text: string };

export function initialModelMapping(mapping: Record<string, string> = {}): ModelMappingDraft {
  return { mode: 'visual', rows: Object.entries(mapping).map(([source, target]) => ({ source, target })) };
}

function mappingIdError(model: string, t: Translate): string {
  const error = modelIdError(model, t);
  if (error) return error;
  if (/[\u0000-\u001f\u007f]/.test(model) || new TextEncoder().encode(model).length > 255)
    return t(
      '映射模型 ID 不能包含控制字符，且不能超过 255 个 UTF-8 字节。',
      'Mapping model IDs must not contain control characters or exceed 255 UTF-8 bytes.',
    );
  return '';
}

function jsonRows(text: string): MappingRow[] {
  const parsed: unknown = JSON.parse(text);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('object');
  // Read flat string pairs separately so duplicate JSON keys are never silently overwritten.
  const token = /"(?:\\.|[^"\\])*"/y;
  let cursor = text.indexOf('{') + 1;
  const skip = () => {
    while (/\s/.test(text[cursor] || '') && cursor < text.length) cursor++;
  };
  const read = () => {
    skip();
    token.lastIndex = cursor;
    const match = token.exec(text);
    if (!match) throw new Error('string');
    cursor = token.lastIndex;
    return JSON.parse(match[0]) as string;
  };
  const rows: MappingRow[] = [];
  skip();
  while (text[cursor] !== '}') {
    const source = read();
    skip();
    cursor++;
    const target = read();
    rows.push({ source, target });
    skip();
    if (text[cursor] === ',') cursor++;
    skip();
  }
  return rows;
}

export function modelMappingResult(
  draft: ModelMappingDraft,
  t: Translate,
): { mapping: Record<string, string>; error: string } {
  let rows: MappingRow[];
  try {
    rows = draft.mode === 'visual' ? draft.rows : jsonRows(draft.text);
  } catch (error) {
    return {
      mapping: {},
      error:
        error instanceof SyntaxError
          ? t('模型映射 JSON 格式无效，请检查后重试。', 'Invalid model mapping JSON. Check its syntax.')
          : t(
              '模型映射须为 JSON 对象，所有替换模型必须是字符串。',
              'Model mapping must be a JSON object with string replacement models.',
            ),
    };
  }
  if (rows.length > MAX_TEMPLATE_MODELS)
    return { mapping: {}, error: t('最多配置 200 条模型映射。', 'Configure at most 200 model mappings.') };
  const sources = new Set<string>();
  for (const [index, row] of rows.entries()) {
    let error =
      !row.source.trim() || !row.target.trim()
        ? t(
            '请填写原始模型和替换模型，或删除此行。',
            'Enter both the original and replacement model, or delete this row.',
          )
        : mappingIdError(row.source, t) || mappingIdError(row.target, t);
    const source = row.source.trim();
    if (!error && sources.has(source)) error = t('原始模型不能重复。', 'Original models must be unique.');
    if (error)
      return { mapping: {}, error: t(`第 ${index + 1} 条映射：${error}`, `Mapping ${index + 1}: ${error}`) };
    sources.add(source);
  }
  return {
    mapping: Object.fromEntries(rows.map((row) => [row.source.trim(), row.target.trim()])),
    error: '',
  };
}

export function sameModelMapping(left: Record<string, string>, right: Record<string, string>): boolean {
  return (
    Object.keys(left).length === Object.keys(right).length &&
    Object.keys(left).every(
      (source) => Object.prototype.hasOwnProperty.call(right, source) && left[source] === right[source],
    )
  );
}

export function pruneModelMapping(draft: ModelMappingDraft, removedModels: string[]): ModelMappingDraft {
  if (!removedModels.length) return draft;
  if (draft.mode === 'visual')
    return { ...draft, rows: draft.rows.filter((row) => !removedModels.includes(row.source.trim())) };
  try {
    const rows = jsonRows(draft.text).filter((row) => !removedModels.includes(row.source.trim()));
    // Preserve duplicate rows until the user fixes them, including while removing another selected model.
    return {
      mode: 'json',
      text: `{${rows.length ? '\n' + rows.map((row) => `  ${JSON.stringify(row.source)}: ${JSON.stringify(row.target)}`).join(',\n') + '\n' : ''}}`,
    };
  } catch {
    return draft;
  }
}

export default function ModelMappingEditor({
  value,
  models,
  disabled,
  onChange,
  pending,
  applyError,
  onApply,
  t,
}: {
  value: ModelMappingDraft;
  models: string[];
  disabled: boolean;
  onChange: (value: ModelMappingDraft) => void;
  pending: boolean;
  applyError: string;
  onApply: () => void;
  t: Translate;
}) {
  const id = useId();
  const result = modelMappingResult(value, t);
  const missingModels = result.error
    ? []
    : Object.keys(result.mapping).filter((model) => !models.includes(model));
  const changeMode = (mode: ModelMappingDraft['mode']) => {
    if (disabled || value.mode === mode || result.error) return;
    onChange(
      mode === 'visual'
        ? initialModelMapping(result.mapping)
        : { mode: 'json', text: JSON.stringify(result.mapping, null, 2) },
    );
  };
  const update = (index: number, field: keyof MappingRow, text: string) => {
    if (disabled || value.mode !== 'visual') return;
    onChange({
      mode: 'visual',
      rows: value.rows.map((row, position) => (position === index ? { ...row, [field]: text } : row)),
    });
  };
  return (
    <section className="model-mapping-editor" aria-label={t('模型映射', 'Model mapping')}>
      <div className="model-mapping-heading">
        <h3>{t('模型映射', 'Model mapping')}</h3>
        <div
          className="model-mapping-tabs"
          role="tablist"
          aria-label={t('模型映射编辑方式', 'Model mapping editor mode')}
        >
          {(['visual', 'json'] as const).map((mode) => (
            <button
              type="button"
              role="tab"
              key={mode}
              id={`${id}-${mode}-tab`}
              aria-selected={value.mode === mode}
              aria-controls={`${id}-panel`}
              disabled={disabled || (value.mode !== mode && !!result.error)}
              onClick={() => changeMode(mode)}
            >
              {mode === 'visual' ? t('可视化', 'Visual') : 'JSON'}
            </button>
          ))}
        </div>
      </div>
      <p className="model-mapping-hint">
        {t(
          '填写原始模型和调用时使用的替换模型 ID。应用后，新的原始模型会加入接收模型；保存模板后生效。',
          'Enter the original model and the replacement model ID used for calls. Apply to add new original models to Accepted models; save the template to persist the changes.',
        )}
      </p>
      <div role="tabpanel" id={`${id}-panel`} aria-labelledby={`${id}-${value.mode}-tab`}>
        {value.mode === 'visual' ? (
          <>
            <datalist id={`${id}-models`}>
              {sortModelsByReleaseDate(models).map((model) => (
                <option key={model} value={model} />
              ))}
            </datalist>
            <div className="model-mapping-rows">
              {value.rows.map((row, index) => (
                <div className="model-mapping-row" key={index}>
                  <label>
                    <span>{t('原始模型', 'Original model')}</span>
                    <input
                      aria-label={t(`原始模型 ${index + 1}`, `Original model ${index + 1}`)}
                      list={`${id}-models`}
                      value={row.source}
                      disabled={disabled}
                      onChange={(event) => update(index, 'source', event.target.value)}
                      spellCheck={false}
                      autoCapitalize="off"
                      autoComplete="off"
                    />
                  </label>
                  <label>
                    <span>{t('替换模型', 'Replacement model')}</span>
                    <input
                      aria-label={t(`替换模型 ${index + 1}`, `Replacement model ${index + 1}`)}
                      value={row.target}
                      disabled={disabled}
                      onChange={(event) => update(index, 'target', event.target.value)}
                      spellCheck={false}
                      autoCapitalize="off"
                      autoComplete="off"
                    />
                  </label>
                  <button
                    type="button"
                    className="icon-button danger"
                    aria-label={t(`删除映射 ${index + 1}`, `Delete mapping ${index + 1}`)}
                    disabled={disabled}
                    onClick={() => {
                      if (!disabled)
                        onChange({
                          mode: 'visual',
                          rows: value.rows.filter((_, position) => position !== index),
                        });
                    }}
                  >
                    <Trash2 size={17} />
                  </button>
                </div>
              ))}
              {!value.rows.length && (
                <p className="model-mapping-empty">{t('未设置模型映射。', 'No model mapping configured.')}</p>
              )}
            </div>
            <button
              type="button"
              className="button secondary"
              disabled={disabled || value.rows.length >= MAX_TEMPLATE_MODELS}
              onClick={() => {
                if (!disabled)
                  onChange({ mode: 'visual', rows: [...value.rows, { source: '', target: '' }] });
              }}
            >
              <Plus size={15} />
              {t('添加映射', 'Add mapping')}
            </button>
          </>
        ) : (
          <textarea
            className="model-mapping-json mono"
            aria-label={t('模型映射 JSON', 'Model mapping JSON')}
            aria-invalid={!!result.error}
            value={value.text}
            disabled={disabled}
            onChange={(event) => onChange({ mode: 'json', text: event.target.value })}
            spellCheck={false}
            rows={7}
          />
        )}
      </div>
      {missingModels.length > 0 && (
        <aside className="model-mapping-missing" aria-label={t('缺失模型', 'Missing models')}>
          <strong>
            {t(
              `以下 ${missingModels.length} 个原始模型尚未加入接收模型`,
              `${missingModels.length} original model${missingModels.length === 1 ? ' is' : 's are'} missing from Accepted models`,
            )}
          </strong>
          <ul className="model-mapping-missing-list">
            {sortModelsByReleaseDate(missingModels).map((model) => (
              <li key={model}>{model}</li>
            ))}
          </ul>
          <p>
            {t(
              '点击后将添加这些模型并同步应用全部映射，保存模板后生效。',
              'Add these models and apply all mapping changes together. Save the template to persist them.',
            )}
          </p>
          <button
            type="button"
            className="button secondary model-mapping-missing-add"
            disabled={disabled}
            onClick={() => {
              if (!disabled && !result.error) onApply();
            }}
          >
            <Plus size={15} />
            {t('添加缺失模型', 'Add missing models')}
          </button>
        </aside>
      )}
      <div className="model-mapping-footer">
        <span
          className={result.error || applyError ? 'danger-text' : 'muted'}
          role={result.error || applyError ? 'alert' : 'status'}
        >
          {result.error ||
            applyError ||
            (pending && !missingModels.length
              ? t(
                  '映射有未应用的更改，请先应用映射。',
                  'Mapping changes have not been applied. Apply them before saving.',
                )
              : '')}
        </span>
        <div className="model-mapping-actions">
          <button
            type="button"
            className="text-button"
            disabled={disabled}
            onClick={() => {
              if (!disabled)
                onChange(value.mode === 'json' ? { mode: 'json', text: '{}' } : initialModelMapping());
            }}
          >
            {t('清空映射', 'Clear mapping')}
          </button>
          {!missingModels.length && (
            <button
              type="button"
              className="button secondary"
              disabled={disabled || !!result.error || !pending}
              onClick={() => {
                if (!disabled && !result.error) onApply();
              }}
            >
              {t('应用映射', 'Apply mapping')}
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
