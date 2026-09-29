import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Compressors buffer a streamed response, which collapses the pipeline stage
  // events into a single clump at the end. That is indistinguishable from a
  // broken backend, so compression is disabled at the application (R-011).
  // Anything in front of this must be configured to leave SSE alone.
  compress: false,

  reactStrictMode: true,

  // Route handlers that stream are Node-runtime by default. This is stated
  // rather than assumed: the SSE proxy at app/api/chat/stream/route.ts must run
  // on Node, because the Edge runtime buffers rather than relaying.
  experimental: {
    proxyTimeout: 120_000,
  },

  eslint: {
    // Linting is a separate step with its own output. Failing the production
    // build on a lint nit hides real build failures.
    ignoreDuringBuilds: false,
  },

  // No secret is ever exposed to the browser. The browser talks to its own
  // same-origin route handlers, which hold the backend base URL server-side.
  env: {},
};

export default nextConfig;
