import type { Row } from './core';
import { categoryFamily } from './catalog';

// User-selected OpenAI catalog, deduplicated by exact model ID (2026-09-10).
const openAIModels = [
  'gpt-5',
  'gpt-5.1',
  'gpt-5.2',
  'gpt-5.2-pro',
  'gpt-5.3-codex',
  'gpt-5.4',
  'gpt-5.4-pro',
  'gpt-5.5',
  'gpt-5.5-pro',
  'gpt-4o',
  'gpt-5.4-mini',
  'gpt-5-mini-2025-08-07',
  'o3-mini-2025-01-31',
  'gpt-4-turbo-2024-04-09',
  'gpt-4o-transcribe-2025-03-20',
  'gpt-4.1-2025-04-14',
  'gpt-5.5-pro-2026-04-23',
  'gpt-4o-2024-08-06',
  'gpt-4o-transcribe',
  'gpt-5.4-2026-03-05',
  'gpt-5.6-sol',
  'gpt-5.6-luna',
  'gpt-5.6-terra',
  'gpt-image-2.5',
  'gpt-image-2',
  'gpt-6-astra',
] as const;

const serviceVariants: Record<string, string[]> = {
  AWS: ['bedrock', 'aws_claude'],
  Azure: ['azure_gpt', 'azure_claude'],
  Google: ['ai_studio_gemini', 'vertex_gemini', 'vertex_claude'],
};

function uploadModelType(family: string, format: Row | undefined, variant: string): string {
  const variants = serviceVariants[family];
  if (variant) return variants?.includes(variant) ? variant : '';
  const kind = format?.schema_config?.type;
  const remoteType = Number(format?.schema_config?.remote_type);
  const code = format?.code;
  if (family === 'AWS') {
    if (kind === 'aws_claude' || code === 'newapi-14-aws-claude-v1') return 'aws_claude';
    if (
      ['aws_bedrock', 'aws_ak_sk', 'aws_api_key'].includes(kind) ||
      ['newapi-33-aws-bedrock-v1', 'newapi-33-aws-ak-sk-v1', 'newapi-33-aws-api-key-v1'].includes(code)
    )
      return 'bedrock';
    return '';
  }
  if (family === 'Azure') {
    if (kind === 'azure_claude' || code === 'newapi-14-azure-claude-v1') return 'azure_claude';
    if (
      kind === 'azure_gpt' ||
      (kind === 'api_key' && remoteType === 3) ||
      ['newapi-3-azure-gpt-v1', 'newapi-3-api-key-v1'].includes(code)
    )
      return 'azure_gpt';
    return '';
  }
  if (family === 'Google') {
    if (kind === 'vertex_gemini' || code === 'newapi-41-vertex-gemini-v1') return 'vertex_gemini';
    if (kind === 'vertex_claude' || code === 'newapi-41-vertex-claude-v1') return 'vertex_claude';
    if ((kind === 'api_key' && remoteType === 24) || code === 'newapi-24-api-key-v1')
      return 'ai_studio_gemini';
    // Old Vertex credentials do not identify their business service on their own.
    return '';
  }
  if (
    ['Anthropic', 'OpenAI', 'OpenRouter', 'OpenCode'].includes(family) &&
    kind === 'api_key' &&
    Number.isInteger(remoteType) &&
    remoteType > 0
  )
    return `api_key:${remoteType}`;
  return '';
}

/** OpenAI services use the curated list; other services reuse configured models. */
export function templateModelCandidates({
  category,
  format,
  variant = '',
  templates,
  formats,
  sites,
}: {
  category: Row | undefined;
  format: Row | undefined;
  variant?: string;
  templates: Row[];
  formats: Row[];
  sites: Row[];
}): string[] {
  if (!category?.id || !format?.id) return [];
  const family = categoryFamily(category);
  const selectedType = uploadModelType(family, format, variant);
  if (family === 'OpenAI' || (family === 'Azure' && selectedType === 'azure_gpt'))
    return [...new Set(openAIModels)];
  return templates.flatMap((template) => {
    if (template.category_id !== category.id) return [];
    const site = sites.find((item) => item.id === template.site_id);
    if (!site || site.archived) return [];
    const sourceFormat = formats.find((item) => item.id === template.format_id);
    const sourceType = uploadModelType(family, sourceFormat, template.variant || '');
    const matches =
      selectedType && sourceType ? selectedType === sourceType : template.format_id === format.id;
    return matches && Array.isArray(template.models) ? template.models : [];
  });
}
