# Software supply chain: pins, Renovate, CI, signed releases

How Keelson keeps what it builds and runs reproducible and verifiable. Spec and design decisions:
`docs/contracts/129-m8-supply-chain.md`. Plan §5 ("pinned by digest", LiteLLM "cosign-verified"),
§14 (monthly upgrades, LiteLLM 1.82.7/1.82.8 and `litellm_init.pth` blocked) and §21 (ADK/LiteLLM
churn rules).

| Control | Where | Checked by |
|---|---|---|
| Every image `name:<exact tag>@sha256:<digest>`, every action `owner/repo@<sha> # vX.Y.Z` | Dockerfiles, `compose.yaml`, `infra/cloudrun/versions.env`, `infra/supply-chain/signing.env`, scripts, workflows | `scripts/supply-chain/check_pins.py` (CI `meta`) |
| Libraries pinned exactly with hashed lockfiles | `uv.lock` x5, `pnpm-lock.yaml`, `apps/merxa-web/pnpm-lock.yaml`, `e2e/playwright/package-lock.json`, `evals/requirements.txt` | `uv sync --frozen`, `pnpm install --frozen-lockfile` in every build |
| Version caps (`60-build-guide.md`) | `renovate.json` `allowedVersions` | Renovate + `check_pins.py` P7 on the lockfiles |
| 3-day cooling-off for new releases (7 days for ADK/LiteLLM/MCP) | `renovate.json`, `pnpm-workspace.yaml` + `apps/merxa-web/pnpm-workspace.yaml` (`minimumReleaseAge: 4320`) | `check_pins.py` P8 keeps them equal |
| ADK / LiteLLM / MCP / AG-UI upgrades need a spike, replay changes a re-record | PR labels `spike-verified`, `replays-rerecorded` | `scripts/supply-chain/upgrade_gate.py` (CI `upgrade-gate`) |
| Every Dockerfile and test stage registered once | `infra/supply-chain/images.json` | `check_pins.py` P9; CI matrices come from it |
| Same test commands locally and in CI | `scripts/supply-chain/ci_run_suite.py` | CI `python` / `web` jobs |
| Built images: non-root, no `litellm_init.pth` / code-running `.pth`, no secrets, labels; no fixable critical CVE | `scripts/supply-chain/check-image.sh`, Grype + `.grype.yaml` | CI `images`, `release.yml` |
| Release images signed keylessly, SBOM + SLSA provenance attested, digests in a signed `keelson-release.env` | `.github/workflows/release.yml`, `.github/actions/sign-attest` | `scripts/supply-chain/verify-images.sh`, `deploy.sh`, `compose-release.sh` |
| Cloud Run deploys only verified digests | `scripts/cloudrun/deploy.sh --release` | before any `gcloud` call; exit 2 otherwise |
| LiteLLM proxy image verified with BerriAI's key | `infra/supply-chain/keys/litellm-cosign.pub` | CI `images`, `release.yml`, `verify-images.sh --compose` |

Everything here runs without an LLM and without cloud credentials; the tests need Docker only.

## 1. Pinning images

Always the form `name:<exact tag>@sha256:<index digest>`: the tag says what it is (and lets Renovate
propose updates), the digest says exactly which bytes. "Exact" means the most specific tag the
publisher has: `python:3.14.8-slim-trixie`, `caddy:2.11.7-alpine`, `postgres:18.6-trixie`, never
`3.14-slim`, `2-alpine`, `latest`, `stable`. PostGIS only publishes `18-3.6`-style tags (allowed; the
digest pins it).

```bash
scripts/supply-chain/pin-image.sh caddy:2.11.7-alpine          # -> caddy:2.11.7-alpine@sha256:...
scripts/supply-chain/pin-image.sh --verify "$(sed -n 's/^LITELLM_IMAGE=//p' infra/cloudrun/versions.env)"
```

`pin-image.sh` uses `docker buildx imagetools inspect` (a registry lookup, nothing is pulled) and
returns the multi-platform index digest. The same pin may appear in several files (compose, overlay
`ARG` default, `versions.env`, scripts); P8 requires them to be identical, so change them together.

Exceptions: images built from this repository (`keelson-*`, `scratch`) and the overlay argument
`ARG ENGINE_IMAGE=keelson-engine-api:latest` (deploy.sh always passes the engine image it built or
verified).

## 2. `check_pins.py`

```bash
python3 scripts/supply-chain/check_pins.py              # whole repository; exit status = number of findings
python3 scripts/supply-chain/check_pins.py --rule P1,P9 # only some rules
```

