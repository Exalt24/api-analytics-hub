/**
 * The figure guard: a summary may cite only numbers that are in the data it was
 * written from. It reads digits and numbers written as words, and returns every
 * number it could not find in the data.
 *
 * The same idea as `verify_grounded` in app/ai/catalog_copy.py and the monthly report
 * narrative check in app/report/, applied to the export summary.
 *
 * WHAT IS ALLOWED. Each daily value, each month total, and each "days with data" count
 * (money in its plain decimal form, so 1,234.56 and 1234.56 are the same figure). The
 * report's own month and year, and a date that is one of the data's own days, are
 * dates and not figures, so they are removed before the numbers are read.
 *
 * WHAT IS REJECTED. Any other number, however small, including a percentage, a rounded
 * figure ($1.2k reads as 1.2 and 1000), a sum the model worked out, a number written
 * as a word (forty-two, a dozen, twice, hundreds) and a digit from a script other than
 * ASCII. It leans to over-rejecting on purpose: the cost of a false rejection is the
 * plain template summary, and the cost of a miss is a wrong figure in a client's report.
 * `one` is read as a number except in "one of" and "no one".
 *
 * NOT CAUGHT, and stated rather than hidden: a right figure in the wrong role, a wrong
 * direction word (up for down), a cause the data does not show, and roman numerals.
 */
import type { ReportData } from "@/lib/report/data";
import { monthName, plainAmount } from "@/lib/report/format";

/** Canonical text of a number: no commas, no sign, no leading or trailing zeros. */
export function canon(raw: string): string {
  let s = raw.replace(/,/g, "").replace(/^[-+]/, "");
  if (s.includes(".")) s = s.replace(/0+$/, "").replace(/\.$/, "");
  s = s.replace(/^0+(?=\d)/, "");
  return s === "" ? "0" : s;
}

/** Every figure the data states, in canonical form. */
export function allowedFigures(data: ReportData): Set<string> {
  const out = new Set<string>();
  const add = (plain: string): void => {
    out.add(canon(plain));
  };
  for (const r of data.daily) add(plainAmount(r.value, r.unit, r.currency));
  for (const t of data.totals) {
    add(plainAmount(t.total, t.unit, t.currency));
    add(String(t.daysWithData));
  }
  return out;
}

const MONTH_NAMES = [
  "january", "february", "march", "april", "may", "june",
  "july", "august", "september", "october", "november", "december",
];

function dateSet(data: ReportData): Set<string> {
  return new Set(data.daily.map((r) => r.date));
}

