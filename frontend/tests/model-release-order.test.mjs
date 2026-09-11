import assert from 'node:assert/strict';
import test from 'node:test';
import { modelReleaseDate, sortModelsByReleaseDate } from '../src/model-release-order.ts';

test('sorts verified releases newest first without changing the input', () => {
  const models = Object.freeze([
    'claude-haiku-4-5-20251001',
    'claude-opus-4-7',
    'gpt-6-astra',
    'claude-sonnet-4-6',
    'gpt-5.6-sol',
  ]);
  const before = [...models];

  assert.deepEqual(sortModelsByReleaseDate(models), [
    'gpt-6-astra',
    'gpt-5.6-sol',
    'claude-opus-4-7',
    'claude-sonnet-4-6',
    'claude-haiku-4-5-20251001',
  ]);
  assert.deepEqual(models, before);
});

test('deduplicates trimmed IDs but preserves distinct provider IDs and variants', () => {
  const models = [
    ' claude-opus-4-7 ',
    'claude-opus-4-7',
    'anthropic/claude-opus-4-7',
    'claude-opus-4-7-thinking',
    ' gpt-6-astra ',
    'gpt-6-astra',
  ];

  assert.deepEqual(sortModelsByReleaseDate(models), [
    'gpt-6-astra',
    'claude-opus-4-7',
    'anthropic/claude-opus-4-7',
    'claude-opus-4-7-thinking',
  ]);
});

test('unknown release dates remain at the end in their original order', () => {
  const unknowns = [
    'private-z-model',
    'private-a-model',
    'private-unknown-20991231',
    'constructor',
    '__proto__',
  ];
  for (const model of unknowns) assert.equal(modelReleaseDate(model), null);

  assert.deepEqual(
    sortModelsByReleaseDate([
      unknowns[0],
      'claude-sonnet-4-6',
      unknowns[1],
      'gpt-6-astra',
      ...unknowns.slice(2),
    ]),
    ['gpt-6-astra', 'claude-sonnet-4-6', ...unknowns],
  );
  assert.deepEqual(sortModelsByReleaseDate(unknowns), unknowns);
});

test('models released on the same day keep their original order', () => {
  const sameDay = ['gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-5.6-luna'];
  for (const model of sameDay) assert.equal(modelReleaseDate(model), '2026-07-09');

  assert.deepEqual(
    sortModelsByReleaseDate([sameDay[0], 'claude-opus-4-7', sameDay[1], 'gpt-6-astra', sameDay[2]]),
    ['gpt-6-astra', ...sameDay, 'claude-opus-4-7'],
  );
});

test('uses the confirmed public release date rather than an eight-digit ID suffix', () => {
  assert.equal(modelReleaseDate('claude-haiku-4-5-20251001'), '2025-10-15');
  assert.equal(modelReleaseDate('claude-sonnet-4-6'), '2026-02-17');
  assert.equal(modelReleaseDate('claude-opus-4-7'), '2026-04-16');
  assert.equal(modelReleaseDate('gpt-6-astra'), '2026-09-03');
  assert.equal(modelReleaseDate('private-unknown-20260910'), null);
  assert.equal(modelReleaseDate('private-unknown-2026-09-10'), null);

  assert.deepEqual(sortModelsByReleaseDate(['private-unknown-20991231', 'claude-haiku-4-5-20251001']), [
    'claude-haiku-4-5-20251001',
    'private-unknown-20991231',
  ]);
});

test('resolves provider prefixes and supported aliases without replacing displayed IDs', () => {
  const aliases = [
    ['openai/gpt-6-astra', '2026-09-03'],
    ['publishers/openai/gpt-6-astra', '2026-09-03'],
    ['anthropic/claude-opus-4-7', '2026-04-16'],
    ['publishers/anthropic/claude-sonnet-4-6', '2026-02-17'],
    ['claude-sonnet-4.6', '2026-02-17'],
    ['claude-opus-4-7-thinking-high', '2026-04-16'],
    ['anthropic.claude-haiku-4-5-20251001-v1:0', '2025-10-15'],
    ['us.anthropic.claude-haiku-4-5-20251001-v1:0', '2025-10-15'],
    ['global.anthropic.claude-haiku-4-5-20251001-v1:0', '2025-10-15'],
    ['publishers/anthropic/claude-haiku-4-5@20251001', '2025-10-15'],
  ];
  for (const [model, expectedDate] of aliases) {
    assert.equal(modelReleaseDate(model), expectedDate, model);
    assert.deepEqual(sortModelsByReleaseDate([model]), [model]);
  }
});

test('sorts merged candidates and existing selections as one list', () => {
  const candidates = Object.freeze([
    'claude-haiku-4-5-20251001',
    'private-candidate-model',
    'claude-sonnet-4-6',
  ]);
  const selected = Object.freeze([
    'gpt-6-astra',
    'claude-opus-4-7',
    'gpt-5.6-sol',
    'claude-haiku-4-5-20251001',
    'private-selected-model',
  ]);

  assert.deepEqual(sortModelsByReleaseDate([...candidates, ...selected]), [
    'gpt-6-astra',
    'gpt-5.6-sol',
    'claude-opus-4-7',
    'claude-sonnet-4-6',
    'claude-haiku-4-5-20251001',
    'private-candidate-model',
    'private-selected-model',
  ]);
  assert.equal(candidates[0], 'claude-haiku-4-5-20251001');
  assert.equal(selected[0], 'gpt-6-astra');
});

test('handles an empty model list', () => {
  assert.deepEqual(sortModelsByReleaseDate([]), []);
});