Stdlib only (Git Bash, the GitHub runner or `python:3.14.8-slim-trixie`). One JSON line per finding
`{"rule","file","line","message"}` on stdout, a per-rule count on stderr.

| Rule | Checks |
|---|---|
| P1 | Dockerfiles: `# syntax=`, `FROM`, `COPY/RUN --from=<image>` and the `ARG` defaults they use are pinned |
| P2 | `compose*.yaml`: every `image:` is pinned |
| P3 | `infra/cloudrun/versions.env`, `infra/supply-chain/signing.env`: every `*_IMAGE` is pinned |
| P4 | `scripts/`, `infra/`, `e2e/` shell + PowerShell and `.github/`: image assignments and literals of known repositories are pinned; so are the `image`s of the toolchain `suites` in `infra/supply-chain/images.json` |
| P5 | workflows/actions: `uses:` is `./…` or `owner/repo[/path]@<40-hex sha> # vX.Y.Z`; no `docker://` |
| P6 | no floating (`latest`, `stable`, `edge`, …) or inexact (`2-alpine`, `3.14-slim`) tags, even with a digest |
| P7 | caps in the lockfiles (every locked version of a package) and image tags: motion <14, @tanstack/react-table <9, TypeScript <6.1, maplibre-gl ≥6.4.1, engine fastapi <0.142 and opentelemetry-api ≤1.42.1 while google-adk 2.11.0, LiteLLM never 1.82.7/1.82.8 (package and image), python <3.15, node <25, postgres <19, valkey <9, Keycloak <27, Caddy <3 |
| P8 | one pin per image variant (repository + tag without its version: `uv:0.12.23` and `uv:0.12.23-python3.14-trixie-slim` are two variants); pnpm (package.json x2, corepack lines, `scripts/native/versions.psd1`), uv, LiteLLM (package = proxy image = native), Keycloak, keycloak-config-cli and Caddy (= `versions.psd1`); overlay `ARG` defaults = `versions.env`; pydantic / mcp / fastmcp equal across the uv projects; AG-UI major.minor; Playwright npm = image; Valkey major = Memorystore; pnpm `minimumReleaseAge` = Renovate's |
| P9 | `infra/supply-chain/images.json`: valid; every Dockerfile, compose `build:` and Dockerfile `test` stage registered; every registered path/target exists; deploy.sh image variables = the registry's `deploy_var`s; deploy.sh / compose-release.sh release maps match the registry |

Typical messages and what to do:

* `P1 … FROM node:24-slim is not pinned (name:<exact tag>@sha256:<digest>)` → pin it with `pin-image.sh`.
* `P9 … Dockerfile services/x/Dockerfile is not registered in infra/supply-chain/images.json` → add an
  entry (section 3).
* `P9 … stage 'test' of services/x/Dockerfile has no suite` → add a `suites` entry for it.
* `P8 … python is pinned to 2 different tag/digest pairs: …` → one of the files was not updated.

## 3. `infra/supply-chain/images.json` (adding an image)

The single registry of first-party images and CI suites (schema `keelson.images/1`, validated by
`scripts/supply-chain/registry.py`). CI matrices (`matrix.py`), local suite runs (`ci_run_suite.py`),
release signing (`release.yml`), the release file (`release_manifest.py`) and the deploy map are all
derived from it. **A new Dockerfile is not done until it is registered here** (P9 fails otherwise):

```jsonc
{
  "name": "fernhill-sidecar",                 // [a-z0-9-]; also the ghcr repository name and RELEASE_IMAGE_FERNHILL_SIDECAR
  "dockerfile": "services/adapter-sidecar/Dockerfile",
  "context": ".",
  "target": "runtime",                        // null = last stage
  "release": true,                            // built, scanned, signed and listed in keelson-release.env
  "deploy_var": "IMAGE_FERNHILL_SIDECAR",     // only if scripts/cloudrun/deploy.sh deploys it (+ its release map)
  "suites": [
    {"id": "fernhill-sidecar", "target": "test", "memory": "1g"}
    // + "db": "pg-core", "valkey": true, "env": {...}, "cmd": [...], "deselect": [{"nodeid", "reason"}]
  ]
}
```

Optional keys: `overlay_of` + `overlay_arg` (a Cloud Run overlay built `FROM` another signed image),
`domain_overlay` (bakes the public domain; built only when `KEELSON_PUBLIC_DOMAIN` is set),
`verify_upstream {versions_env_key, pubkey}` (verify a third-party base with its publisher key first).
Toolchain suites without a Dockerfile (contracts-py, evals, supply-chain) live in the top-level
`suites` list with a pinned `image`, a `workdir` and a `cmd`.

