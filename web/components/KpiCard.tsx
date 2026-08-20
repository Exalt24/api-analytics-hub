/**
 * One number with its label. Deliberately dumb: no fetching, no formatting rules
 * of its own, so a designer replacing this file cannot change what the number
 * means. That separation is what makes "drop a Figma design in later" cheap.
 */
type Props = {
  label: string;
  value: string | null;
  hint?: string;
};

export function KpiCard({ label, value, hint }: Props) {
  return (
    <div className="card">
      <div className="card-label">{label}</div>
      {/* An unavailable metric says so. Rendering a dash instead of 0 is the whole
          point: zero is a business result, unavailable is a permissions or sync
          problem, and they must never look the same. */}
      <div className={value === null ? "card-value muted" : "card-value"}>
        {value ?? "not available"}
      </div>
      {hint ? <div className="card-hint">{hint}</div> : null}
    </div>
  );
}
