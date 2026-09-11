import assert from 'node:assert/strict';
import test from 'node:test';
import { settlementGroupPayable, summarizeSettlementPayables } from '../src/settlement-selection.ts';

const row = (id, amount, discount = '100') => ({
  id,
  remote_usage_total: { amount, unit: 'USD', covered: 1, total: 1 },
  settlement: { usage_amount: amount, discount_percent: discount },
});

test('the current percentage applies to the displayed USD total', () => {
  assert.equal(settlementGroupPayable(row('a', '100', '80')), '80');
  assert.equal(settlementGroupPayable(row('a', '100', '100.000000')), '100');
  assert.equal(settlementGroupPayable(row('a', '100.00000001', '80.123456')), '80.12345601');
});

test('valid zero percentages and zero consumption produce known zero', () => {
  assert.equal(settlementGroupPayable(row('a', '100', '0')), '0');
  assert.equal(settlementGroupPayable(row('a', '100', '0.000000')), '0');
  assert.equal(settlementGroupPayable(row('a', '0', '80')), '0');
  assert.equal(settlementGroupPayable(row('a', null, '0')), null);
  assert.equal(settlementGroupPayable(row('a', '0', null)), null);
  assert.deepEqual(summarizeSettlementPayables([]), { amount: '0', count: 0, uncomputedCount: 0 });
  assert.deepEqual(summarizeSettlementPayables([row('a', '10', '0'), row('b', '0', '80')]), {
    amount: '0',
    count: 2,
    uncomputedCount: 0,
  });
});

test('eight-place HALF_UP distinguishes below, exactly at, and above the midpoint', () => {
  assert.equal(settlementGroupPayable(row('a', '0.00000001', '49.999999')), '0');
  assert.equal(settlementGroupPayable(row('a', '0.00000001', '50')), '0.00000001');
  assert.equal(settlementGroupPayable(row('a', '0.00000001', '50.000001')), '0.00000001');
  assert.equal(settlementGroupPayable(row('a', '0.00000003', '50')), '0.00000002');
  assert.equal(settlementGroupPayable(row('a', '1.99999999', '50')), '1');
});

test('consumption keeps more than eight fractional places until the final payment is rounded', () => {
  assert.equal(settlementGroupPayable(row('a', '0.123456789', '80')), '0.09876543');
  assert.equal(settlementGroupPayable(row('a', '0.000000009', '80')), '0.00000001');
  assert.equal(settlementGroupPayable(row('a', '0.0000000051', '80')), '0');
  assert.equal(settlementGroupPayable(row('a', '0.000000004999999999')), '0');
  assert.equal(settlementGroupPayable(row('a', '0.000000005000000001')), '0.00000001');
  assert.equal(settlementGroupPayable(row('a', '1.000000000')), '1');
});

test('large amounts and eight-decimal tails retain precision through multiplication and addition', () => {
  assert.equal(
    settlementGroupPayable(row('a', '9007199254740993.12345678', '80')),
    '7205759403792794.49876542',
  );
  assert.deepEqual(
    summarizeSettlementPayables([
      row('a', '9007199254740993.12345678'),
      row('b', '0.00000001'),
      row('c', '0.87654322'),
    ]),
    { amount: '9007199254740994.00000001', count: 3, uncomputedCount: 0 },
  );
  assert.equal(summarizeSettlementPayables([row('a', '0.1'), row('b', '0.2')]).amount, '0.3');
});

test('totals sum the same rounded payments displayed for each selected group', () => {
  assert.deepEqual(
    summarizeSettlementPayables([row('a', '0.00000001', '50'), row('b', '0.00000001', '50')]),
    { amount: '0.00000002', count: 2, uncomputedCount: 0 },
  );
});

test('historical payable, payment factor, and applied discounts never affect this formula', () => {
  const item = row('a', '100', '80');
  Object.assign(item.settlement, {
    payable_usdt: '999',
    due_usd: '900',
    payment_factor: '50',
    applied_discount_percents: ['1', '2'],
  });
  assert.equal(settlementGroupPayable(item), '80');
  assert.deepEqual(summarizeSettlementPayables([item]), {
    amount: '80',
    count: 1,
    uncomputedCount: 0,
  });
  delete item.settlement.discount_percent;
  assert.equal(settlementGroupPayable(item), null);
});

test('remote coverage and cumulative counters do not replace the new-usage projection', () => {
  const item = row('a', '125', '80');
  item.remote_usage_total.covered = 1;
  item.remote_usage_total.total = 3;
  assert.equal(settlementGroupPayable(item), '100');
  assert.equal(summarizeSettlementPayables([item]).amount, '100');
  item.remote_usage_total.covered = 0;
  assert.equal(settlementGroupPayable(item), '100');
  item.remote_usage_total.amount = null;
  assert.equal(settlementGroupPayable(item), '100');
  item.settlement.usage_amount = null;
  assert.equal(settlementGroupPayable(item), null);
});