A suite with `db: pg-core` gets a throwaway PostGIS + pgvector database reachable as `e1-test-pg`
(`postgres` / `test`), `valkey: true` a Valkey as `e1-test-valkey` (password `test`): the names the
service READMEs already use. `deselect` lists the known environment failures (120 §5) with a reason.

## 4. Running CI locally

```bash
python3 scripts/supply-chain/ci_run_suite.py --list
python3 scripts/supply-chain/ci_run_suite.py optimizer                 # build the test target, run it
python3 scripts/supply-chain/ci_run_suite.py engine-platform --dry-run # print the docker commands only
python3 scripts/supply-chain/ci_run_suite.py signal-hub --no-build --image keelson-ci/signal-hub:test
scripts/supply-chain/tests/run.sh [--with-renovate] [--with-docker]    # the supply-chain tooling itself
scripts/cloudrun/tests/run.sh                                          # Cloud Run deploy tooling
```

`ci_run_suite.py` names everything `keelson_ci_<suite>_<random>`, caps every container's memory and
removes it all afterwards (`--keep` to debug). `scripts/supply-chain/tests/run.sh` runs `check_pins`,
shellcheck v0.11.0 (severity warning) on every script, the pytest suites (`scripts/supply-chain/tests`,
`scripts/cloudrun/tests`, `scripts/tests/test_init_secrets.py`), actionlint and zizmor; `--with-renovate`
adds `renovate-config-validator --strict` and the extraction test (`renovate --platform=local
--dry-run=extract`, ~1.9 GB image); `--with-docker` adds the offline signing e2e (section 8).

## 5. CI (`.github/workflows/ci.yml`)

Triggers: pull requests (incl. label changes, for the gate), pushes to `main`, merge queue, nightly
02:23 UTC, manual. Top-level `permissions: {}`; each job gets only what it needs; checkouts never
persist credentials; no `pull_request_target`; no `${{ … }}` inside `run:` scripts (values go through
`env:`). No secrets are used.

| Job | What |
|---|---|
| `setup` | matrices from `images.json`; whether the docker-level tests apply (PRs touching `scripts/`, `infra/`, `.github/`, `compose.yaml`) |
| `meta` | `check_pins.py`, actionlint, zizmor (offline audits, `.github/zizmor.yml`), shellcheck, `renovate-config-validator --strict`, Renovate extraction |
| `upgrade-gate` | PRs: `upgrade_gate.py --base <base sha>` (section 6) |
| `python (<suite>)` | each run suite: build pg-core and the test target with the GHA layer cache, then `ci_run_suite.py <suite> --no-build`; includes `contracts-py`, `evals` and `supply-chain` |
| `web (<suite>)` | build `keelson-web` `test` / `workspace-test` and `merxa-web` `test` (building runs typecheck, lint, vitest; `workspace-test` also `gen-zod --check` and `gen-tokens --check`) |
| `image (<name>)` | every release image: upstream key check (LiteLLM), build the release target (single manifest), `check-image.sh --require-labels`, Grype (fails on a critical CVE with a fix), SARIF to code scanning |
| `docker-tests` | db-init against Postgres 18, `parity-check.sh --selftest`, Caddy routes, the signing e2e |

Suggested branch ruleset for `main`: required checks (by their check names) `meta (pins, lint, renovate)`,
`upgrade-gate`, every `python (<suite>)`, `web (<suite>)` and `image (<name>)`; protect `v*` tags. A job
skipped by its `if:` (upgrade-gate outside pull requests) counts as passed.

## 6. Renovate (`renovate.json`) and the upgrade gate

Uses the Mend Renovate GitHub App (free for public repositories; no token in the repository).
Weekly (Monday 00:00-05:59 Asia/Kolkata), at most 8 open / 4 per hour, dependency dashboard on,
OSV + GitHub vulnerability alerts (those bypass the schedule). Majors wait for approval on the
dashboard. Monthly: container digest refresh (grouped, automerged), lock-file maintenance, CI tool
images. GitHub Actions are pinned to SHAs with the version as a comment and automerge on minor/patch.

Managers: npm (pnpm workspace, merxa-web, e2e/playwright), pep621 (the five uv projects),
pip_requirements (evals), dockerfile, docker-compose, github-actions and regex managers for
`versions.env` / `signing.env`, the corepack pnpm lines, image literals in scripts / workflows /
`images.json`, and the pnpm + LiteLLM versions of `scripts/native/versions.psd1` (its hash-pinned
archives stay manual). `scripts/supply-chain/tests/renovate_extract.sh` proves every pinned image is
extracted exactly once and nothing is extracted from test fixtures.

