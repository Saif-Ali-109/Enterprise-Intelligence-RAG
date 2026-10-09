import type { HTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * Layout surfaces.
 *
 * `Card` is a `<section>` with an optional heading wired by `aria-labelledby`,
 * rather than a `<div>`: a region of the page that a screen-reader user can jump
 * between is worth having, and it costs one generated id. The heading is
 * supplied by the caller because the text is the caller's, not a prop default.
 */
export function Card({
  className,
  labelledBy,
  children,
  ...props
}: HTMLAttributes<HTMLElement> & { labelledBy?: string }) {
  return (
    <section
      aria-labelledby={labelledBy}
      className={cn("rounded-lg border bg-background p-6", className)}
      {...props}
    >
      {children}
    </section>
  );
}

export function CardHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("mb-4 flex flex-col gap-1", className)} {...props} />;
}

export function CardTitle({
  className,
  id,
  ...props
}: HTMLAttributes<HTMLHeadingElement> & { id?: string }) {
  // `h2` by default: the page supplies `h1`, and a document that jumps from h1
  // to h3 breaks the heading hierarchy T170 checks.
  return <h2 id={id} className={cn("text-lg font-semibold", className)} {...props} />;
}

export function CardDescription({ className, ...props }: HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("text-sm text-muted-foreground", className)} {...props} />;
}

export function CardContent({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("grid gap-4", className)} {...props} />;
}

/**
 * A status word.
 *
 * Colour plus text, never colour alone (FR-059): every state here renders its
 * name, so the badge is redundant for a reader who cannot distinguish the hues.
 * `tone` carries meaning; `children` carries the word.
 *
 * **The text colours are the `*-text` tokens, not `text-success`/`text-destructive`.**
 * The latter were chosen as graphic colours and measure 3.65:1 on white — fine for
 * a fill, unreadable as 12px text. axe found this badge on the ask view in the
 * light scheme; the badge is small text, so it needs the 4.5:1 tokens.
 */
export type BadgeTone = "neutral" | "success" | "warning" | "danger" | "info";

const badgeTones: Record<BadgeTone, string> = {
  neutral: "border text-muted-foreground",
  success: "border-success-border/50 text-success-text",
  warning: "border-warning-border/50 text-warning-text",
  danger: "border-danger-border bg-danger-surface text-danger-text",
  info: "border text-foreground",
};

export function Badge({
  tone = "neutral",
  className,
  children,
  ...props
}: HTMLAttributes<HTMLSpanElement> & { tone?: BadgeTone; children: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium",
        badgeTones[tone],
        className,
      )}
      {...props}
    >
      {children}
    </span>
  );
}