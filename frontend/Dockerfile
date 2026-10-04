# Frontend image. Node 20.9+ is the floor recorded in research.md R-011; the
# App Router stream proxy behaves differently below it.
#
# Multi-stage so the shipped image contains no source maps, no dev server, and
# no dependency tree beyond what the production build needs. No secret is present
# in any layer: the backend URL is supplied at RUNTIME, not baked in.

# --- stage 1: install dependencies --------------------------------------------
FROM node:20.9-alpine AS deps

WORKDIR /app
COPY package.json package-lock.json* ./
# `npm ci` when a lockfile exists (reproducible), `npm install` otherwise.
# The lockfile is committed precisely so this branch is normally the first one.
RUN --mount=type=cache,target=/root/.npm \
    if [ -f package-lock.json ]; then npm ci; else npm install; fi

# --- stage 2: build -----------------------------------------------------------
FROM node:20.9-alpine AS builder

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

ENV NEXT_TELEMETRY_DISABLED=1
# The backend URL is NOT provided here. Anything passed as an env var at build
# time is inlined into the client bundle; a credential there would be public.
RUN npm run build

# --- stage 3: runtime ---------------------------------------------------------
FROM node:20.9-alpine AS runtime

WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

RUN addgroup --system --gid 10001 nodejs \
    && adduser --system --uid 10001 nextjs

# `output: standalone` emits a self-contained server with only the modules it
# actually reaches, which is what keeps the runtime image small.
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

USER nextjs

EXPOSE 3000

# `compress: false` in next.config.ts is what keeps the SSE proxy from buffering
# the event stream. Do not "fix" that by enabling gzip here.
CMD ["node", "server.js"]