Caps (`allowedVersions`): the P7 list above. Gated groups (dashboard approval, `needs-spike` label,
7-day minimum age, never automerged): `adk stack` (google-adk, google-genai), `litellm` (package +
proxy image, monthly, 1.82.7/1.82.8 excluded), `mcp` (mcp, fastmcp), `ag-ui` (Python + TS). ortools
(golden LP fixtures), httpx and the Keycloak pair (Keycloak stays 26.x) also need approval. Updates
that touch a tool also pinned in `scripts/native/versions.psd1` (uv, Keycloak, keycloak-config-cli,
Caddy) carry a PR note: update its Version/Url/Sha256 in the same PR, P8 fails until they match.

`upgrade_gate.py` diffs every lockfile, `models.lock`, `infra/litellm/config.yaml` and the LiteLLM
image pins between the PR base and head, directly and transitively:

| Change | Label required |
|---|---|
| google-adk, google-genai, litellm (package or image), mcp, fastmcp, ag-ui-protocol, @ag-ui/core, any engine `opentelemetry-*` | `spike-verified` |
| `models.lock`, `infra/litellm/config.yaml`, google-adk, google-genai, litellm | `replays-rerecorded` |

The job summary carries the checklist: S1 `pytest -m live tests/runtime/test_live_s1.py` against a
live LiteLLM (needs Gemini credit), S2 connector/MCP suites, S3 worker/replay/resume/crash suites +
a manual `kill -9` check, the offline e2e scripts with `LLM_REPLAY_MODE=replay`. While credits are
depleted S1 cannot run, so these upgrades stay frozen: intended.

## 7. Releases (`.github/workflows/release.yml`)

On every push to `main` and every `vX.Y.Z` tag of this repository (forks build nothing) **once releases are
enabled**: they are opt-in, so set the repository variable `RELEASE_ENABLED` to `true` (Settings → Secrets
and variables → Actions → Variables). Until then the workflow's jobs are skipped and nothing is built,
pushed to GHCR or signed (the CI workflow is unaffected):

1. `build-sign` (per release image): clean build (no shared layer cache) for `linux/amd64` as a
   single manifest, push to `ghcr.io/rahulkar/keelson/<name>` by digest, `check-image.sh`, Syft SPDX
   SBOM, Grype gate, `cosign sign` keyless with annotations `git_sha`, `image` (and `domain`),
   `cosign attest --type spdxjson`, SLSA v1 provenance (`actions/attest-build-provenance`, pushed to
   the registry). LiteLLM's base is verified with BerriAI's key first.
2. `overlays` (only when the repository variable `KEELSON_PUBLIC_DOMAIN` is set): `engine-cloudrun`
   (`FROM` the signed engine digest) and `kc-config-cloudrun`, signed with `domain=<domain>`.
3. `manifest`: `release_manifest.py` writes `keelson-release.env`, `cosign sign-blob` writes
   `keelson-release.env.sigstore.json`, then `verify-images.sh --release` verifies it end to end; both
   files are a 90-day workflow artifact (and, on tags, GitHub Release assets via `publish`).

`keelson-release.env` (`RELEASE_SCHEMA=keelson.release/1`): sorted `KEY=value` lines, no quotes:

```
RELEASE_SCHEMA=keelson.release/1
RELEASE_REPOSITORY=rahulkar/keelson-supply-chain-recovery
RELEASE_GIT_SHA=<40 hex>
RELEASE_REF=refs/heads/main            # or refs/tags/vX.Y.Z
RELEASE_RUN_ID=<run id>
RELEASE_CREATED_AT=2026-10-10T08:00:00Z
RELEASE_DOMAIN=demo.example.com        # empty without domain overlays
RELEASE_IMAGE_DB_INIT=ghcr.io/rahulkar/keelson/db-init@sha256:...
RELEASE_IMAGE_ENGINE=ghcr.io/rahulkar/keelson/engine@sha256:...
...
```

Signing identity (`infra/supply-chain/signing.env`): issuer `https://token.actions.githubusercontent.com`,
certificate identity `…/rahulkar/keelson-supply-chain-recovery/.github/workflows/release.yml@refs/heads/main`
or `@refs/tags/vX.Y.Z`, workflow repository and trigger `push`. A signature made by any other
workflow, branch, pull request, fork or trigger does not verify.

## 8. Verifying

```bash
scripts/supply-chain/verify-images.sh --release keelson-release.env   # the file, then every image
scripts/supply-chain/verify-images.sh -a git_sha=<sha> ghcr.io/rahulkar/keelson/engine@sha256:...
scripts/supply-chain/verify-images.sh --compose                       # third-party compose pins + LiteLLM key
scripts/supply-chain/check-image.sh keelson-engine-api:latest         # static image checks
```

