import assert from 'node:assert/strict';
import test from 'node:test';
import { templateModelCandidates } from '../src/template-model-candidates.ts';

function choices(family, kind, remoteType, overrides = {}) {
  const category = { id: 'category', family };
  const format = {
    id: 'format',
    category_id: category.id,
    schema_config: { type: kind, remote_type: remoteType },
  };
  return templateModelCandidates({
    category,
    format,
    templates: [],
    formats: [format],
    sites: [],
    ...overrides,
  });
}

for (const [family, kind, remoteType] of [
  ['AWS', 'aws_bedrock', 33],
  ['AWS', 'aws_ak_sk', 33],
  ['AWS', 'aws_api_key', 33],
  ['AWS', 'aws_claude', 14],
  ['Anthropic', 'api_key', 14],
  ['Azure', 'azure_claude', 14],
  ['Google', 'vertex_claude', 41],
  ['OpenRouter', 'api_key', 20],
  ['OpenCode', 'api_key', 14],
]) {
  test(`${family}/${kind} offers Claude choices without sites or configured templates`, () => {
    const result = choices(family, kind, remoteType);
    assert.equal(result.length, 9);
    assert.equal(new Set(result).size, 9);
    assert.ok(result.includes('claude-haiku-4-5-20251001'));
    assert.ok(result.includes('claude-opus-4-8'));
    assert.ok(result.includes('claude-fable-5-1'));
    assert.ok(result.every((model) => model.startsWith('claude-')));
  });
}

test('automatically created empty drafts keep choices visible without selecting or changing models', () => {
  const draft = Object.freeze({
    category_id: 'category',
    format_id: 'format',
    site_id: 'site',
    variant: 'bedrock',
    enabled: false,
    models: Object.freeze([]),
  });
  const templates = Object.freeze([draft]);
  const result = choices('AWS', 'aws_bedrock', 33, { templates, sites: [{ id: 'site', enabled: false }] });
  assert.equal(result.length, 9);
  assert.deepEqual(draft.models, []);
  assert.equal(draft.enabled, false);
});

test('merges configured models only from the same category and service on retained sites', () => {
  const templates = [
    {
      category_id: 'category',
      format_id: 'format',
      site_id: 'site',
      models: ['claude-opus-4-8', 'custom-bedrock'],
    },
    { category_id: 'category', format_id: 'proxy', site_id: 'site', models: ['proxy-only'] },
    { category_id: 'other-category', format_id: 'format', site_id: 'site', models: ['other-category-only'] },
    { category_id: 'category', format_id: 'format', site_id: 'archived', models: ['archived-only'] },
    { category_id: 'category', format_id: 'format', site_id: 'missing', models: ['missing-site-only'] },
  ];
  const before = structuredClone(templates);
  const result = choices('AWS', 'aws_bedrock', 33, {
    templates,
    formats: [
      { id: 'format', schema_config: { type: 'aws_bedrock', remote_type: 33 } },
      { id: 'proxy', schema_config: { type: 'aws_claude', remote_type: 14 } },
    ],
    sites: [{ id: 'site' }, { id: 'archived', archived: true }],
  });
  assert.equal(result.length, 10);
  assert.ok(result.includes('custom-bedrock'));
  assert.equal(result.filter((model) => model === 'claude-opus-4-8').length, 1);
  assert.deepEqual(templates, before);
});

test('keeps OpenAI choices and Gemini or unknown services separate from the Claude catalog', () => {
  for (const [family, kind, remoteType] of [
    ['OpenAI', 'api_key', 1],
    ['Azure', 'azure_gpt', 3],
  ]) {
    const result = choices(family, kind, remoteType);
    assert.equal(result.length, 26);
    assert.ok(result.includes('gpt-6-astra'));
    assert.ok(result.every((model) => !model.startsWith('claude-')));
  }
  assert.deepEqual(choices('Google', 'api_key', 24), []);
  assert.deepEqual(choices('Google', 'vertex_gemini', 41), []);
  assert.deepEqual(choices('AWS', 'unknown', 99), []);
  assert.deepEqual(choices('AWS', 'aws_bedrock', 33, { variant: 'legacy' }), []);
  assert.deepEqual(choices('AWS', 'aws_bedrock', 33, { category: undefined }), []);
  assert.deepEqual(choices('AWS', 'aws_bedrock', 33, { format: undefined }), []);
});
