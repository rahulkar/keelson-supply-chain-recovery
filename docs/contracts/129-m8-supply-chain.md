# 129 · M8 Supply-chain hardening: digest pins, Renovate, CI, cosign-signed releases

Plan §5 (every service "pinned by digest", the LiteLLM image "@sha256:… (cosign-verified)"), §14
(cosign, digests, monthly upgrades, "CI blocks 1.82.7/1.82.8 and litellm_init.pth"), §19 M8
("Renovate and cosign automation"), §21 (ADK/LiteLLM churn); workstream 9 of `120-m8-overview.md`.
Builds on `00-overview.md` (Python conventions), `60-build-guide.md` (containers only, version
caps), `50-engine-workflow.md` ("litellm hash-locked, never 1.82.7/1.82.8") and the Cloud Run deploy
(`docs/cloud-run.md`). Operator how-to: `docs/supply-chain.md`.

Goal: everything Keelson builds or runs is pinned to an immutable digest or commit SHA; Renovate
keeps the pins current inside the known caps and gates; CI runs every suite in the same containers
people use locally; every push to `main` and every `vX.Y.Z` tag produces keyless-signed images
with SBOM and SLSA provenance and a signed release file; deploys refuse anything that does not
verify. No runtime code, API, contract, database or LLM behaviour changes: this is build, CI and
deploy tooling. Everything is testable offline (no LLM, no GCP credentials, no GitHub).

## 1. Scope (implemented)

| Item | Where |
|---|---|
| Every base and third-party image pinned as `name:<exact tag>@sha256:<index digest>` (resolved and re-verified 2026-10-10 with `docker buildx imagetools inspect`; each equals what the old floating tag resolves to, except uv: §10) | `compose.yaml`, all Dockerfiles (`# syntax`, `FROM`, `ARG` defaults, `COPY --from` uv), `infra/cloudrun/images/*`, `infra/cloudrun/versions.env` (Phoenix `version-20.20.0` instead of `latest`), helper/test scripts, `e2e/playwright/run.sh`, workflows |
| Pin resolver | `scripts/supply-chain/pin-image.sh` |
| Registry of first-party images and CI suites (`keelson.images/1`) + strict loader | `infra/supply-chain/images.json`, `scripts/supply-chain/registry.py` |
| GitHub matrices and local == CI suite runner | `scripts/supply-chain/matrix.py`, `scripts/supply-chain/ci_run_suite.py` |
| `workspace-test` target: contracts-ts `gen-zod --check`, tokens `gen-tokens --check`, `packages/*` typecheck + vitest | `apps/keelson-web/Dockerfile` |
| Pin / cap / consistency / registry checker (P1–P9) | `scripts/supply-chain/check_pins.py` |
| Renovate policy (managers, caps, groups, gated groups, schedules, dashboard, vulnerability alerts) | `renovate.json` |
| Upgrade gate (labels `spike-verified`, `replays-rerecorded`; S1–S3 checklist in the job summary) | `scripts/supply-chain/upgrade_gate.py`, CI job `upgrade-gate` |
| CI: setup, meta (pins, actionlint, zizmor, shellcheck, Renovate validator + extraction), upgrade-gate, python suites with pg-core/Valkey, web build targets, image checks + Grype + SARIF, docker-tests | `.github/workflows/ci.yml`, `.github/zizmor.yml`, `.grype.yaml` |
| Release: push by digest, check-image, Syft SPDX SBOM, Grype gate, keyless `cosign sign` (annotations `git_sha`, `image`, `domain`), `cosign attest` SPDX, SLSA v1 provenance, signed `keelson-release.env`, domain overlays, GitHub Release assets | `.github/workflows/release.yml`, `.github/actions/sign-attest/action.yml`, `scripts/supply-chain/release_manifest.py` |
| Verification library and tools; vendored BerriAI cosign key; signing policy | `scripts/supply-chain/lib/verify.sh`, `verify-images.sh`, `check-image.sh`, `infra/supply-chain/keys/`, `infra/supply-chain/signing.env` |
| deploy.sh signed-release mode and gate (`--release`, `--release-tag`, `--verify-only`, `--allow-unsigned`, `--allow-sha-mismatch`, `--signing-env`), AR remote repository `ghcr`, Phoenix mirrored from its pinned digest; `signal-hub.sh` honours release mode (through `build_push`) | `scripts/cloudrun/deploy.sh` |
| Local stack from a verified release | `scripts/supply-chain/compose-release.sh` (git-ignored `compose.release.yaml`) |
| pnpm `minimumReleaseAge` = Renovate's 3 days | `pnpm-workspace.yaml`, `apps/merxa-web/pnpm-workspace.yaml` (copied by its Dockerfile) |
| CODEOWNERS for the supply-chain controls | `.github/CODEOWNERS` |
| Tests: tooling unit tests, Renovate extraction, offline cosign e2e, Cloud Run gate tests | `scripts/supply-chain/tests/`, `scripts/cloudrun/tests/test_cloudrun.py` |
| Docs and contract text | `docs/supply-chain.md` (new), `docs/cloud-run.md`, `docs/infra.md`, `infra/cloudrun/README.md`, `00-overview.md` (pinned uv/python, pin rule), `60-build-guide.md` (lockfiles authoritative, caps table, new-image rule), web READMEs |