Per image: signature by the release identity carrying the expected annotations, SPDX SBOM attestation,
SLSA v1 provenance (`cosign verify-attestation --type slsaprovenance1`, falling back to `gh attestation
verify --bundle-from-oci` with the same issuer and certificate identity regexp, so workflow file and
ref are pinned there too). Tags are refused: only `@sha256:` references are verified. cosign comes from
`$COSIGN_BIN`, `cosign` on PATH or the pinned `COSIGN_IMAGE` through Docker. Verification needs ghcr.io
and the public Sigstore roots; when they are unreachable it fails (fail closed).

Offline proof with real cosign: `scripts/supply-chain/tests/e2e_sign_verify.sh` starts a throwaway
registry, signs tiny images with test keys and checks that a signed image passes and an unsigned, a
wrong-key, a tag-only and a wrong-`git_sha` reference fail; that `deploy.sh --verify-only` accepts a
signed release and refuses one listing an unsigned image or a tampered file before any `gcloud` call;
and that `compose-release.sh` writes its override only for the good release.

## 9. Deploying a signed release (Cloud Run)

```bash
git checkout <RELEASE_GIT_SHA>     # the manifests are rendered from the checkout
scripts/cloudrun/deploy.sh --project P --region R --domain D --release-tag v1.2.0 --verify-only
scripts/cloudrun/deploy.sh --project P --region R --domain D --release-tag v1.2.0
scripts/cloudrun/deploy.sh --project P --region R --domain D --release ./keelson-release.env   # artifact of a main build
scripts/cloudrun/deploy.sh --project P --region R --domain D --allow-unsigned                  # local builds (dev/smoke)
```

Before any `gcloud` call deploy.sh verifies the release file, checks `RELEASE_GIT_SHA` = `HEAD`
(`--allow-sha-mismatch` to override, with a warning) and `RELEASE_DOMAIN` = `--domain`, and verifies
every image it will deploy (domain overlays with `domain=<domain>`). Any failure: exit 2, nothing
deployed. In release mode nothing is built: an Artifact Registry **remote repository** `ghcr` proxies
ghcr.io, and the services run `<region>-docker.pkg.dev/<project>/ghcr/rahulkar/keelson/<name>@<the
verified digest>` (same digest = same bytes). Without `--release`/`--release-tag` deploy.sh refuses
to run unless `--allow-unsigned` is given, and `--allow-unsigned` is refused when `.env` has
`DEMO_MODE=0`. Details: `docs/cloud-run.md` §3.

## 10. Running the local stack from a release (saves disk)

```bash
scripts/supply-chain/compose-release.sh --release keelson-release.env
docker compose build pg-core      # once: pg-core is not a release image
docker compose -f compose.yaml -f compose.release.yaml up -d --no-build
```

Writes the git-ignored `compose.release.yaml` (`build: !reset null`, `image: <verified ref@digest>` for
bootstrap, engine-api, engine-worker, optimizer, merxa-api, merxa-web, keelson-web and the signal-hub
services) only when every check passes. pg-core keeps its local build (without a local
`keelson-pg-core` image `up --no-build` stops at it, hence the one-time build). Release images are built with
the Dockerfile defaults: build arguments set in `.env` for compose builds do not apply to them.

## 11. One-time GitHub settings (manual)

1. Install the Mend Renovate app for the repository (no onboarding PR: `renovate.json` already
   exists; the dependency dashboard issue appears after the first run).
2. Enable Dependabot alerts (keep Dependabot version updates off: Renovate owns updates) and code scanning.
3. After the first release run, make the `ghcr.io/rahulkar/keelson/*` packages public (or give the
   Artifact Registry remote repository upstream credentials in Secret Manager).
4. Optional repository variable `KEELSON_PUBLIC_DOMAIN` (domain overlays for Cloud Run).
5. Branch ruleset on `main` with the required checks of section 5; protect `v*` tags. Require
   code-owner review (`.github/CODEOWNERS`) once a second maintainer exists.

## 12. Exceptions and keys

* `.grype.yaml`: an accepted vulnerability needs a reason naming who accepted it and `expires
  YYYY-MM-DD`; `test_workflows.py` fails on a missing reason or a past expiry.
* `infra/supply-chain/keys/`: vendored publisher keys with source commit, fetch date and SHA-256
  (`README.md` there). A key changes only by a reviewed PR; after an upstream rotation verification
  fails closed until the new key is vendored.