/** Remove the month phrase and the data's own dates, which are labels and not figures. */
function stripDates(text: string, data: ReportData): string {
  const days = dateSet(data);
  const [year = "", mm = ""] = data.month.split("-");
  const monthLong = monthName(data.month).toLowerCase();
  let out = text;
  // "October 2026" and "2026-10", the month the report is for
  out = out.replace(new RegExp(monthLong.replace(" ", "\\s+"), "gi"), " ");
  out = out.replace(new RegExp(`(?<![\\d-])${year}-${mm}(?![\\d-])`, "g"), " ");
  // an ISO date that is one of the data's own days
  out = out.replace(/(?<![\d-])(\d{4}-\d{2}-\d{2})(?![\d-])/g, (m, d: string) => (days.has(d) ? " " : m));
  // "October 14", "Oct 14th" or "14 October", only when that day is in the data and any year is the report's
  const monthWord = "(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\\.?";
  const monthIndex = (w: string): number => {
    const lower = w.toLowerCase();
    const i = MONTH_NAMES.findIndex((n) => n === lower || n.slice(0, 3) === lower.slice(0, 3));
    return i + 1;
  };
  const inData = (mon: number, day: number, yr: string | undefined): boolean => {
    if (yr !== undefined && yr !== year) return false;
    const iso = `${year}-${String(mon).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    return days.has(iso);
  };
  out = out.replace(
    new RegExp(`\\b${monthWord}\\s+(\\d{1,2})(?:st|nd|rd|th)?(?:,?\\s+(\\d{4}))?(?!\\d)`, "gi"),
    (m, mon: string, day: string, yr: string | undefined) => (inData(monthIndex(mon), Number(day), yr) ? " " : m),
  );
  out = out.replace(
    new RegExp(`\\b(\\d{1,2})(?:st|nd|rd|th)?\\s+${monthWord}(?:,?\\s+(\\d{4}))?(?!\\d)`, "gi"),
    (m, day: string, mon: string, yr: string | undefined) => (inData(monthIndex(mon), Number(day), yr) ? " " : m),
  );
  return out;
}

const DIGITS = /\d+(?:,\d{3})*(?:\.\d+)?/g;

/** The digit numbers in a text, as written (commas kept), after dates are removed. */
export function digitTokens(text: string): string[] {
  return text.match(DIGITS) ?? [];
}

const UNITS: Record<string, number> = {
  zero: 0, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10,
  eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15, sixteen: 16, seventeen: 17, eighteen: 18, nineteen: 19,
};
const TENS: Record<string, number> = {
  twenty: 20, thirty: 30, forty: 40, fifty: 50, sixty: 60, seventy: 70, eighty: 80, ninety: 90,
};
const SCALES: Record<string, number> = { hundred: 100, thousand: 1000, million: 1_000_000, billion: 1_000_000_000 };
const ORDINALS: Record<string, string> = {
  first: "one", second: "two", third: "three", fourth: "four", fifth: "five", sixth: "six", seventh: "seven",
  eighth: "eight", ninth: "nine", tenth: "ten", eleventh: "eleven", twelfth: "twelve", thirteenth: "thirteen",
  fourteenth: "fourteen", fifteenth: "fifteen", sixteenth: "sixteen", seventeenth: "seventeen", eighteenth: "eighteen",
  nineteenth: "nineteen", twentieth: "twenty", thirtieth: "thirty", fortieth: "forty", fiftieth: "fifty",
  sixtieth: "sixty", seventieth: "seventy", eightieth: "eighty", ninetieth: "ninety", hundredth: "hundred",
  thousandth: "thousand", millionth: "million",
};
/** Words that state a quantity without a digit. The value is what the word asserts; the plural forms assert none, so they never match. */
const FIXED: Record<string, string> = {
  half: "0.5", quarter: "0.25", dozen: "12", twice: "2", double: "2", doubled: "2", triple: "3", tripled: "3", thrice: "3",
  hundreds: "hundreds", thousands: "thousands", millions: "millions", dozens: "dozens", billions: "billions",
};

type Kind = "unit" | "tens" | "scale";

function wordKind(w: string): Kind | undefined {
  if (w in UNITS) return "unit";
  if (w in TENS) return "tens";
  if (w in SCALES) return "scale";
  return undefined;
}

/** Numbers written as words: [the words as written, canonical value]. */
export function wordTokens(text: string): Array<[string, string]> {
  const words = text.toLowerCase().split(/[^a-z]+/).filter(Boolean);
  const out: Array<[string, string]> = [];
  let i = 0;
  while (i < words.length) {
    const raw = words[i]!;
    const w = ORDINALS[raw] ?? raw;
    const fixed = FIXED[raw];
    if (fixed !== undefined) {
      out.push([raw, canon(fixed)]);
      i += 1;
      continue;
    }
    if (wordKind(w) === undefined) {
      i += 1;
      continue;
    }
    // "one of" and "no one" are not figures
    if (w === "one" && raw === "one" && (words[i + 1] === "of" || words[i + 1] === "another" || words[i - 1] === "no")) {
      i += 1;
      continue;
    }
    // read one number: a run of number words, `and` allowed after hundred or thousand
    let total = 0;
    let current = 0;
    let last: Kind | undefined;
    const start = i;
    while (i < words.length) {
      const rawWord = words[i]!;
      const word = ORDINALS[rawWord] ?? rawWord;
      const kind = wordKind(word);
      if (kind === undefined) {
        const next = words[i + 1];
        const nextWord = next === undefined ? undefined : (ORDINALS[next] ?? next);
        if (rawWord === "and" && last === "scale" && nextWord !== undefined && wordKind(nextWord) !== undefined && wordKind(nextWord) !== "scale") {
          i += 1;
          continue;
        }
        break;
      }
      if (last === "unit" && (kind === "unit" || kind === "tens")) break; // "five six": two numbers
      if (last === "tens" && kind === "tens") break;
      if (last === "tens" && kind === "unit" && UNITS[word]! >= 10) break;
      if (kind === "unit") current += UNITS[word]!;
      else if (kind === "tens") current += TENS[word]!;
      else if (word === "hundred") current = (current || 1) * 100;
      else {
        total += (current || 1) * SCALES[word]!;
        current = 0;
      }
      last = kind;
      i += 1;
    }
    const written = words.slice(start, i).filter((x) => x !== "and").join("-");
    out.push([written, String(total + current)]);
  }
  return out;
}

export type GuardVerdict = { ok: boolean; violations: string[] };

/**
 * Check every text of a summary against the data. `violations` lists each number
 * that is not a figure of the data, as written, once each.
 */
export function checkFigures(texts: string[], data: ReportData): GuardVerdict {
  const allowed = allowedFigures(data);
  const bad: string[] = [];
  const flag = (s: string): void => {
    if (!bad.includes(s)) bad.push(s);
  };
  for (const original of texts) {
    const text = stripDates(original.normalize("NFKC"), data);
    if (/[^\x00-\x7F]/.test(text) && /\p{Nd}/u.test(text.replace(/[0-9]/g, ""))) flag("non-ASCII digit");
    for (const token of digitTokens(text)) {
      if (!allowed.has(canon(token))) flag(token);
    }
    for (const [written, value] of wordTokens(text)) {
      if (!allowed.has(value)) flag(written);
    }
  }
  return { ok: bad.length === 0, violations: bad };
}
