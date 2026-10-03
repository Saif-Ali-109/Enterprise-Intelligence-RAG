"use client";

/**
 * Registering a documentation source (FR-057, FR-058, SC-010).
 *
 * **The form is not the authority; it is the operator's first line of defence.**
 * Every rule the backend enforces is enforced here too, because a form that
 * accepts a private address and returns `SSRF_BLOCKED` three round trips later
 * teaches the operator that the field does not matter. But a rule that is only
 * *checked* by the server is the one that counts: the client's checks exist to
 * save a request, never to be the reason a request was believed safe.
 *
 * **The product domain is optional and says so.** FR-013 forbids forcing a
 * classification, so the field is free text with suggestions, not a select with
 * a default. Leaving it blank sends `null` — "cross-product or not yet known" —
 * and the list renders that as an absence rather than as a guess.
 *
 * **Registration reports the job, it does not wait for it.** `start_crawl`
 * defaults to true because registering a source and leaving it un-crawled is
 * almost never what was meant; the returned job id is handed to
 * `crawl-status.tsx`, which polls it until it finishes. Blocking here would
 * look frozen for as long as the crawl takes.
 *
 * Accessibility, all of it load-bearing rather than decorative:
 * - every control has a `<label htmlFor>`; the error text is wired with
 *   `aria-describedby` so it is announced with the field rather than only
 *   appearing on the page (FR-058);
 * - `aria-invalid` plus a thickened border, never colour alone (FR-059);
 * - the result is announced in a `role="status"` live region, so a screen
 *   reader hears "registered, crawl started" without hunting for it;
 * - a refusal is `role="alert"`, carrying the code and the request id, because
 *   an error the operator cannot quote is an error they cannot report;
 * - every control is a native element, so tab order, Enter-to-submit, and
 *   Space-to-toggle come from the platform rather than from JavaScript (FR-057).
 */
