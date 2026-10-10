# infra/cloudrun: Cloud Run artifacts

The deployment of the compose stack to Google Cloud Run. **How to deploy and how it maps to
compose: [`docs/cloud-run.md`](../../docs/cloud-run.md).** Nothing here runs locally; the only
entry point is `scripts/cloudrun/deploy.sh`.

| Path | What |
|---|---|
| `services/*.yaml` | Knative `serving.knative.dev/v1` Services: engine-api, keelson-web, optimizer, merxa-api, merxa-web, litellm, keycloak, phoenix (optional) |
| `workerpools/engine-worker.yaml` | `run.googleapis.com/v1` WorkerPool (no port, fixed instance count) |
| `jobs/keelson-bootstrap.yaml` | compose `bootstrap` one-shot as a Cloud Run Job |
| `jobs/keelson-kc-config.yaml` | compose `kc-config` (realm import) as a Cloud Run Job |
| `jobs/keelson-db-init.yaml` | template, one job per Cloud SQL instance: roles, databases, pinned extensions |
| `lb/url-map.yaml`, `lb/http-redirect.yaml` | global external ALB URL maps (replace Caddy's host routing) |
| `images/` | Cloud Run overlays: `engine.Dockerfile` (bakes models.lock, packs, tenants.yaml, geo), `keycloak.Dockerfile` (`kc.sh build`), `litellm.Dockerfile` (config), `kc-config.Dockerfile` (realm files), `db-init/` (psql job), `rewrite-hosts.sh` |
| `signal-hub/*.yaml`, `signal-hub.sh` | M6 Signal Hub: internal-ingress API service, bootstrap Job, one poll Job per adapter (`--once --adapter X`) run by Cloud Scheduler; `signal-hub.sh` is sourced by `scripts/cloudrun/deploy.sh` (build, secrets incl. the optional `LITELLM_SIGNALS_KEY` stored as "unset", deploy after `keelson-bootstrap` and before engine-api, which gets `SIGNAL_HUB_URL` + the shared secret) |
| `versions.env` | pins: Cloud SQL extension versions (Cloud SQL's own for POSTGRES_18), Valkey version, third-party images |
| `cloudsql.md` | Cloud SQL layout and the extension pin procedure |

Manifests contain `${NAME}` placeholders (project, region, domain, image digests, run.app URLs,
private IPs). `deploy.sh` renders them (comments are dropped, unknown placeholders are an error)
and applies them with `gcloud run services|jobs|worker-pools replace`. `deploy.sh --render-only DIR`
renders without touching anything.

Images (M8, `docs/supply-chain.md`): `deploy.sh --release FILE | --release-tag vX.Y.Z` deploys a
signed release (cosign-verified before any gcloud call; nothing is built; the verified ghcr.io
digests are pulled through the Artifact Registry remote repository `ghcr`); `--allow-unsigned`
builds the app images and the `images/` overlays locally (dev/smoke; refused with `DEMO_MODE=0`).
Every `*_IMAGE` in `versions.env` and every overlay `ARG` default is `name:<exact tag>@sha256:<digest>`
and both are equal (`scripts/supply-chain/check_pins.py` P3/P8, `test_cloudrun.py`). A new Cloud Run
image also needs an `infra/supply-chain/images.json` entry with its `deploy_var` and a line in
deploy.sh's `RELEASE_IMAGE_MAP`.

Conventions every manifest follows (checked by `scripts/cloudrun/tests`):
* container port 8080 for our images (Cloud Run sets `PORT`), 4000 for LiteLLM, 6006 for Phoenix;
  a startup probe on that port; `PORT` is never set explicitly (reserved);
* secrets only as `valueFrom.secretKeyRef` (Secret Manager id `keelson-<kebab-case env name>`), never
  literal values; each service account is granted exactly the secrets its manifest references;
* ingress `internal-and-cloud-load-balancing` for everything the ALB serves, `internal` for the
  optimizer; nothing is reachable on `*.run.app` from the internet;
* public origins come from `${DOMAIN}` only (no `localhost` survives rendering).

Tests: `scripts/cloudrun/tests/run.sh` (shellcheck, pytest on the rendered manifests and the
dry run, db-init against throwaway Postgres, parity-check self-test) and
`scripts/cloudrun/tests/overlays.sh [--with-keycloak]` (builds the overlays, imports the realms
into the optimized Keycloak). Image parity: `scripts/cloudrun/parity-check.sh`.
