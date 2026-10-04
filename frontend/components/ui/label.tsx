import type { LabelHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * A field label.
 *
 * `htmlFor` is required by the type, not by convention: a label without it
 * names nothing, and a control with no programmatic name fails FR-058 while
 * looking perfectly usable with a mouse. `required` renders the asterisk, and
 * `aria-hidden` on it, so the required state is announced as *required* rather
 * than as a character.
 */
export interface LabelProps extends LabelHTMLAttributes<HTMLLabelElement> {
  required?: boolean;
  children: ReactNode;
}

export function Label({ className, required, children, ...props }: LabelProps) {
  return (
    <label className={cn("text-sm font-medium leading-tight", className)} {...props}>
      {children}
      {required ? (
        <span aria-hidden="true" className="ml-0.5 text-destructive">
          *
        </span>
      ) : null}
    </label>
  );
}