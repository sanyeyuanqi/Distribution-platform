export type SettlementPayableRow = {
  id: string;
  remote_usage_total?: {
    amount?: unknown;
    unit?: unknown;
    covered?: unknown;
    total?: unknown;
  } | null;
  settlement?: {
    usage_amount?: unknown;
    discount_percent?: unknown;
  } | null;
};

const PAYMENT_SCALE = 100000000n;
const DISCOUNT_DIVISOR = 100000000n; // A percentage with six fractional digits.
const DECIMAL_AMOUNT = /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;

function decimalParts(value: unknown): { coefficient: bigint; precision: number } | null {
  if (
    typeof value !== 'string' ||
    value.length > 1001 ||
    value.trim() !== value ||
    !DECIMAL_AMOUNT.test(value)
  ) {
    return null;
  }
  const [whole, fraction = ''] = value.split('.');
  if (whole.length + fraction.length > 1000) return null;
  return { coefficient: BigInt(whole + fraction), precision: fraction.length };
}

function scaledAmount(value: unknown, precision = 8): bigint | null {
  const parsed = decimalParts(value);
  if (parsed === null || parsed.precision > precision) return null;
  return parsed.coefficient * 10n ** BigInt(precision - parsed.precision);
}

function decimalAmount(value: bigint): string {
  const whole = (value / PAYMENT_SCALE).toString();
  const fraction = (value % PAYMENT_SCALE).toString().padStart(8, '0').replace(/0+$/, '');
  return fraction ? `${whole}.${fraction}` : whole;
}

/** Apply the current rate to the server's new-usage projection, rounded HALF_UP to 8 places. */
export function settlementGroupPayable(row: SettlementPayableRow): string | null {
  if (row.remote_usage_total?.unit !== 'USD') return null;
  const usage = decimalParts(row.settlement?.usage_amount);
  const discount = scaledAmount(row.settlement?.discount_percent, 6);
  if (usage === null || discount === null || discount > DISCOUNT_DIVISOR) return null;
  const divisor = 10n ** BigInt(usage.precision) * DISCOUNT_DIVISOR;
  return decimalAmount((usage.coefficient * discount * PAYMENT_SCALE + divisor / 2n) / divisor);
}

/** Summarize already-selected eligible groups; unknown amounts never become a subtotal. */
export function summarizeSettlementPayables(rows: readonly SettlementPayableRow[]): {
  amount: string | null;
  count: number;
  uncomputedCount: number;
} {
  const amounts = new Map<string, bigint | null>();
  for (const row of rows) {
    const amount = scaledAmount(settlementGroupPayable(row));
    if (!amounts.has(row.id)) {
      amounts.set(row.id, amount);
    } else if (amounts.get(row.id) !== amount) {
      // Conflicting copies cannot establish one trustworthy payable for this group.
      amounts.set(row.id, null);
    }
  }

  let total = 0n;
  let uncomputedCount = 0;
  for (const amount of amounts.values()) {
    if (amount === null) uncomputedCount += 1;
    else total += amount;
  }
  return {
    amount: uncomputedCount ? null : decimalAmount(total),
    count: amounts.size,
    uncomputedCount,
  };
}
