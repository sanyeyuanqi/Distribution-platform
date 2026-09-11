import Select from './Select';
import { Field, useApp } from '../core';
import type { Row } from '../core';

export const emptyAccountInfo = () => ({ balance_usd: null, rpm: null, tpm: null, prepaid: null, kd: null });
export const defaultChannelConfig = () => ({
  base_url: '',
  organization: '',
  other: '',
  azure_responses_version: '',
  status: 2,
  priority: 0,
  weight: 1,
  auto_ban: 1,
  model_mapping: {},
  status_code_mapping: {},
  default_upload_mode: 'batch',
  rpm_enabled: false,
  rpm_limit: null,
  account_info: emptyAccountInfo(),
});

export function accountError(value: Row): string {
  for (const name of ['balance_usd', 'rpm', 'tpm']) {
    const number = value[name];
    if (number === null || number === undefined || number === '') continue;
    if (
      !Number.isFinite(Number(number)) ||
      Number(number) < 0 ||
      (name !== 'balance_usd' && !Number.isInteger(Number(number)))
    )
      return name === 'balance_usd' ? '余额须为非负数或留空。' : '号况 RPM / TPM 须为非负整数或留空。';
  }
  return '';
}

export function AccountInfoFields({
  value,
  onChange,
}: {
  value: Row;
  onChange: (name: string, value: number | boolean | null) => void;
}) {
  const { t } = useApp();
  return (
    <div className="form-grid account-info-fields">
      {(['balance_usd', 'rpm', 'tpm'] as const).map((name) => (
        <Field
          key={name}
          label={
            name === 'balance_usd'
              ? t('余额（USD）', 'Balance (USD)')
              : `${name.toUpperCase()} ${t('额度', 'quota')}`
          }
        >
          <input
            type="number"
            min={0}
            step={name === 'balance_usd' ? 'any' : 1}
            value={value[name] ?? ''}
            onChange={(event) =>
              onChange(name, event.target.value === '' ? null : Number(event.target.value))
            }
            placeholder={t('未填写', 'Unspecified')}
          />
        </Field>
      ))}
      {(['prepaid', 'kd'] as const).map((name) => (
        <Field
          key={name}
          label={name === 'prepaid' ? t('是否预付费', 'Prepaid account') : t('是否允许 KD', 'Allow KD')}
        >
          <Select
            value={value[name] === null || value[name] === undefined ? '' : String(value[name])}
            onChange={(event) =>
              onChange(name, event.target.value === '' ? null : event.target.value === 'true')
            }
          >
            <option value="">{t('未填写', 'Unspecified')}</option>
            <option value="true">{t('是', 'Yes')}</option>
            <option value="false">{t('否', 'No')}</option>
          </Select>
        </Field>
      ))}
    </div>
  );
}

export function RpmProtection({
  enabled,
  limit,
  onChange,
}: {
  enabled: boolean;
  limit: number | null;
  onChange: (enabled: boolean, limit: number | null) => void;
}) {
  const { t } = useApp();
  return (
    <div className="upload-rpm-config">
      <label className="upload-setting-toggle">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => onChange(event.target.checked, limit)}
        />
        <span>{t('开启 RPM 保护', 'Enable RPM protection')}</span>
      </label>
      {enabled && (
        <Field
          label={t('RPM 保护上限', 'RPM protection limit')}
          hint={t(
            '每分钟最大请求数，须为正整数；目标站点不支持时将阻止分发。',
            'Positive requests-per-minute limit. Unsupported destinations block distribution.',
          )}
        >
          <input
            type="number"
            min={1}
            step={1}
            value={limit ?? ''}
            onChange={(event) =>
              onChange(enabled, event.target.value === '' ? null : Number(event.target.value))
            }
            placeholder={t('请输入每分钟请求上限', 'Requests per minute')}
          />
        </Field>
      )}
    </div>
  );
}

export function parseModelOverride(source: string): { models: string[] | null; error: string } {
  if (!source.trim()) return { models: null, error: '' };
  let value: unknown;
  try {
    value = JSON.parse(source);
  } catch {
    return { models: null, error: '模型范围须为有效的 JSON 字符串数组。' };
  }
  if (
    !Array.isArray(value) ||
    !value.length ||
    value.length > 200 ||
    value.some(
      (item) =>
        typeof item !== 'string' || !item.trim() || Array.from(item).length > 200 || /[,\n]/.test(item),
    )
  )
    return { models: null, error: '填写 1–200 个模型名称字符串；使用模板全部模型请留空。' };
  return { models: [...new Set(value.map((item: string) => item.trim()))], error: '' };
}

export function credentialSummary(
  source: string,
  json: boolean | 'auto',
): { count: number | null; duplicates: number } {
  source = source.replace(/\r\n?/g, '\n');
  if (!source.trim()) return { count: 0, duplicates: 0 };
  let rows: string[];
  if (json) {
    try {
      rows = [];
      let cursor = 0;
      while (cursor < source.length) {
        while (/\s/.test(source[cursor] || '') && cursor < source.length) cursor++;
        if (cursor >= source.length) break;
        if (source[cursor] !== '{' && source[cursor] !== '[') {
          if (json !== 'auto') return { count: null, duplicates: 0 };
          const end = source.indexOf('\n', cursor);
          rows.push(source.slice(cursor, end < 0 ? source.length : end).trim());
          cursor = end < 0 ? source.length : end + 1;
          continue;
        }
        const start = cursor;
        let depth = 0;
        let quoted = false;
        let escaped = false;
        for (; cursor < source.length; cursor++) {
          const character = source[cursor];
          if (quoted) {
            if (escaped) escaped = false;
            else if (character === '\\') escaped = true;
            else if (character === '"') quoted = false;
          } else if (character === '"') quoted = true;
          else if (character === '{' || character === '[') depth++;
          else if (character === '}' || character === ']') {
            depth--;
            if (depth === 0) {
              cursor++;
              break;
            }
          }
        }
        if (depth !== 0 || quoted) return { count: null, duplicates: 0 };
        const value: unknown = JSON.parse(source.slice(start, cursor));
        rows.push(
          ...(Array.isArray(value) ? value : [value]).map((item) =>
            json === 'auto' && typeof item === 'string' ? item.trim() : canonicalCredential(item),
          ),
        );
        let following = cursor;
        while (following < source.length && /\s/.test(source[following])) following++;
        if (
          following < source.length &&
          source[following] !== '{' &&
          source[following] !== '[' &&
          !source.slice(cursor, following).includes('\n')
        )
          return { count: null, duplicates: 0 };
      }
    } catch {
      return { count: null, duplicates: 0 };
    }
  } else
    rows = source
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
  return { count: rows.length, duplicates: rows.length - new Set(rows).size };
}

function canonicalCredential(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalCredential).join(',')}]`;
  if (value !== null && typeof value === 'object')
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalCredential((value as Record<string, unknown>)[key])}`)
      .join(',')}}`;
  return JSON.stringify(value);
}
