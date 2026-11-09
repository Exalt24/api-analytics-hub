import { describe, expect, it } from "vitest";
import { computeTotals, foldDaily, type DailyRow } from "@/lib/report/data";
import { canon, checkFigures, digitTokens, wordTokens } from "@/lib/report/guard";
import { templateSummary } from "@/lib/report/summary";
import { sampleData } from "./report-data";

const data = sampleData();
const check = (...texts: string[]) => checkFigures(texts, data);

describe("canon", () => {
  it("drops commas, sign, and leading and trailing zeros", () => {
    expect(canon("1,234.50")).toBe("1234.5");
    expect(canon("007")).toBe("7");
    expect(canon("-5")).toBe("5");
    expect(canon("3.0")).toBe("3");
    expect(canon("0.50")).toBe("0.5");
    expect(canon("0")).toBe("0");
  });
});

describe("digits", () => {
  it("accepts every figure of the data in any of its written forms", () => {
    const v = check("Orders totalled 38 over 3 days.", "Gross sales were $3,722.21, or 3722.21, with 8 orders on a quiet day and 15 on a busy day.", "Refunds were $35.50.");
    expect(v).toEqual({ ok: true, violations: [] });
  });

  it("rejects a figure that is in no row, however small", () => {
    expect(check("Orders totalled 39.").violations).toEqual(["39"]);
    expect(check("There was 1 refund.").violations).toEqual(["1"]);
    expect(check("Sales were $4,000 and refunds $12.").violations).toEqual(["4,000", "12"]);
  });

  it("rejects a percentage, a rounded figure and a worked out sum", () => {
    expect(check("Orders rose 12% on the month.").violations).toEqual(["12"]);
    expect(check("Gross sales were about $3.7k.").violations).toEqual(["3.7"]);
    expect(check("Gross plus refunds came to $3,757.71.").violations).toEqual(["3,757.71"]);
    expect(check("Sales were $3,722.2.").violations).toEqual(["3,722.2"]); // one digit short of 3,722.21
  });

  it("does not read a sign as a way round: -38 is still the figure 38, and -39 is not", () => {
    expect(check("Orders moved by -38.").ok).toBe(true);
    expect(check("Orders moved by -39.").ok).toBe(false);
  });

  it("reads digits glued to letters, and full width digits", () => {
    expect(check("Q4 was strong.").violations).toEqual(["4"]);
    expect(check("Orders totalled ３８.").ok).toBe(true); // full width 38
    expect(check("Orders totalled ９９.").ok).toBe(false); // full width 99
  });

  it("refuses a digit from another script outright", () => {
    expect(check("Orders totalled ٣٨.").violations).toEqual(["non-ASCII digit"]);
  });

  it("reads every text it is given, not just the first", () => {
    expect(check("Fine, 38 orders.", "But 77 refunds.").violations).toEqual(["77"]);
  });

  it("digitTokens keeps a comma list apart from a thousands separator", () => {
    expect(digitTokens("5, 6 and 1,000")).toEqual(["5", "6", "1,000"]);
  });
});

describe("dates are labels, not figures, when they are the data's own", () => {
  it("accepts the report month, a day in the data, in ISO and in words", () => {
    expect(check("For October 2026 the best day was 2026-10-01.").ok).toBe(true);
    expect(check("Peak orders came on October 1 and again on Oct 3rd.").ok).toBe(true);
    expect(check("It peaked on 3 October.").ok).toBe(true);
    expect(check("The month is 2026-10.").ok).toBe(true);
  });

  it("rejects a day that is not one of the data's days, and another year", () => {
    expect(check("It peaked on October 9.").violations).toEqual(["9"]);
    expect(check("It peaked on 2026-10-09.").violations).toContain("2026");
    expect(check("It peaked on 2026-10-09.").ok).toBe(false);
    expect(check("It peaked on October 1, 2025.").ok).toBe(false);
  });

  it("does not let a date hide a figure next to it", () => {
    expect(check("On October 1 there were 41 orders.").violations).toEqual(["41"]);
  });
});

