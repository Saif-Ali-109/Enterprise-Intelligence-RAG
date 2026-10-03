"use client";

/**
 * The one client-side provider this application has.
 *
 * **The `QueryClient` is created once per mount, never per render.** A client
 * built during render is a new cache on every re-render, which discards every
 * in-flight request and re-fetches the world; `useState` with an initialiser is
 * the fix, and it is here rather than inside each hook so that no component can
 * forget it.
 *
 * **`retry` is 2 for reads and 0 for writes.** Re-fetching a list after a network
 * blip is right. Re-sending a registration or a deletion is not: the API is
 * idempotent for registration and for deletion, but an operator watching a
 * button press three times is a worse outcome than an error they can retry
 * themselves — and a retried *crawl* is a second crawl.
 *
 * **No devtools overlay.** The query cache is not a debugging story this
 * application wants; the inspection view (T131) is the deliberate one, and it
 * shows pipeline state rather than React internals.
 */
import { useState, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Reads are safe to repeat; the API answers a repeated GET with the
            // same registry, so a transient failure is worth two more tries.
            retry: 2,
            // Exponential backoff capped at 5s: long enough not to hammer a
            // service that is genuinely down, short enough that a blip is
            // invisible to the operator.
            retryDelay: (attempt) => Math.min(1_000 * 2 ** attempt, 5_000),
            refetchOnWindowFocus: true,
          },
          mutations: {
            // A write is an action a person took. Repeating it behind their back
            // is not resilience, it is a second action they did not authorise.
            retry: 0,
          },
        },
      }),
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}