## 2. Design

### 2.1 Pins

Form `name:<exact tag>@sha256:<digest>`; the digest is the one the registry serves for the tag (the
multi-platform index), so every platform stays pinnable and Renovate can propose tag and digest
updates as reviewable PRs. "Exact" = the most specific tag published (`python:3.14.8-slim-trixie`,
`caddy:2.11.7-alpine`; PostGIS only has `18-3.6`). Pinned on 2026-10-10:

| Image | Tag |
|---|---|
| python | `3.14.8-slim-trixie` (`sha256:a2b82f3c…`) |
| node | `24.21.0-slim` |
| ghcr.io/astral-sh/uv | `0.12.23` (and `0.12.23-python3.14-trixie-slim` for the contracts-py suite) |
| docker/dockerfile | `1.7.1` |
| caddy · ubuntu/squid · valkey/valkey | `2.11.7-alpine` · `6.6-24.04_beta` · `8.1.10-alpine` |
| postgres · postgis/postgis | `18.6-trixie` · `18-3.6` |
| quay.io/keycloak/keycloak · adorsys/keycloak-config-cli | `26.8.0` · `6.5.1-26` |
| ghcr.io/berriai/litellm-non_root | `1.104.0` (also verified with BerriAI's key) |
| arizephoenix/phoenix | `version-20.20.0` |
| CI/test tools | shellcheck `v0.11.0`, actionlint `1.7.12`, zizmor `1.30.1`, Renovate `44.145.1`, cosign `v3.1.3`, registry `3.1.2`, Playwright `v1.63.0-noble` |

Allowed unpinned refs: images built here (`keelson*`, `scratch`) and the overlay argument
`ARG ENGINE_IMAGE=keelson-engine-api:latest` (deploy.sh / release.yml always pass the engine image;
Renovate ignores it).

### 2.2 Image registry `infra/supply-chain/images.json` (`keelson.images/1`)

`images[]`: `name` (`[a-z0-9-]`, also the ghcr repository and `RELEASE_IMAGE_<NAME>` key), `dockerfile`,
`context`, `target` (null = last stage), `release` (built, scanned, signed, listed in the release
file), optional `deploy_var` (`IMAGE_*` of deploy.sh; release images only), `overlay_of` +
`overlay_arg` (Cloud Run overlay built `FROM` another signed image), `domain_overlay`,
`verify_upstream {versions_env_key, pubkey}`, `suites[]`, `comment`. Suites: `id`, `kind` (`run` =
run the built target; `build` = building the target is the test), `target`, `db` (`pg-core`|null),
`valkey`, `memory`, `env`, `cmd` (default: the image CMD), `deselect [{nodeid, reason}]`. Top-level
`suites[]` are toolchain suites (`image` pinned, `workdir`, `cmd`): `contracts-py` (uv: pytest, mypy,
`export_schemas.py --check`), `evals` (pytest + `run_live.py --dry-run`), `supply-chain` (check_pins +
the tooling and Cloud Run pytest suites). Registered: engine (engine-platform, engine-runtime),
optimizer, signal-hub, merxa-api, keelson-web (keelson-web, web-packages), merxa-web, pg-core,
db-init, keycloak-cloudrun, litellm-cloudrun (upstream key), engine-cloudrun (overlay of engine,
domain), kc-config-cloudrun (domain). `registry.py` validates everything strictly (unknown keys,
duplicate names/suites/Dockerfiles/deploy vars, pinned tool images, overlay rules) and reports every
problem at once.

The 120 §5 known failures are `deselect`ed with their reason (engine PDF tests, signal-hub
non-superuser bootstrap), so CI is green on the known environment and new failures still fail.

`ci_run_suite.py SUITE [--no-build] [--image REF] [--dry-run] [--keep]`: builds `keelson-ci/<image>:<target>`
(or uses `--image`), starts a throwaway pg-core (`keelson-ci/pg-core:latest`, data on a 768 MiB tmpfs
under a 1 GiB cap — tmpfs counts against the cap; engine-runtime peaks at ~360 MiB of data — `fsync=off`) and
Valkey (compose's pinned image) on a private network with the aliases `e1-test-pg` / `e1-test-valkey`
the service READMEs use, runs the suite with `--memory`, removes everything named
`keelson_ci_<suite>_<random>`. Exit = the suite's status (99 = harness failure). Stdlib; Git Bash or
the GitHub runner.

### 2.3 `check_pins.py`

Rules P1–P9 (`docs/supply-chain.md` §2): pins in Dockerfiles / compose / pin files / scripts,
workflows and the `images.json` toolchain suites (P1–P4), SHA-pinned actions with a version comment and
no `docker://` (P5), no floating or inexact tags (P6), version caps on the lockfiles (every locked
version of a package: `uv.lock` repeats a package per resolution fork) and image tags (P7: motion <14,
@tanstack/react-table <9, TypeScript <6.1 in both pnpm lockfiles, maplibre-gl ≥6.4.1, engine fastapi
<0.142 and opentelemetry-api ≤1.42.1 while google-adk ==2.11.0, LiteLLM ≠1.82.7/1.82.8 in every
`uv.lock` and as image tag, python <3.15, node <25, postgres <19, valkey <9, Keycloak <27, Caddy <3),
cross-file consistency (P8: one pin per image variant, i.e. repository + tag without its version, so
`uv:0.12.23` and `uv:0.12.23-python3.14-trixie-slim` may coexist; pnpm in both package.json, the corepack lines and
`versions.psd1`; uv, LiteLLM, Keycloak, keycloak-config-cli and Caddy = `versions.psd1`; overlay `ARG`
defaults = `versions.env`; pydantic / mcp / fastmcp equal across uv projects; AG-UI major.minor;
Playwright npm = image; Valkey major = Memorystore; pnpm `minimumReleaseAge` = Renovate's) and
registry coverage (P9: every Dockerfile, compose `build:` and `AS test` stage registered, registered
paths and targets exist, deploy.sh `IMAGE_*` = registry `deploy_var`s, deploy.sh `RELEASE_IMAGE_MAP` and
compose-release.sh `COMPOSE_RELEASE_MAP` consistent with the registry). One JSON line per finding,
exit status = number of findings; each message says what to run or add.

### 2.4 CI (`ci.yml`)

Jobs `setup`, `meta`, `upgrade-gate`, `python (<suite>)`, `web (<suite>)`, `image (<name>)`,
`docker-tests` (`docs/supply-chain.md` §5). Matrices come from `matrix.py`, so a registered image or
suite is picked up without editing the workflow. The python job builds pg-core and the test target
with `build-push-action` (`load: true`, GHA cache per image/target) and then runs exactly
`ci_run_suite.py <suite> --no-build`. Build suites use `outputs: type=cacheonly`. The image job
builds the release target as a single manifest (`provenance: false`, `sbom: false`), runs
`check-image.sh --require-labels`, then Grype (`severity-cutoff: critical`, `only-fixed`), and uploads
SARIF only for same-repository events. `docker-tests` runs on push/schedule, or for PRs touching
`scripts/`, `infra/`, `.github/` or `compose.yaml`.

### 2.5 Renovate and the upgrade gate

`renovate.json` (Mend Renovate app; `docs/supply-chain.md` §6): `config:recommended`,
`docker:pinDigests`, `helpers:pinGitHubActionDigestsToSemver`; managers npm, pep621, pip_requirements,
dockerfile, docker-compose, github-actions and regex managers for `versions.env`/`signing.env`
`*_IMAGE=` pins, corepack pnpm lines, image literals in scripts/workflows/`images.json`, and the pnpm and
LiteLLM versions of `scripts/native/versions.psd1`. Weekly schedule, monthly digests / lock-file
maintenance / CI tool images, `minimumReleaseAge` 3 days (7 for the gated stack), majors via the
dashboard, caps as `allowedVersions`, gated groups (`adk stack`, `litellm`, `mcp`, `ag-ui`: dashboard
approval, `needs-spike`, never automerged), engine fastapi cap placed after the web-stack group.
Updates of tools also pinned in `versions.psd1` (uv, Keycloak, kc-config, Caddy) carry a PR note to
update it in the same PR (P8 fails until then).

`upgrade_gate.py --base <sha>` parses `uv.lock`, `pnpm-lock.yaml` (both), `evals/requirements.txt`,
`models.lock`, `infra/litellm/config.yaml` and the LiteLLM image pins at the base (`git show`) and head,
and requires `spike-verified` / `replays-rerecorded` per `docs/supply-chain.md` §6, transitive changes
included. Labels arrive as `PR_LABELS=toJSON(...)` and are parsed as JSON only; the Markdown summary
escapes every value.

### 2.6 Releases (`release.yml`, `sign-attest`)

`setup` (opt-in: runs only when the repository variable `RELEASE_ENABLED` is `true`, so a push to `main`
publishes nothing by default — set at integration, before the first push; forks build nothing; registry
from `signing.env` must be on ghcr.io; `KEELSON_PUBLIC_DOMAIN`
validated with deploy.sh's domain rule) → `build-sign` per non-overlay release image → `overlays`
(only with a domain; the engine overlay is built `FROM` the signed engine digest passed as an
artifact) → `manifest` (`release_manifest.py`, `cosign sign-blob --bundle`, then `verify-images.sh
--release` end to end, 90-day artifact) → `sarif` → `publish` (tags: GitHub Release assets).
Release images are built **without any shared layer cache** (no cache poisoning between PR/CI and
release builds), single-manifest `linux/amd64` with OCI `source`/`revision`/`created` labels, and
tagged `sha-<12>` (+ `vX.Y.Z`); everything downstream uses the digest. LiteLLM's base is verified with
BerriAI's key before it is built on.

`keelson-release.env` (`keelson.release/1`): sorted `KEY=value` lines (`docs/supply-chain.md` §7);
every value is validated by regex both when writing and when reading (deploy.sh parses it with the
same safe read loop as `versions.env`; no jq/python on the deploy machine). Domain overlays appear
exactly when `RELEASE_DOMAIN` is set.

### 2.7 Verification (`lib/verify.sh`)

`sc_load_signing_env` (strict KEY=value, known keys only, anchored identity regexp, pinned
`COSIGN_IMAGE`), `sc_verify_ref REF [k=v…]` (refuses anything but `…@sha256:<64 hex>`; `cosign verify`
with `--certificate-oidc-issuer`, `--certificate-identity-regexp`, `--certificate-github-workflow-repository`,
`--certificate-github-workflow-trigger` and `-a` annotations; `verify-attestation --type spdxjson`; with
`REQUIRE_PROVENANCE=1` `verify-attestation --type slsaprovenance1`, falling back to `gh attestation
verify --bundle-from-oci` with the same `--cert-oidc-issuer` and `--cert-identity-regex`, so the fallback
pins workflow file and ref too), `sc_verify_upstream REF PUBKEY`, `sc_verify_release FILE` (bundle
`FILE.sigstore.json` + strict content validation; images must live under `RELEASE_REGISTRY`). Key mode
(`SIGNER_MODE=key`, `--key` + `--insecure-ignore-tlog`) exists only for the offline e2e; deploy.sh warns
loudly. cosign: `$COSIGN_BIN`, `cosign` on PATH, else the pinned image via Docker with file arguments
mounted read-only (`COSIGN_DOCKER=1` forces Docker, `COSIGN_DOCKER_NETWORK` names its network).

### 2.8 deploy.sh

Flags and preflight as in `docs/cloud-run.md` §3. The gate runs after argument/pin parsing and before
the first gcloud command; in `--dry-run` the cosign commands run (verification changes nothing) and are
echoed. `RELEASE_IMAGE_MAP` maps `IMAGE_ENGINE←ENGINE_CLOUDRUN`, `IMAGE_OPTIMIZER←OPTIMIZER`,
`IMAGE_MERXA_API`, `IMAGE_KEELSON_WEB`, `IMAGE_MERXA_WEB`, `IMAGE_LITELLM←LITELLM_CLOUDRUN`,
`IMAGE_KEYCLOAK←KEYCLOAK_CLOUDRUN`, `IMAGE_KC_CONFIG←KC_CONFIG_CLOUDRUN`, `IMAGE_DB_INIT←DB_INIT`,
`IMAGE_SIGNAL_HUB←SIGNAL_HUB` (P9 + `test_release_map_matches_the_registry`). In release mode
`build_push` builds nothing, the remote repository `ghcr` (`--mode=remote-repository
--remote-docker-repo=https://ghcr.io`, name overridable with `GHCR_REMOTE_REPO`) is created once and
every `IMAGE_*` becomes `<region>-docker.pkg.dev/<project>/ghcr/<path>@<verified digest>`;
`docker auth` is configured only when something is still pushed (Phoenix). `--allow-unsigned` keeps
the M7 behaviour exactly (all existing tests pass with the flag). `--release-tag` downloads with curl
over HTTPS only (`--proto =https`, stdout redirects) from the GitHub Release of `SIGNER_REPOSITORY`;
the release's commit is read with `cd "$ROOT" && git rev-parse HEAD` (no path arguments to native
tools, so the gate also works from Git Bash with `MSYS_NO_PATHCONV=1`).

### 2.9 compose-release.sh

Verifies the release and every image it maps (`COMPOSE_RELEASE_MAP`: bootstrap, engine-api,
engine-worker → ENGINE; optimizer; merxa-api; merxa-web; keelson-web; signal-hub-bootstrap/-api/-poller
→ SIGNAL_HUB), then writes `compose.release.yaml` atomically (`build: !reset null`, `image: <ref>`);
nothing is written on any failure. pg-core keeps its local build (not a release image).

## 3. APIs and contracts

No runtime API, Pydantic/JSON-Schema/Zod contract or wire format changes; `contracts --check` is not
affected. New file formats (documented in `docs/supply-chain.md`): `keelson.images/1`
(`images.json`), `keelson.release/1` (`keelson-release.env` + Sigstore bundle), `signing.env` keys
(`SIGNER_MODE`, `SIGNER_OIDC_ISSUER`, `SIGNER_IDENTITY_REGEXP`, `SIGNER_REPOSITORY`, `SIGNER_TRIGGER`,
`SIGNER_KEY`, `RELEASE_REGISTRY`, `REQUIRE_PROVENANCE`, `COSIGN_IMAGE`). Tool outputs are JSON lines:
`check_pins.py` `{rule, file, line, message}`, `verify-images.sh` `{image, digest, signature, sbom,
provenance, status, detail}`, `check-image.sh` `{image, check, ok, detail}`; exit status = number of
failures throughout (the `parity-check.sh` convention).

## 4. Data and migrations

None (120 §2 allocates no migration to this workstream).

## 5. Configuration

* No new compose service, volume, network or env var; only `image:` lines changed. `.env.example`
  unchanged (test_cloudrun.py treats its keys as secrets; the signing policy is the non-secret
  `infra/supply-chain/signing.env`).
* `infra/cloudrun/versions.env`: `KEYCLOAK_IMAGE`, `LITELLM_IMAGE`, `KC_CONFIG_IMAGE`,
  `POSTGRES_CLIENT_IMAGE`, `PHOENIX_IMAGE` are tag@digest and equal the overlay `ARG` defaults.
* deploy.sh: flags `--release`, `--release-tag`, `--verify-only`, `--allow-unsigned`,
  `--allow-sha-mismatch`, `--signing-env`; env `COSIGN_BIN`, `COSIGN_DOCKER`, `COSIGN_DOCKER_NETWORK`,
  `GHCR_REMOTE_REPO`.
* pnpm: `minimumReleaseAge: 4320` in both pnpm projects.
* `.gitignore`: `/compose.release.yaml`, `/keelson-release.env`, `/*.sigstore.json`.
* GitHub (manual, `docs/supply-chain.md` §11): Renovate app, Dependabot alerts, public GHCR packages
  (or AR upstream credentials), variable `RELEASE_ENABLED=true` to turn releases on, optional variable
  `KEELSON_PUBLIC_DOMAIN`, ruleset with required checks.

## 6. Security

* Only `GITHUB_TOKEN`; no stored secrets, no `pull_request_target`; top-level `permissions: {}` and
  per-job least privilege (`id-token: write` only in release jobs; `test_workflows.py`); checkouts never
  persist credentials; no `${{ … }}` in `run:` (zizmor + test); forks build no release.
* Signing identity pinned to issuer, repository, workflow file `release.yml`, ref `main` or a semver
  tag (regexp anchored; corpus test incl. `main.evil`, `-rc` tags, lookalike repositories, `ci.yml`,
  pull refs) and trigger `push`; annotations bind `git_sha` (deploy.sh also requires it to equal
  `HEAD`) and `domain`.
* Fail closed everywhere: unreachable GHCR/Rekor, a missing bundle, attestation or tool = refusal;
  deploy.sh exits 2 before any gcloud call (proven with a fake gcloud in the e2e and the unit tests).
* `--allow-unsigned` refused with `DEMO_MODE=0`; key mode warns loudly.
* LiteLLM: upstream verified with BerriAI's vendored key (CI image job, release, `verify-images.sh
  --compose`; verified live on 2026-10-10), 1.82.7/1.82.8 banned in three places (Renovate
  `allowedVersions`, check_pins P7, check-image `litellm_init.pth` + executable `.pth` check).
* Cooling-off: 3 days for every update (Renovate and pnpm), 7 days for ADK/LiteLLM/MCP/AG-UI.
* Tools pinned by digest (Syft/Grype versions pinned through the actions); `trivy-action` not used.
* CODEOWNERS on `.github/`, `renovate.json`, `.grype.yaml`, `infra/supply-chain/`, `scripts/supply-chain/`,
  `scripts/cloudrun/`, `versions.env`, `signal-hub.sh`, `models.lock`, `infra/litellm/config.yaml`.
* No application-runtime outbound HTTP was added (egress proxy unaffected); 120 §3.4 invariants hold.

## 7. Tests

| Suite | Command | Result (2026-10-10) |
|---|---|---|
| Tooling: check_pins, shellcheck (all scripts), pytest (`scripts/supply-chain/tests` + `scripts/cloudrun/tests` + `scripts/tests/test_init_secrets.py`), actionlint, zizmor | `scripts/supply-chain/tests/run.sh` | 0 failed parts; pytest 278 passed (after the review fixes) |
| Renovate validator + extraction (every pinned image extracted once, no manager duplicates, uv/npm projects, actions, nothing from fixtures) | `run.sh --with-renovate` | pass |
| Offline cosign e2e: local registry, real cosign v3.1.3 key mode: signed passes; unsigned / wrong key / tag / other `git_sha` fail; deploy.sh `--verify-only` accepts the signed release and refuses an unsigned image and a tampered file with no gcloud call; compose-release writes only for the good release and its override renders with `compose.yaml` | `run.sh --with-docker` (`tests/e2e_sign_verify.sh`) | 14/14 checks |
| Cloud Run: shellcheck, pytest 101 (incl. the new release-gate, `--release-tag` download and Phoenix-mirror tests), dbinit against Postgres 18 with the pinned images, parity self-test | `scripts/cloudrun/tests/run.sh` | pass (dbinit needs `keelson-pg-core:latest`, built for the run) |
| `ci_run_suite.py optimizer` (pinned optimizer Dockerfile built, suite run, cleanup) | through a docker-CLI container | build OK, 209 passed, 4 wall-clock budget tests (5 s) failed under shared-host load; the same 4 fail in the shared m8 harness on unchanged optimizer code (§10) |
| `workspace-test` commands + frozen installs with `minimumReleaseAge` | m8-js harness: `pnpm fetch/install --frozen-lockfile`, `gen-zod --check`, `gen-tokens --check`, `packages/*` typecheck + test | pass (251 viz tests among them); a stale generated module makes `gen-zod --check` fail. The `docker build --target workspace-test` itself was not run on this shared host (full pnpm fetch); CI's `web (web-packages)` job builds it |
| Live pins / key | `pin-image.sh --verify` equivalent over all 21 pinned refs; `verify-images.sh --compose` | all digests current; LiteLLM signature PASS |
| CI database sizing (review) | `ci_run_suite.py`'s pg-core + Valkey settings, engine suites run against them with the m8 harness image | engine-platform 464 passed / 2 skipped / 3 deselected (120 §5); engine-runtime 894 passed; pg peak ~520 MiB (356 MiB tmpfs), hence the 1 GiB cap |
| Web (review) | m8-js harness | `@keelson/web` typecheck + lint + 468 tests; packages 84 + 165 + 75 + 251 tests; merxa-web typecheck + lint + 211 tests; `pnpm config get minimumReleaseAge` = 4320 in both projects; contracts `--check` clean |
| Simulated integration (review) | `git merge-tree` with `m8-post-demo`, conflict resolved and basemap fixed as §11 | check_pins 0 findings; shellcheck clean; supply-chain + Cloud Run + init-secrets pytest pass (2 basemap tests need `--allow-unsigned`, §11) |
| Service + tool suites (review) | m8 harness (`M8_SLOT=18`); the `contracts-py` / `evals` commands of `images.json` exactly as `ci_run_suite.py` runs them (read-only mount) | merxa-api 205 passed; signal-hub 261 passed + the 120 §5 known failure; optimizer 213 passed; contracts-py 1435 passed + mypy clean + schemas up to date; evals tests + `run_live.py --dry-run` pass |

Unit tests per tool: `test_check_pins.py` (pass + fail fixture per rule, exit code, real repo clean),
`test_ci_run_suite.py` (dry-run command lines, fake docker: exit status, health wait, cleanup on
success and on harness failure, `--keep`; matrices and `$GITHUB_OUTPUT`), `test_renovate.py`,
`test_upgrade_gate.py`, `test_workflows.py` (+ signer identity corpus, Grype exception expiry,
CODEOWNERS), `test_verify_images.py`, `test_release_manifest.py` (+ compose-release with fake cosign).

## 8. Deviations

From 120: none (no allocations used; shared files touched minimally — see the integration notes).

From the design brief:
1. Re-resolved pins: `python:3.14.8-slim-trixie` is `sha256:a2b82f3c…` (the brief's `f85c…` was
   superseded by a rebuild of the same tag), Phoenix `version-20.20.0` (the current `latest`), not
   20.19.0.
2. images.json uses `suites` (not `tests`) and has no `build_args_from_versions_env`: overlay `ARG`
   defaults are kept equal to `versions.env` by P8, so release builds use the defaults. A shared
   `registry.py` validates the file for all tools.
3. Script pytest runs in the CI `python` matrix as the `supply-chain` tool suite, not inside `meta`.
4. Release builds use no layer cache at all (the brief cached on `main`): no shared cache between
   untrusted PR builds and signed builds.
5. Keycloak and Caddy caps are expressed as `/^26\./` and `/^2\./` (same effect as `<27`, `<3`).
6. Renovate also manages the pnpm and LiteLLM *versions* of `versions.psd1` (no download needed); the
   hash-pinned archives stay manual (deferred item).
7. Phoenix is mirrored with `docker pull/tag/push` of the digest-pinned reference (unchanged code
   path, works in `--allow-unsigned` too) rather than `cosign copy`; the deployed digest is the pushed
   linux/amd64 manifest of the pinned image. (Review fixes in that M7 path: `digest_of` now takes the
   RepoDigest of the pushed Artifact Registry repository — the pulled-by-digest source keeps its Docker
   Hub entry, which sorts first and was deployed instead — and the one-time Phoenix database password is
   generated without a pipefail SIGPIPE abort, which stopped every first `--enable-phoenix` deploy.)
8. merxa-web gets its own settings-only `pnpm-workspace.yaml` (pnpm 10 reads `minimumReleaseAge` there;
   verified that frozen installs and `pnpm add` are unaffected) instead of an `.npmrc`.
9. zizmor's `self-repository` audit is disabled (`.github/zizmor.yml`): actionlint 1.7.12 does not
   accept the `$/` form it prefers.
10. `scripts/cloudrun/tests/test_cloudrun.py`: three existing parity assertions already failed on the M8
    baseline (M7 compose additions never wired to Cloud Run) and now allow-list them by name:
    `SIGNALS_DATA_KEY` (optional; the hub derives its at-rest key from `SIGNAL_HUB_SHARED_SECRET`) and
    the engine's `EVALS_RESULTS_DIR` + `/app/evals/results` mount (local CLI eval results; the Cloud Run
    admin listing reports the directory absent). Every other compose secret, engine variable and mount
    is still asserted.

## 9. Deferred

* Binary Authorization enforcement (KMS attestor `keelson-cosign-verified`, `deploy.sh --binauthz`,
  `run.googleapis.com/binary-authorization` annotations); verification happens in deploy.sh instead.
* Self-hosted Renovate workflow (`renovatebot/github-action` + GitHub App token) instead of the Mend app.
* Renovate managers for the hash-pinned archives of `scripts/native/versions.psd1` + a CI job that
  downloads each URL and checks its Sha256 (versions of pnpm/LiteLLM there are managed; P8 flags drift).
* Nightly re-scan of the latest released digests; a stack-e2e workflow (compose up with
  `LLM_REPLAY_MODE=replay`, `e2e/api_m5/m6/m7_offline.py`).
* Workload Identity Federation push straight to Artifact Registry; reproducible builds
  (`SOURCE_DATE_EPOCH`, rewrite-timestamp); pinning the PGDG pgvector apt package in pg-core (PGDG
  drops old versions); an admin "build provenance" card.

## 10. Known issues

* Never run on GitHub yet: workflows are validated statically (actionlint, zizmor, structure tests),
  the signing/verification logic offline (key mode). The first push to `main` is the live acceptance:
  GHCR + cosign v3 referrers, `verify-attestation --type slsaprovenance1` on the
  `attest-build-provenance` bundle (fallback `gh attestation verify`), annotations with v3 bundles,
  AR remote repository on ghcr.io (packages must be public).
* `ghcr.io/astral-sh/uv:latest` has moved past 0.12.23; the pin stays 0.12.23 (= `versions.psd1`,
  the uv that wrote the locks). Renovate's `uv` group proposes the bump; update `versions.psd1` and the
  `uv` constraint in the same PR.
* Grype's database changes daily, so a scan can turn red without a code change (critical + fixable
  only; `.grype.yaml` exceptions with expiry).
* While Gemini credits are depleted S1 cannot run, so the upgrade gate keeps ADK/LiteLLM/MCP/AG-UI
  upgrades frozen (intended); security fixes there also wait for a human `spike-verified`.
* Release images use Dockerfile defaults: compose build args from `.env` (e.g.
  `KEELSON_CSP_MAP_ORIGINS` after the base-map merge) are not applied by compose-release or a
  release deploy.
* Domain overlays exist only when `KEELSON_PUBLIC_DOMAIN` equals the deploy domain; otherwise release
  mode refuses (use `--allow-unsigned`).
* `scripts/cloudrun/tests/dbinit.sh` needs a local `keelson-pg-core:latest` (pre-existing; CI builds it).
* Optimizer wall-clock budget tests (5 s, `test_perf.py`, `test_replan_m5.py`) fail on this shared,
  heavily loaded host in both the CI-style container and the m8 harness; not related to this change
  (they passed in the review's quieter harness run: 213 passed). Slow CI runners may hit the same budget.
* `ci_run_suite.py` tool suites bind-mount the checkout, so they need a host docker CLI (not
  docker-outside-of-docker from a container).
* Image suites run the Dockerfile `test` targets, which contain only the files those stages copy:
  engine tests that read other parts of the repository skip there (`test_replay.py::
  test_production_deployments_never_enable_replay` needs `infra/cloudrun`,
  `test_m3_candidates.py::test_ci_every_default_lever_op_is_supported_by_merxa` needs the merxa-api
  sources). They still run in the mounted-checkout harness; copying more of the tree into the test
  stages is left to the service owners.
* `compose-release.sh` replaces only release images: pg-core must exist locally (`docker compose build
  pg-core` once) before `up --no-build`.
* CODEOWNERS review cannot be required while the repository has one maintainer.

## 11. Integration notes

* Merge first (120 §6 order). A trial `git merge-tree` against `m8-post-demo` (maps-protomaps and
  signals-clustering already merged) gives **one conflict**, `scripts/cloudrun/deploy.sh` after
  `signal_hub_init`: keep both blocks — `source "$CR/basemap.sh"; basemap_init` first, then the
  signed-release loop over `RELEASE_REFS`. In release mode `KEELSON_WEB_BUILD_ARGS` (basemap CSP) cannot
  apply; consider a warning there.
* `check_pins.py` on that merge reports exactly 5 findings, all `infra/basemap/Dockerfile`. Fix
  (verified to give 0 findings): `# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e`,
  `ARG NODE_IMAGE=node:24.21.0-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20`,
  and append to `images.json`:
  `{"name": "basemap-fetch", "dockerfile": "infra/basemap/Dockerfile", "context": ".", "target": "runtime", "release": false, "suites": [{"id": "basemap-fetch", "kind": "build", "target": "test"}]}`.
* deploy.sh now refuses to run without an image source, so the two maps-protomaps tests in
  `scripts/cloudrun/tests/test_cloudrun.py` that call it directly (`test_basemap_dry_run_uploads_manifest_last`,
  `test_basemap_needs_a_directory_outside_dry_run`) need `"--allow-unsigned"` in their `deploy(...)`
  arguments (the `render(...)` helper with `--render-only` needs nothing). With the conflict resolved as
  above, the basemap fix and these two arguments, a simulated merge passes check_pins (0 findings),
  shellcheck, and all supply-chain, Cloud Run and `test_init_secrets.py` pytest suites.
* Every later workstream that adds a Dockerfile, a compose `build:`/`image:`, a `test` stage or a Cloud
  Run image: pin with `scripts/supply-chain/pin-image.sh`, register in `images.json` (+ `deploy_var`, the
  `RELEASE_IMAGE_MAP` line in deploy.sh and, for compose services from a release image,
  `COMPOSE_RELEASE_MAP`), then `python3 scripts/supply-chain/check_pins.py` must print 0 findings.
  New dependency families with a known cap get a `renovate.json` packageRule.
* Re-run `scripts/supply-chain/tests/run.sh --with-renovate --with-docker` and
  `scripts/cloudrun/tests/run.sh` after merging (they read the merged deploy.sh, compose and registry).
