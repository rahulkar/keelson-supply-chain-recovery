# syntax=docker/dockerfile:1.7
# keelson-web: the Keelson web app (Next.js 16 standalone, port 3000).
#
# Build context: the repository root (compose.yaml). The pnpm workspace (apps/keelson-web +
# packages/{tokens-keelson,ui-keelson,viz,contracts-ts}) is installed from the root lockfile and
# @keelson/web is built; the @keelson/* packages are compiled from source by Next
# (transpilePackages). `Dockerfile.dockerignore` next to this file limits the context to exactly
# those paths.
#
#   docker build -f apps/keelson-web/Dockerfile -t keelson-web .
#   docker build -f apps/keelson-web/Dockerfile --target test .     # typecheck + lint + unit tests

ARG NODE_IMAGE=node:24-slim

FROM ${NODE_IMAGE} AS base
ENV PNPM_HOME=/pnpm CI=true NEXT_TELEMETRY_DISABLED=1
RUN corepack enable && corepack prepare pnpm@10.34.6 --activate
WORKDIR /repo

# ── dependencies: the store is filled from the lockfile alone (cached until it changes) ──
FROM base AS deps
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,id=keelson-web-pnpm,target=/pnpm/store \
    pnpm fetch --frozen-lockfile --store-dir /pnpm/store
COPY apps/keelson-web/package.json apps/keelson-web/
COPY packages/tokens-keelson/package.json packages/tokens-keelson/
COPY packages/ui-keelson/package.json packages/ui-keelson/
COPY packages/viz/package.json packages/viz/
COPY packages/contracts-ts/package.json packages/contracts-ts/
RUN --mount=type=cache,id=keelson-web-pnpm,target=/pnpm/store \
    pnpm install --frozen-lockfile --offline --store-dir /pnpm/store

# ── build ──
FROM deps AS build
COPY packages/tokens-keelson/ packages/tokens-keelson/
COPY packages/ui-keelson/ packages/ui-keelson/
COPY packages/viz/ packages/viz/
COPY packages/contracts-ts/ packages/contracts-ts/
COPY apps/keelson-web/ apps/keelson-web/
ENV NODE_ENV=production NODE_OPTIONS=--max-old-space-size=2048
# Third-party base-map origins baked into the CSP (next.config.ts; "none" = strict build,
# docs/contracts/127-m8-basemap.md). A build ARG, so it is seen by `next build` only.
ARG KEELSON_CSP_MAP_ORIGINS=https://tiles.openfreemap.org
RUN pnpm --filter @keelson/web build

# ── tests (not part of the runtime image): docker build --target test … ──
FROM build AS test
ENV NODE_ENV=test
RUN pnpm --filter @keelson/web typecheck && pnpm --filter @keelson/web lint && pnpm --filter @keelson/web test

# ── runtime: standalone server only, non-root ──
FROM ${NODE_IMAGE} AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0 \
    ENGINE_API_INTERNAL_URL=http://engine-api:8000
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
# Monorepo standalone layout: /app/node_modules (traced deps) + /app/apps/keelson-web/server.js
COPY --from=build --chown=app:app /repo/apps/keelson-web/.next/standalone ./
COPY --from=build --chown=app:app /repo/apps/keelson-web/.next/static ./apps/keelson-web/.next/static
COPY --from=build --chown=app:app /repo/apps/keelson-web/public ./apps/keelson-web/public
WORKDIR /app/apps/keelson-web
USER app
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:'+(process.env.PORT||3000)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
CMD ["node", "server.js"]
