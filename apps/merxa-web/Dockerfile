# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# merxa-web: Merxa Retail Suite UI (Next.js 16 standalone, port 3001).
# Build context: repository root (compose.yaml), but only apps/merxa-web is copied:
# Merxa is a separate product and never sees Keelson sources.

ARG NODE_IMAGE=node:24.21.0-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20

FROM ${NODE_IMAGE} AS deps
WORKDIR /app
ENV PNPM_HOME=/pnpm CI=true NEXT_TELEMETRY_DISABLED=1
RUN corepack enable && corepack prepare pnpm@10.34.6 --activate
COPY apps/merxa-web/package.json apps/merxa-web/pnpm-lock.yaml apps/merxa-web/pnpm-workspace.yaml ./
RUN --mount=type=cache,id=merxa-web-pnpm,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store && pnpm install --frozen-lockfile

FROM deps AS build
# Only the sources the build and tests need (BuildKit then transfers only these): local
# caches in the folder (e.g. a stray .pnpm-store from a bind-mounted pnpm run) stay out.
COPY apps/merxa-web/next.config.ts apps/merxa-web/tsconfig.json apps/merxa-web/eslint.config.mjs \
     apps/merxa-web/vitest.config.ts ./
COPY apps/merxa-web/public ./public
COPY apps/merxa-web/test ./test
COPY apps/merxa-web/src ./src
ENV NODE_ENV=production NODE_OPTIONS=--max-old-space-size=2048
RUN pnpm build

# Test stage (not part of the runtime image): docker build --target test …
FROM build AS test
ENV NODE_ENV=test
RUN pnpm typecheck && pnpm lint && pnpm test

FROM ${NODE_IMAGE} AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3001 HOSTNAME=0.0.0.0 \
    MERXA_API_INTERNAL_URL=http://merxa-api:8100
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
COPY --from=build --chown=app:app /app/public ./public
USER app
EXPOSE 3001
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:'+(process.env.PORT||3001)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
CMD ["node", "server.js"]
