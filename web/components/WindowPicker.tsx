"use client";

/**
 * The 7 / 30 / 90 day filter.
 *
 * Real buttons in a group with aria-pressed, not a listbox of submit buttons.
 * The W3C pattern for a set of mutually exclusive toggles is a group of buttons;
 * role="listbox" with role="option" on buttons is a documented antipattern and
 * reads incorrectly in screen readers.
 */
type Props = {
  value: number;
  options: number[];
  onChange: (days: number) => void;
  disabled?: boolean;
};

export function WindowPicker({ value, options, onChange, disabled }: Props) {
  return (
    <div className="segmented" role="group" aria-label="Date range">
      {options.map((days) => (
        <button
          key={days}
          type="button"
          aria-pressed={days === value}
          disabled={disabled}
          className={days === value ? "seg active" : "seg"}
          onClick={() => onChange(days)}
        >
          {days} days
        </button>
      ))}
    </div>
  );
}
