import type { Row } from './core';

export const CHANNEL_CATEGORIES = [
  { name: 'AWS', description: 'Amazon Bedrock', color: '#f59e0b' },
  { name: 'Anthropic', description: 'Claude 官方', color: '#d97757' },
  { name: 'OpenAI', description: 'GPT 官方', color: '#10a987' },
  { name: 'Azure', description: 'Microsoft Azure', color: '#168bd2' },
  { name: 'Google', description: 'Gemini / Vertex', color: '#4285f4' },
  { name: 'OpenRouter', description: 'OpenRouter Claude', color: '#7265ec' },
  { name: 'OpenCode', description: 'OpenCode Claude', color: '#253044' },
] as const;

export function catalogRows(rows: Row[], nameKey = 'name'): Row[] {
  return CHANNEL_CATEGORIES.flatMap(({ name }) => {
    const row = rows.find((item) => categoryFamily(item, nameKey) === name);
    return row ? [row] : [];
  });
}

export function categoryFamily(category?: Row, nameKey = 'category_name'): string {
  return category?.family || category?.category_family || category?.[nameKey] || category?.name || '';
}

export function formatLabel(format: Row, siblings: Row[]): string {
  const name = format.name || format.code;
  const matches = siblings.filter((item) => (item.name || item.code) === name);
  if (matches.length < 2) return name;
  const version = String(format.version || '1');
  const sameVersion = matches.filter((item) => String(item.version || '1') === version).length > 1;
  return `${name} · v${version}${sameVersion ? ` · ${format.code}` : ''}`;
}

export function googleUploadType(format: Row): string {
  const kind = format.schema_config?.type || format.variant;
  const remoteType = Number(format.schema_config?.remote_type || format.remote_type);
  if (kind === 'vertex_gemini' || format.code === 'newapi-41-vertex-gemini-v1') return 'Vertex AI · Gemini';
  if (kind === 'vertex_claude' || format.code === 'newapi-41-vertex-claude-v1') return 'Vertex AI · Claude';
  if (kind === 'ai_studio_gemini' || remoteType === 24 || format.code === 'newapi-24-api-key-v1')
    return 'AI Studio · Gemini';
  return '';
}

export function googleUploadFormats(formats: Row[]): Row[] {
  return formats.filter((format) => !!googleUploadType(format));
}

/** Service identity of the current, publicly selectable credential formats. */
export function uploadServiceVariant(category: Row, format: Row): string {
  const family = categoryFamily(category);
  const kind = format.schema_config?.type;
  const remoteType = Number(format.schema_config?.remote_type);
  if (family === 'AWS') {
    if (kind === 'aws_claude') return 'aws_claude';
    return remoteType === 33 ? 'bedrock' : 'legacy';
  }
  if (family === 'Azure') {
    if (kind === 'azure_claude') return 'azure_claude';
    return kind === 'azure_gpt' || remoteType === 3 ? 'azure_gpt' : 'legacy';
  }
  if (family === 'Google') {
    if (['vertex_gemini', 'vertex_claude'].includes(kind)) return kind;
    return kind === 'ai_studio_gemini' || remoteType === 24 ? 'ai_studio_gemini' : 'vertex_legacy';
  }
  return '';
}

export function uploadTypeLabel(category: Row, format: Row, siblings: Row[]): string {
  const family = categoryFamily(category);
  const kind = format.schema_config?.type;
  const remoteType = Number(format.schema_config?.remote_type || format.remote_type);
  if (family === 'AWS')
    return `${category.name} · ${kind === 'aws_claude' || format.code === 'newapi-14-aws-claude-v1' ? 'Claude 代理' : 'Bedrock'}`;
  if (family === 'Azure')
    return `${category.name} · ${kind === 'azure_claude' || remoteType === 14 ? 'Claude' : 'GPT'}`;
  if (family === 'Google') return googleUploadType(format) || format.name;
  return siblings.length > 1 ? `${category.name} · ${formatLabel(format, siblings)}` : category.name;
}

export function categoryAppearance(name: string) {
  return CHANNEL_CATEGORIES.find((category) => category.name === name);
}
