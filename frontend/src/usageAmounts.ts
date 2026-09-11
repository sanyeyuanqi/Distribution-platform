export function usageAmountText(value: unknown): string | null {
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  const match = /^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(String(value));
  if (!match) return null;
  const exponent = Number(match[4] || 0);
  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 1000) return null;
  // Expand Decimal strings without losing small fractions or large integer digits.
  const digits = match[2] + (match[3] || '');
  const point = match[2].length + exponent;
  const whole = point <= 0 ? '0' : digits.slice(0, point).padEnd(point, '0');
  const fraction = point <= 0 ? '0'.repeat(-point) + digits : digits.slice(point);
  const integer = whole.replace(/^0+(?=\d)/, '').replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const decimals = fraction.replace(/0+$/, '').padEnd(2, '0');
  const sign = match[1] && /[1-9]/.test(whole + fraction) ? '-' : '';
  return `${sign}${integer}.${decimals}`;
}
