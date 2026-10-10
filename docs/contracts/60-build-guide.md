# 60 · Builder guide (how to build & test on this machine)

**Host:** Windows 11, Docker Desktop (WSL2) with **8 GB RAM / 6 CPUs**, Git Bash + PowerShell. **D: has only ~7 GB free** — keep heavy artefacts (venvs, node_modules, caches) inside Docker volumes, not on D:. No `uv`, `pnpm`, `make` on the host — run toolchains in containers. Host has Node 25 and Python 3.14 but do not install global packages on the host.

## Running toolchains in containers (Git Bash)
Always prefix with `MSYS_NO_PATHCONV=1` so paths are not mangled, and use `D:/primark`:
```bash
# Python (uv) — venv lives in the container, uv cache in a named volume
MSYS_NO_PATHCONV=1 docker run --rm --memory 1500m \
  -v D:/primark:/work -v keelson_uvcache:/root/.cache/uv \
  -e UV_PROJECT_ENVIRONMENT=/opt/venv -e UV_LINK_MODE=copy \
  -w /work/services/<svc> ghcr.io/astral-sh/uv:python3.14-trixie-slim \
  sh -c "uv sync --frozen 2>/dev/null || uv lock && uv sync; uv run pytest -q"

# Node (pnpm via corepack inside node:24-slim); node_modules in a named volume, never on D:
MSYS_NO_PATHCONV=1 docker run --rm --memory 2500m \
  -v D:/primark:/work -v keelson_pnpm_store:/pnpm/store \
  -v keelson_nm_<app>:/work/apps/<app>/node_modules \
  -e PNPM_HOME=/pnpm -e NODE_OPTIONS=--max-old-space-size=2048 \
  -w /work/apps/<app> node:24-slim \
  sh -c "corepack enable && corepack prepare pnpm@10.34.6 --activate && pnpm install && pnpm build"
```
(If `ghcr.io/astral-sh/uv:python3.14-trixie-slim` is unavailable, use `python:3.14-slim-trixie` + `COPY/pip install uv`.)

## Memory etiquette (shared 8 GB)
- Several builders run concurrently. Cap every container (`--memory`). Never leave long-running containers up after you finish; `docker compose down` what you started (do **not** remove volumes you didn't create, and never `docker system prune`).
- Use `docker compose -p keelson` (project name in compose.yaml) when you need shared services; start only the services you need (`docker compose up -d pg-merxa merxa-api`).
- Don't run two `next build`s at once if you can avoid it.

## Lockfiles & reproducibility
Since M8 the lockfiles are the authority for exact versions (Renovate keeps them current; `docs/supply-chain.md`): `uv.lock` per uv project (`uv sync --frozen`), `pnpm-lock.yaml` (workspace) and `apps/merxa-web/pnpm-lock.yaml` (`pnpm install --frozen-lockfile`), `e2e/playwright/package-lock.json`, `evals/requirements.txt` (exact pins). Direct dependencies stay exact (`==` / no ranges). Baseline at M7: google-adk 2.11.0, litellm 1.104.0, fastmcp 4.0.11, mcp 2.3.0, ortools 9.15.6755, ag-ui-protocol 1.0.0, pydantic 2.13.5; TypeScript 5.9.3, Next 16.3.8, React 19.3.0, @ag-ui/core 1.0.2, zod 4.6.5, motion 13.5.1.

Known caps — encoded in `renovate.json` (`allowedVersions`) **and** enforced on the lockfiles / image tags by `scripts/supply-chain/check_pins.py` (P7):

| Cap | Why |
|---|---|
| TypeScript < 6.1 | typescript-eslint |
| motion 13.x (< 14), @tanstack/react-table 8.21.x (< 9) | API breaks in the next major |
| maplibre-gl ≥ 6.4.1 | M3 map fixes |
| engine fastapi < 0.142, opentelemetry-api ≤ 1.42.1 (while google-adk 2.11.0) | google-adk 2.11 caps opentelemetry-api at 1.42.1 |
| litellm never 1.82.7 / 1.82.8 (package and proxy image) | compromised releases (plan §14) |
| python < 3.15, node < 25, postgres < 19, valkey < 9, Keycloak 26.x, Caddy 2.x | requires-python; corepack; Cloud SQL POSTGRES_18; Memorystore VALKEY_8_0; realm files; Caddyfile v2 |

google-adk / google-genai / litellm / mcp / fastmcp / AG-UI upgrades are gated (PR labels `spike-verified`, `replays-rerecorded`; `scripts/supply-chain/upgrade_gate.py`). New releases cool off 3 days (7 for that stack) before Renovate or pnpm (`minimumReleaseAge`) resolves them.

**Images:** every image reference is `name:<exact tag>@sha256:<digest>` (`scripts/supply-chain/pin-image.sh`); a new Dockerfile or `test` stage must be registered in `infra/supply-chain/images.json` (CI matrices, signing and the deploy map come from it). CI runs the same container commands as local builds (`scripts/supply-chain/ci_run_suite.py`).

## Dockerfiles
- Python services: multi-stage; builder stage installs with `uv sync --frozen --no-dev` into `/opt/venv`; runtime `python:3.14.8-slim-trixie@sha256:…` (pinned), non-root user `app` (uid 10001), `ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1`, `EXPOSE $PORT`. Build context for engine & optimizer is the **repo root** (they need `packages/contracts-py`); merxa-api context is its own folder.
- Next apps: build context repo root, `output: 'standalone'`, runtime `node:24.21.0-slim@sha256:…` (pinned) non-root, `CMD ["node","server.js"]`.

## Definition of done for a builder
1. Code + Dockerfile + README for your component; follows `docs/contracts/*` exactly (or lists deviations explicitly).
2. Tests pass inside a container; the image builds.
3. You report: files, how to run, test results, contract deviations, open issues, integration notes.
