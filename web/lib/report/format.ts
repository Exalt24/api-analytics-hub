/**
 * Money and counts as text, in integers.
 *
 * Money is an integer in minor units with its currency beside it (the README's
 * architecture section). It becomes a string here by integer division, never through
 * a float, so a total of 1,234,567 minor units reads 12,345.67 and not 12345.670000001.
 */

/** The currencies with no minor unit. The same list as `_ZERO_DECIMAL` in app/connectors/shopify.py. */
const ZERO_DECIMAL: ReadonlySet<string> = new Set([
  "JPY", "KRW", "VND", "CLP", "ISK", "XAF", "XOF", "BIF", "DJF", "GNF", "KMF", "MGA", "PYG", "RWF", "UGX", "VUV", "XPF",
]);

export type Unit = "minor_currency" | "count";

export function currencyExponent(currency: string): number {
  return ZERO_DECIMAL.has(currency.toUpperCase()) ? 0 : 2;
}

/** `123456n`, USD gives `1234.56`; a count gives `42`. No thousands separator: this is the machine form used in CSV and Excel. */
export function plainAmount(value: bigint, unit: Unit, currency: string | null): string {
  if (unit === "count") return value.toString();
  const exponent = currencyExponent(currency ?? "USD");
  const negative = value < 0n;
  const abs = negative ? -value : value;
  if (exponent === 0) return `${negative ? "-" : ""}${abs}`;
  const scale = 10n ** BigInt(exponent);
  const whole = abs / scale;
  const frac = (abs % scale).toString().padStart(exponent, "0");
  return `${negative ? "-" : ""}${whole}.${frac}`;
}

function groupThousands(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** The reader form: `$1,234.56`, `EUR 1,234.56`, `1,234` for a count. */
export function displayAmount(value: bigint, unit: Unit, currency: string | null): string {
  const plain = plainAmount(value, unit, currency);
  const negative = plain.startsWith("-");
  const body = negative ? plain.slice(1) : plain;
  const [whole = "0", frac] = body.split(".");
  const grouped = groupThousands(whole) + (frac !== undefined ? `.${frac}` : "");
  if (unit === "count") return `${negative ? "-" : ""}${grouped}`;
  const code = (currency ?? "USD").toUpperCase();
  const prefix = code === "USD" ? "$" : `${code} `;
  return `${negative ? "-" : ""}${prefix}${grouped}`;
}

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

export function monthName(month: string): string {
  const m = /^(\d{4})-(\d{2})$/.exec(month);
  if (!m) throw new Error(`not a YYYY-MM month: ${month}`);
  const name = MONTHS[Number(m[2]) - 1];
  if (!name) throw new Error(`not a month: ${month}`);
  return `${name} ${m[1]}`;
}

/** First day of the month and the first day of the next, as YYYY-MM-DD. */
export function monthBounds(month: string): { start: string; end: string } {
  const m = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(month);
  if (!m) throw new Error(`not a YYYY-MM month: ${month}`);
  const year = Number(m[1]);
  const mon = Number(m[2]);
  const nextYear = mon === 12 ? year + 1 : year;
  const nextMon = mon === 12 ? 1 : mon + 1;
  const pad = (n: number): string => String(n).padStart(2, "0");
  return { start: `${m[1]}-${m[2]}-01`, end: `${nextYear}-${pad(nextMon)}-01` };
}