import { useId, useState, type FormEvent } from "react";
import { ApiError, describeError, isApiError } from "@/lib/api-client";
import { useRegisterSource } from "@/hooks/useRegistry";
import { CrawlStatus } from "@/components/sources/crawl-status";
import { Button } from "@/components/ui/button";
import { Checkbox, Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

/**
 * Domains this project's sources use, as suggestions only.
 *
 * A `<datalist>`, not a `<select>`: the operator can type a domain nobody has
 * used yet, and nothing here decides on their behalf what their content is
 * (FR-013). The list comes from `data/source_manifest.json`; it is a convenience,
 * not an enumeration.
 */
const PRODUCT_DOMAINS = ["jira", "confluence", "jsm", "developer"];

/** The contract's bounds, repeated so the form never sends a request it knows is invalid. */
const BOUNDS = {
  maxPages: { min: 1, max: 500 },
  maxDepth: { min: 0, max: 5 },
  delaySeconds: { min: 0.1, max: 60 },
} as const;

interface FieldErrors {
  name?: string;
  startUrl?: string;
  maxPages?: string;
  maxDepth?: string;
  delaySeconds?: string;
}

/**
 * What the server refused, mapped onto a field where it belongs.
 *
 * `SSRF_BLOCKED` and `INVALID_URL` are about the URL and nowhere else, so they
 * are attached to the URL field instead of being announced as a form-wide error
 * the operator has to locate. Everything else — including an unexplained
 * failure — is left to the alert at the top of the form, where it is visible
 * even if the operator's attention is elsewhere.
 */
function fieldErrorFor(error: ApiError): FieldErrors {
  switch (error.code) {
    case "SSRF_BLOCKED":
      return {
        startUrl:
          error.message ||
          "The crawler refused that address. It is private, local, or not a public documentation site.",
      };
    case "INVALID_URL":
      return { startUrl: error.message || "That is not a usable URL." };
    case "VALIDATION_ERROR": {
      const detail = error.details;
      // The API reports which field failed when it knows; when it does not, the
      // message still names it, so nothing is silently dropped.
      const field = typeof detail.field === "string" ? detail.field : null;
      if (field === "start_url") {
        return { startUrl: error.message };
      }
      if (field === "name") {
        return { name: error.message };
      }
      if (field === "max_pages") {
        return { maxPages: error.message };
      }
      if (field === "max_depth") {
        return { maxDepth: error.message };
      }
      if (field === "delay_seconds") {
        return { delaySeconds: error.message };
      }
      return {};
    }
    default:
      return {};
  }
}

/** The client's own checks. Each one names the bound it came from. */
function validate(values: {
  name: string;
  startUrl: string;
  maxPages: number;
  maxDepth: number;
  delaySeconds: number;
}): FieldErrors {
  const errors: FieldErrors = {};

  if (values.name.trim() === "") {
    errors.name = "A name is required, so this source can be recognised in the list.";
  }
  if (values.startUrl.trim() === "") {
    errors.startUrl = "A start URL is required.";
  } else if (!/^https:\/\/[^\s]+$/.test(values.startUrl.trim())) {
    // HTTPS only, and not merely because it is safer: an `http://` documentation
    // URL is nearly always a typo, and the backend refuses it anyway.
    errors.startUrl = "Enter a full https:// URL, for example https://example.atlassian.com/docs/.";
  }
  if (
    !Number.isInteger(values.maxPages) ||
    values.maxPages < BOUNDS.maxPages.min ||
    values.maxPages > BOUNDS.maxPages.max
  ) {
    errors.maxPages = `Between ${BOUNDS.maxPages.min} and ${BOUNDS.maxPages.max} pages.`;
  }
  if (
    !Number.isInteger(values.maxDepth) ||
    values.maxDepth < BOUNDS.maxDepth.min ||
    values.maxDepth > BOUNDS.maxDepth.max
  ) {
    errors.maxDepth = `Between ${BOUNDS.maxDepth.min} and ${BOUNDS.maxDepth.max}.`;
  }
  if (
    !Number.isFinite(values.delaySeconds) ||
    values.delaySeconds < BOUNDS.delaySeconds.min ||
    values.delaySeconds > BOUNDS.delaySeconds.max
  ) {
    errors.delaySeconds = `Between ${BOUNDS.delaySeconds.min} and ${BOUNDS.delaySeconds.max} seconds.`;
  }

  return errors;
}

export interface SourceFormProps {
  /** Called with the id of a newly registered source, so a list can focus it. */
  onRegistered?: (sourceId: string) => void;
}

export function SourceForm({ onRegistered }: SourceFormProps) {
  const formId = useId();
  const register = useRegisterSource();

  const [name, setName] = useState("");
  const [startUrl, setStartUrl] = useState("");
  const [productDomain, setProductDomain] = useState("");
  const [maxPages, setMaxPages] = useState("40");
  const [maxDepth, setMaxDepth] = useState("2");
  const [delaySeconds, setDelaySeconds] = useState("1");
  const [startCrawl, setStartCrawl] = useState(true);
  const [errors, setErrors] = useState<FieldErrors>({});

  const submitError = register.error;
  const serverFieldErrors = isApiError(submitError) ? fieldErrorFor(submitError) : {};
  const fieldError = (field: keyof FieldErrors): string | undefined =>
    errors[field] ?? serverFieldErrors[field];

  function describedBy(field: keyof FieldErrors, hintId?: string): string | undefined {
    const parts = [fieldError(field) !== undefined ? `${formId}-${field}-error` : null, hintId ?? null];
    const rendered = parts.filter((part) => part !== null).join(" ");
    return rendered === "" ? undefined : rendered;
  }

  function onSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const parsed = {
      name: name.trim(),
      startUrl: startUrl.trim(),
      maxPages: Number(maxPages),
      maxDepth: Number(maxDepth),
      delaySeconds: Number(delaySeconds),
    };
    const found = validate(parsed);
    setErrors(found);
    if (Object.keys(found).length > 0) {
      // Nothing is submitted, and the first bad field takes focus so the reason
      // is where the operator is already looking.
      const first = document.getElementById(`${formId}-${Object.keys(found)[0] ?? "name"}`);
      if (first instanceof HTMLElement) {
        first.focus();
      }
      return;
    }

    register.mutate(
      {
        name: parsed.name,
        start_url: parsed.startUrl,
        // An empty box is an explicit "not specified", sent as null rather than
        // omitted: the difference is a source that says "cross-product" and one
        // whose classification was never asked about.
        product_domain: productDomain.trim() === "" ? null : productDomain.trim(),
        max_pages: parsed.maxPages,
        max_depth: parsed.maxDepth,
        delay_seconds: parsed.delaySeconds,
        start_crawl: startCrawl,
      },
      {
        onSuccess: (result) => {
          setName("");
          setStartUrl("");
          setProductDomain("");
          setErrors({});
          onRegistered?.(result.source.id);
        },
      },
    );
  }

  const announcement = register.isSuccess
    ? register.data.created
      ? `Registered ${register.data.source.name}.${
          register.data.crawlJob === null
            ? " No crawl was started."
            : ` Crawl ${register.data.crawlJob.status} as job ${register.data.crawlJob.id}.`
        }`
      : `${register.data.source.name} is already registered. No new crawl was started.`
    : null;

  return (
    <Card labelledBy={`${formId}-title`}>
      <CardHeader>
        <CardTitle id={`${formId}-title`}>Register a source</CardTitle>
        <CardDescription>
          A public documentation URL. The crawler reads that page&apos;s sitemap and its
          links, obeys the site&apos;s robots rules, and stores headings and units — never
          page text in this application&apos;s database.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} noValidate aria-busy={register.isPending} className="grid gap-5">
          {/* The outcome. `status` so it is announced politely; `alert` so a
              refusal interrupts. Both are needed and they are not interchangeable. */}
          <p role="status" aria-live="polite" className="sr-only">
            {announcement ?? ""}
          </p>

          {submitError !== null && submitError !== undefined ? (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm"
            >
              <p className="font-medium">
                {isApiError(submitError) ? submitError.code : "Registration failed"}
              </p>
              <p className="mt-1">{describeError(submitError)}</p>
              {isApiError(submitError) && Object.keys(serverFieldErrors).length === 0 ? (
                <p className="mt-1 text-xs text-muted-foreground">
                  Nothing was registered. Correct the source and try again.
                </p>
              ) : null}
            </div>
          ) : null}

          {announcement !== null ? (
            <p className="rounded-md border p-3 text-sm">{announcement}</p>
          ) : null}

          <div className="grid gap-2">
            <Label htmlFor={`${formId}-name`} required>
              Name
            </Label>
            <Input
              id={`${formId}-name`}
              name="name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              aria-invalid={fieldError("name") !== undefined}
              aria-describedby={describedBy("name")}
              maxLength={200}
              autoComplete="off"
            />
            <FieldError id={`${formId}-name-error`} message={fieldError("name")} />
          </div>

          <div className="grid gap-2">
            <Label htmlFor={`${formId}-startUrl`} required>
              Start URL
            </Label>
            <Input
              id={`${formId}-startUrl`}
              name="start_url"
              type="url"
              inputMode="url"
              value={startUrl}
              onChange={(event) => setStartUrl(event.target.value)}
              aria-invalid={fieldError("startUrl") !== undefined}
              aria-describedby={describedBy("startUrl", `${formId}-startUrl-hint`)}
              placeholder="https://support.atlassian.com/jira-software-cloud/docs/"
              autoComplete="off"
            />
            <p id={`${formId}-startUrl-hint`} className="text-xs text-muted-foreground">
              The crawl stays on this site and below this path. Private and local addresses
              are refused before any connection is opened.
            </p>
            <FieldError id={`${formId}-startUrl-error`} message={fieldError("startUrl")} />
          </div>

          <div className="grid gap-2">
            <Label htmlFor={`${formId}-productDomain`}>Product domain</Label>
            <Input
              id={`${formId}-productDomain`}
              name="product_domain"
              list={`${formId}-products`}
              value={productDomain}
              onChange={(event) => setProductDomain(event.target.value)}
              aria-describedby={`${formId}-productDomain-hint`}
              maxLength={100}
              autoComplete="off"
            />
            <datalist id={`${formId}-products`}>
              {PRODUCT_DOMAINS.map((domain) => (
                <option key={domain} value={domain} />
              ))}
            </datalist>
            <p id={`${formId}-productDomain-hint`} className="text-xs text-muted-foreground">
              Optional. Leave it blank for a cross-product or not-yet-classified source — no
              value is ever chosen for you.
            </p>
          </div>

          <fieldset className="grid gap-3 border-t pt-4">
            <legend className="text-sm font-medium">Crawl bounds</legend>
            <div className="grid gap-5 sm:grid-cols-3">
              <div className="grid gap-2">
                <Label htmlFor={`${formId}-maxPages`} required>
                  Maximum pages
                </Label>
                <Input
                  id={`${formId}-maxPages`}
                  name="max_pages"
                  type="number"
                  inputMode="numeric"
                  min={BOUNDS.maxPages.min}
                  max={BOUNDS.maxPages.max}
                  step={1}
                  value={maxPages}
                  onChange={(event) => setMaxPages(event.target.value)}
                  aria-invalid={fieldError("maxPages") !== undefined}
                  aria-describedby={describedBy("maxPages")}
                />
                <FieldError id={`${formId}-maxPages-error`} message={fieldError("maxPages")} />
              </div>

              <div className="grid gap-2">
                <Label htmlFor={`${formId}-maxDepth`} required>
                  Maximum depth
                </Label>
                <Input
                  id={`${formId}-maxDepth`}
                  name="max_depth"
                  type="number"
                  inputMode="numeric"
                  min={BOUNDS.maxDepth.min}
                  max={BOUNDS.maxDepth.max}
                  step={1}
                  value={maxDepth}
                  onChange={(event) => setMaxDepth(event.target.value)}
                  aria-invalid={fieldError("maxDepth") !== undefined}
                  aria-describedby={describedBy("maxDepth")}
                />
                <FieldError id={`${formId}-maxDepth-error`} message={fieldError("maxDepth")} />
              </div>

              <div className="grid gap-2">
                <Label htmlFor={`${formId}-delaySeconds`} required>
                  Delay between pages (s)
                </Label>
                <Input
                  id={`${formId}-delaySeconds`}
                  name="delay_seconds"
                  type="number"
                  inputMode="decimal"
                  min={BOUNDS.delaySeconds.min}
                  max={BOUNDS.delaySeconds.max}
                  step={0.1}
                  value={delaySeconds}
                  onChange={(event) => setDelaySeconds(event.target.value)}
                  aria-invalid={fieldError("delaySeconds") !== undefined}
                  aria-describedby={describedBy("delaySeconds", `${formId}-delaySeconds-hint`)}
                />
                <p id={`${formId}-delaySeconds-hint`} className="text-xs text-muted-foreground">
                  A floor: a site&apos;s own crawl-delay is used when it asks for longer.
                </p>
                <FieldError
                  id={`${formId}-delaySeconds-error`}
                  message={fieldError("delaySeconds")}
                />
              </div>
            </div>
          </fieldset>

          <Checkbox
            id={`${formId}-startCrawl`}
            name="start_crawl"
            checked={startCrawl}
            onChange={(event) => setStartCrawl(event.target.checked)}
            label="Start a crawl now"
            description="Off registers the source without crawling it, for a maintenance window."
          />

          <div className="flex items-center gap-3">
            <Button type="submit" disabled={register.isPending}>
              {register.isPending ? "Registering…" : "Register source"}
            </Button>
            {register.isPending ? (
              <span className="text-sm text-muted-foreground">Waiting for the service…</span>
            ) : null}
          </div>

          {/* The crawl this registration started, watched to its end. It lives
              here rather than in the list because this is the action that began
              it: the operator's eyes are here, and a panel that moved the moment
              they registered would be a panel they had to hunt for. */}
          {register.isSuccess && register.data.crawlJob !== null ? (
            <CrawlStatus jobId={register.data.crawlJob.id} />
          ) : null}
        </form>
      </CardContent>
    </Card>
  );
}

/**
 * A field's error message.
 *
 * Rendered as an element the `aria-describedby` above points at, so the reason
 * is announced with the field. `null` when there is nothing to say: an empty
 * `<p>` with an `id` is announced as an empty string in some screen readers,
 * which reads as a message that says nothing.
 */
function FieldError({ id, message }: { id: string; message: string | undefined }) {
  if (message === undefined) {
    return null;
  }
  return (
    <p id={id} className="text-xs font-medium text-destructive">
      {message}
    </p>
  );
}