import type { InputHTMLAttributes, SelectHTMLAttributes, TextareaHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

/**
 * Form controls.
 *
 * `aria-invalid` is styled, and not with colour alone: an invalid field also
 * carries a border *and* a thicker ring, because FR-059 forbids conveying state
 * by colour on its own and a red border on a white background is the exact case
 * that rule exists for. The `aria-describedby` wiring that announces the reason
 * is the caller's, since only the caller knows which message belongs to which
 * field.
 */
const control = "w-full rounded-md border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground aria-invalid:border-destructive aria-invalid:ring-2 aria-invalid:ring-destructive/40 disabled:opacity-60";

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn(control, "h-9", className)} {...props} />;
}

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(control, "min-h-16", className)} {...props} />;
}

export function Select({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={cn(control, "h-9", className)} {...props} />;
}

/**
 * A checkbox with its own label.
 *
 * Wrapping rather than using `id`/`htmlFor` alone: a checkbox with a visually
 * distant label is a target a pointer misses, and the accessible name is the
 * same either way. The label is the accessible name here, so it is required
 * rather than optional.
 */
export function Checkbox({
  label,
  description,
  className,
  id,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { label: string; description?: string }) {
  const inputId = id ?? `checkbox-${props.name ?? label.replace(/\W+/g, "-").toLowerCase()}`;
  return (
    <div className={cn("flex items-start gap-2", className)}>
      <input
        id={inputId}
        type="checkbox"
        className="mt-0.5 size-4 shrink-0 rounded-sm border accent-accent"
        {...props}
      />
      <div className="grid gap-0.5">
        <label htmlFor={inputId} className="text-sm font-medium leading-tight">
          {label}
        </label>
        {description !== undefined ? (
          <p id={`${inputId}-description`} className="text-xs text-muted-foreground">
            {description}
          </p>
        ) : null}
      </div>
    </div>
  );
}