describe("numbers written as words", () => {
  const words = (t: string) => wordTokens(t).map(([w, v]) => `${w}=${v}`);

  it("reads compounds, hundreds, scales and ordinals", () => {
    expect(words("forty-two orders")).toEqual(["forty-two=42"]);
    expect(words("one hundred and five")).toEqual(["one-hundred-five=105"]);
    expect(words("two thousand three hundred")).toEqual(["two-thousand-three-hundred=2300"]);
    expect(words("a hundred orders")).toEqual(["hundred=100"]);
    expect(words("the twenty-first day")).toEqual(["twenty-first=21"]);
    expect(words("five six")).toEqual(["five=5", "six=6"]);
    expect(words("twenty twenty")).toEqual(["twenty=20", "twenty=20"]);
  });

  it("reads the quantity words that carry no digit", () => {
    expect(words("a dozen")).toEqual(["dozen=12"]);
    expect(words("twice as many")).toEqual(["twice=2"]);
    expect(words("half of it")).toEqual(["half=0.5"]);
    expect(words("hundreds of orders")).toEqual(["hundreds=hundreds"]);
  });

  it("rejects a number word that is not a figure of the data, and accepts one that is", () => {
    expect(check("There were forty-two orders.").violations).toEqual(["forty-two"]);
    expect(check("Orders came to thirty-eight.").ok).toBe(true); // 38 is the month total
    expect(check("Eight orders on the fifth day.").ok).toBe(false); // fifth = 5 is not in the data
    expect(check("Refunds hit a dozen.").violations).toEqual(["dozen"]);
    expect(check("Sales were half what they were.").violations).toEqual(["half"]);
    expect(check("Sales were in the thousands.").violations).toEqual(["thousands"]);
    expect(check("One hundred and five orders.").violations).toEqual(["one-hundred-five"]);
  });

  it("reads one as a number except in 'one of' and 'no one'", () => {
    expect(check("One of the best months.").ok).toBe(true);
    expect(check("No one expected it.").ok).toBe(true);
    expect(check("There was one refund.").violations).toEqual(["one"]);
  });
});

describe("the template summary passes its own guard", () => {
  // a seeded generator, so the 300 worlds are the same on every run
  function rng(seed: number): () => number {
    let s = seed >>> 0;
    return () => {
      s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
      return s / 2 ** 32;
    };
  }

  const KINDS = [
    { key: "orders", label: "Orders", unit: "count" as const, currency: null },
    { key: "gross_sales", label: "Gross sales", unit: "minor_currency" as const, currency: "USD" },
    { key: "refunds", label: "Refunds", unit: "minor_currency" as const, currency: "EUR" },
    { key: "payments", label: "Payments", unit: "count" as const, currency: null },
    { key: "ad_leads", label: "Local Services leads", unit: "count" as const, currency: null },
    { key: "spend_jpy", label: "Spend", unit: "minor_currency" as const, currency: "JPY" },
  ];

  it("holds over 300 generated months, and an invented figure is refused in every one", () => {
    const rand = rng(20261109);
    let invented = 0;
    for (let world = 0; world < 300; world += 1) {
      const rows: DailyRow[] = [];
      const kinds = KINDS.filter(() => rand() < 0.6);
      if (kinds.length === 0) kinds.push(KINDS[0]!);
      const days = 1 + Math.floor(rand() * 28);
      for (const k of kinds) {
        for (let d = 1; d <= days; d += 1) {
          if (rand() < 0.2) continue;
          const big = rand() < 0.2 ? 10 ** (6 + Math.floor(rand() * 4)) : 1000;
          rows.push({
            date: `2026-10-${String(d).padStart(2, "0")}`,
            metricKey: k.key,
            label: k.label,
            unit: k.unit,
            currency: k.currency,
            value: BigInt(Math.floor(rand() * big)),
          });
        }
      }
      if (rows.length === 0) continue;
      const daily = foldDaily(rows);
      const world1 = { tenantName: "T", month: "2026-10", daily, totals: computeTotals(daily) };
      const t = templateSummary(world1);
      const texts = [t.headline, ...t.highlights];
      expect(checkFigures(texts, world1), `world ${world}`).toEqual({ ok: true, violations: [] });

      // control: a total plus one is a figure the data does not hold, unless it collides with another figure
      const first = world1.totals[0]!;
      const planted = String(first.total + 1n);
      const known = new Set([...world1.daily.map((r) => String(r.value)), ...world1.totals.map((x) => String(x.total))]);
      if (first.unit === "count" && !known.has(planted)) {
        invented += 1;
        expect(checkFigures([`${first.label} was ${planted}.`], world1).ok, `world ${world}`).toBe(false);
      }
    }
    expect(invented).toBeGreaterThan(100);
  });
});
