/**
 * Class-name joining.
 *
 * Two arguments, deliberately. `clsx` flattens conditionals and
 * `tailwind-merge` drops the earlier class when a later one contradicts it, so
 * a caller can pass `className` last and have it win over a component default.
 * Neither is used anywhere else in the app: adding a third argument is how
 * class precedence becomes undecidable across a codebase.
 */
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/**
 * An ISO 8601 instant, rendered for a reader.
 *
 * Returns `null` rather than a placeholder for a missing value: an absent
 * timestamp and a broken one are different facts, and rendering both as "—"
 * makes them indistinguishable. `toLocaleString` on the client means the
 * operator reads their own timezone's format, which is not the server's.
 */
export function formatTimestamp(value: string | null | undefined): string | null {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }
  return parsed.toLocaleString();
}

/** An ISO instant as a relative age: "3 minutes ago", "2 days ago". */
export function formatRelative(value: string | null | undefined): string | null {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }
  const seconds = Math.round((Date.now() - parsed.getTime()) / 1000);
  const units: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ["second", 60],
    ["minute", 60],
    ["hour", 24],
    ["day", 7],
    ["week", 4.34524],
    ["month", 12],
    ["year", Number.POSITIVE_INFINITY],
  ];
  let amount = seconds;
  for (const [unit, size] of units) {
    if (Math.abs(amount) < size) {
      return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(
        -Math.round(amount),
        unit,
      );
    }
    amount = amount / size;
  }
  return null;
}