test('unknown or missing inputs invalidate the total instead of reporting a known subtotal', () => {
  const missing = [
    row('null-amount', null),
    row('null-discount', '100', null),
    { id: 'missing-remote', settlement: { usage_amount: '100', discount_percent: '80' } },
    { id: 'missing-settlement', remote_usage_total: { amount: '100', unit: 'USD' } },
    { ...row('missing-projection', '100'), settlement: {} },
    { ...row('missing-discount', '100'), settlement: { usage_amount: '100' } },
    { ...row('null-remote', '100'), remote_usage_total: null },
    { ...row('null-settlement', '100'), settlement: null },
    { ...row('missing-usage', '100'), settlement: { discount_percent: '80' } },
  ];
  for (const item of missing) assert.equal(settlementGroupPayable(item), null);
  assert.deepEqual(summarizeSettlementPayables([row('known', '500'), ...missing]), {
    amount: null,
    count: 10,
    uncomputedCount: 9,
  });
  for (const unit of ['CNY', 'USDT', 'usd', '', null, undefined]) {
    const item = row('a', '100');
    item.remote_usage_total.unit = unit;
    assert.equal(settlementGroupPayable(item), null);
  }
});

test('invalid numbers and over-precise percentages are never coerced or truncated', () => {
  const invalid = [
    '',
    ' ',
    ' 1',
    '1 ',
    '1\n',
    '-1',
    '-0',
    '+1',
    '01',
    '00.1',
    '.1',
    '1.',
    '1e3',
    '1,000',
    'NaN',
    'Infinity',
    1,
    0,
    NaN,
    Infinity,
    true,
    undefined,
    {},
  ];
  for (const value of invalid) {
    assert.equal(settlementGroupPayable(row('a', value)), null);
    const item = row('a', '100');
    item.settlement.discount_percent = value;
    assert.equal(settlementGroupPayable(item), null);
  }
  for (const value of ['0.1234567', '80.0000000', '100.000001', '101']) {
    assert.equal(settlementGroupPayable(row('a', '100', value)), null);
  }
  assert.equal(settlementGroupPayable(row('a', '1'.repeat(1001))), null);
  assert.equal(settlementGroupPayable(row('a', `0.${'0'.repeat(999)}1`)), null);
});

test('deduplicates IDs and equivalent computed payments without changing input', () => {
  const rows = Object.freeze([
    Object.freeze(row('a', '100', '80')),
    Object.freeze(row('a', '100.00000000', '80.000000')),
    Object.freeze(row('b', '2.25')),
    Object.freeze(row('b', '4.5', '50')),
  ]);
  const before = structuredClone(rows);
  assert.deepEqual(summarizeSettlementPayables(rows), {
    amount: '82.25',
    count: 2,
    uncomputedCount: 0,
  });
  assert.deepEqual(rows, before);
});

test('duplicate unknown or conflicting groups are counted once and remain uncomputed', () => {
  const rows = [
    row('unknown', null),
    row('unknown', '5'),
    row('conflict', '100', '80'),
    row('conflict', '100', '70'),
    row('conflict', '100', '80'),
    row('known', '10'),
  ];
  const expected = { amount: null, count: 3, uncomputedCount: 2 };
  assert.deepEqual(summarizeSettlementPayables(rows), expected);
  assert.deepEqual(summarizeSettlementPayables([...rows].reverse()), expected);
});

test('order projection settles only new usage at the current frozen-preview rate', () => {
  const item = row('again', '125', '80');
  item.settlement = { usage_amount: '25', discount_percent: '60' };
  assert.equal(settlementGroupPayable(item), '15');
  assert.equal(summarizeSettlementPayables([item]).amount, '15');
  item.settlement.usage_amount = null;
  assert.equal(settlementGroupPayable(item), null);
  item.settlement.usage_amount = '0';
  assert.equal(settlementGroupPayable(item), '0');
});

test('legacy financials and nested order shapes are not accepted as current settlement data', () => {
  const item = row('legacy', '100', '80');
  item.settlement = { financials: { discount_percent: '80' } };
  assert.equal(settlementGroupPayable(item), null);
  item.settlement = { order: { usage_amount: '100', discount_percent: '80' } };
  assert.equal(settlementGroupPayable(item), null);
  assert.deepEqual(summarizeSettlementPayables([item]), {
    amount: null,
    count: 1,
    uncomputedCount: 1,
  });
});
