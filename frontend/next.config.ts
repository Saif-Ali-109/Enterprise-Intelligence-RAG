import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The standalone server the Dockerfile runs. Without this the image contains a
  // `node_modules` tree and the full Next runtime instead of a traced subset,
  // which is the difference between a 200 MB image and a 120 MB one — and it is
  // stated here rather than left implicit, because the Dockerfile already assumes
  // it and a missing key fails at image *run* time, not at build time.
  output: "standalone",

  // Compressors buffer a streamed response, which collapses the pipeline stage
  // events into a single clump at the end. That is indistinguishable from a
  // broken backend, so compression is disabled at the application (R-011).
  // Anything in front of this must be configured to leave SSE alone.
  compress: false,

  // Turbopack infers its workspace root from the nearest lockfile above the
  // project. On a machine that has an unrelated `package.json` in `$HOME`, that
  // inference walks *past* this directory and every build prints a warning
  // about a lockfile it ignored. Stating the root here makes the build's
  // dependency tree this project's, deterministically, with no absolute path in
  // the file.
  turbopack: {
    root: __dirname,
  },

  reactStrictMode: true,

  // Route handlers that stream are Node-runtime by default. This is stated
  // rather than assumed: the SSE proxy at app/api/chat/stream/route.ts must run
  // on Node, because the Edge runtime buffers rather than relaying.
  experimental: {
    proxyTimeout: 120_000,
  },

  // Next 16 no longer runs ESLint as part of `next build`, so there is no
  // `eslint.ignoreDuringBuilds` key to set: linting is a separate `npm run
  // lint` with its own exit code, and a build failure is never hidden behind a
  // lint nit.

  // No secret is ever exposed to the browser. The browser talks only to this
  // application's own same-origin `/api/v1/*` route handlers, which hold the
  // backend base URL server-side: a `NEXT_PUBLIC_*` value is inlined at build
  // time, which would both publish the backend address and put a compiled-in
  // string in the client bundle.
  env: {},
};

export default nextConfig;
