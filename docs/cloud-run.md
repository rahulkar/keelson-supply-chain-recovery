# Keelson on Google Cloud Run

How the compose stack (`compose.yaml`, `docs/infra.md`) maps to Google Cloud, and how to deploy
it with `scripts/cloudrun/deploy.sh`. Artifacts: `infra/cloudrun/` (manifests, overlay images,
pins). Nothing is deployed without your GCP project: the script needs `PROJECT`, `REGION` and a
domain you control, and `--dry-run` shows every command first.

## 1. Mapping

| Compose service | Cloud Run / GCP | Notes |
|---|---|---|
| `caddy` | Global external Application Load Balancer (`EXTERNAL_MANAGED`): static IP `keelson-ip`, Google-managed certificate, serverless NEGs, URL map `keelson-urlmap` (`infra/cloudrun/lb/url-map.yaml`), port 80 redirect | Same host routing, incl. the **`/api` bypass** (`keelson.<domain>/api`, `/api/*` go straight to engine-api, never through Next.js). `/admin*` + `/realms/master*` on `auth.<domain>`: Cloud Armor `deny-403` instead of Caddy's `respond 403`. Unknown hosts: redirect to `keelson.<domain>` (Caddy: 404). |
| `keelson-web` | Service `keelson-web` | ingress internal + LB; calls engine-api on its run.app URL through the VPC |
| `engine-api` | Service `engine-api` | min 1, max 4, request timeout 3600 s (SSE), Direct VPC egress all-traffic |
| `engine-worker` | **Worker pool** `engine-worker` (`run.googleapis.com/v1 WorkerPool`) | no port; `manualInstanceCount` (default 1), 2 vCPU / 2 GiB |
| `bootstrap` (one-shot) | **Job** `keelson-bootstrap` | executed by deploy.sh after the realm import, before engine-api |
| `optimizer` | Service `optimizer` | ingress **internal** only (no LB route), min 0, concurrency 4 |
| `merxa-api` | Service `merxa-api` | ingress internal + LB; **min = max = 1** (migrates + seeds on start, in-process webhook worker, snapshots on an in-memory volume) |
| `merxa-web` | Service `merxa-web` | ingress internal + LB; calls merxa-api on its run.app URL through the VPC |
| `keycloak` | Service `keycloak` (image `infra/cloudrun/images/keycloak.Dockerfile`) | **`kc.sh build`** at image build (`KC_DB=postgres`, health, `KC_CACHE=local`), runs **`start --optimized --cache=local`**, **min = max = 1**, CPU always allocated, `KC_PROXY_HEADERS=xforwarded`, `KC_HOSTNAME=https://auth.<domain>`, backchannel dynamic |
| `kc-config` (one-shot) | **Job** `keelson-kc-config` (image `kc-config.Dockerfile`) | realm files baked in, dev origins rewritten to `<domain>` |
| `litellm` | Service `litellm` (image `litellm.Dockerfile`, config baked in) | ingress internal + LB; console `gateway.<domain>` behind **IAP** |
| `phoenix` (profile obs) | Service `phoenix` (optional, `--enable-phoenix`) | console `traces.<domain>` behind IAP, data in Cloud SQL; otherwise use Cloud Trace |
| `basemap` volume + Caddy `/basemap/*` (M8) | **Bucket** `<project>-keelson-basemap` (objects `basemap/*`, public read) behind the **backend bucket** `keelson-basemap-bb` with **Cloud CDN** (optional, `--enable-basemap --basemap-dir DIR`; `infra/cloudrun/basemap.sh`) | DIR = `scripts/basemap/export.sh DIR` from a filled local volume; upload order archive → glyphs / sprites / licences → `manifest.json` (no-cache) → prune old archives; GCS serves byte ranges, so the browser code and the CSP (`'self'`) are unchanged. Without the flag keelson-web answers `/basemap/manifest.json` with 204 and `auto` uses OpenFreeMap |
| `basemap-fetch` (profile basemap) | — (run locally, then export + `--enable-basemap`) | a Cloud Run Job writing straight to the bucket is deferred (`docs/contracts/127`) |
| `pg-core`, `pg-litellm`, `pg-merxa` | **Cloud SQL POSTGRES_18** (`keelson-pg`; `--sql-isolation`: + `keelson-pg-litellm`, `merxa-pg`) | private IP only, TLS required, roles/databases/extensions by Job `keelson-db-init-<instance>`; see `infra/cloudrun/cloudsql.md` |
| `valkey` | **Memorystore for Valkey** `keelson-valkey` (`VALKEY_8_0`, cluster mode disabled, AOF, `maxmemory-policy=noeviction`) | Private Service Connect endpoint in `keelson-vpc`, reached through **Direct VPC egress** |
| `egress` (Squid) | Cloud NAT `keelson-nat` (+ the engine's own public-IP-only check) | see "What differs" |
| compose `secrets:` + `.env` | **Secret Manager** (`keelson-<kebab-case name>`) | env vars via `secretKeyRef`; see section 5 |
| networks `edge/app/ai/vendor` | ingress settings + one VPC `keelson-vpc` / subnet `keelson-run` | see section 5 |

URL map (`infra/cloudrun/lb/url-map.yaml`):

| Host | Path | Backend |
|---|---|---|
| `keelson.<domain>` | `/api`, `/api/*` | engine-api |
| | `/basemap/*` (only with `--enable-basemap`) | backend bucket `keelson-basemap-bb` (Cloud CDN) |
| | everything else | keelson-web |
| `merxa.<domain>` | `/v1/*`, `/mcp`, `/mcp/*`, `/mcp-lite`, `/mcp-lite/*`, `/.well-known/*` | merxa-api |
| | everything else | merxa-web |
| `auth.<domain>` | all (Cloud Armor blocks `/admin*`, `/realms/master*`) | keycloak |
| `gateway.<domain>` | `/__keelson/*` (console hand-off) | engine-api |
| | everything else (IAP) | litellm |
| `traces.<domain>` | as gateway (only with `--enable-phoenix`) | phoenix |

Public origins (issuers, redirect URIs, cookie scope) all derive from the one `--domain`:
`OIDC_ISSUER=https://auth.<domain>/realms/keelson`, `MERXA_ISSUER=…/realms/merxa`,
`PUBLIC_URL=https://keelson.<domain>` (https: the engine sets `Secure` cookies; cookies stay
host-only), `PUBLIC_BASE_DOMAIN=<domain>`, `MCP_RESOURCE=https://merxa.<domain>/mcp`,
`TRUSTED_RECEIPT_ISSUERS=https://keelson.<domain>`, `KC_HOSTNAME=https://auth.<domain>`,
`PROXY_BASE_URL=https://gateway.<domain>`. Realm redirect URIs/web origins/JWKS URLs and the
tenant/pack connector URLs are rewritten at image build (`infra/cloudrun/images/rewrite-hosts.sh`;
the build fails if any `.localhost` origin is left).

## 2. Prerequisites

* A GCP project with billing, and an account with Owner (or: Run Admin, Cloud SQL Admin,
  Compute Network Admin, Secret Manager Admin, Service Account Admin + User, Artifact Registry
  Admin, Memorystore Admin, IAP Admin, Logging Admin, Service Usage Admin).
* A domain whose DNS you control (`keelson.`, `merxa.`, `auth.`, `gateway.` [+ `traces.`] below it).
* Tools: `gcloud` (recent, with `run worker-pools` and `memorystore` command groups), `docker`
  (buildx, linux/amd64), `jq`, `bash`. Cloud Shell has all of them.
* `scripts/init.sh` has generated `.env` and `secrets/` (the values are uploaded to Secret
  Manager). `GEMINI_API_KEY` in the environment.
* Quota: Cloud SQL 1 (or 3) instance(s), Memorystore 1 instance, 1 global static IP.

## 3. Deploy, step by step

```bash
gcloud auth login && gcloud auth application-default login
export GEMINI_API_KEY=...               # never written to a file
# 1. look first: verifies the signed release, prints every gcloud command, executes nothing
scripts/cloudrun/deploy.sh --project my-proj --region europe-west1 --domain demo.example.com \
  --release-tag v1.2.0 --dry-run
# 2. deploy (idempotent: re-run after any change); the checkout must be at the release's commit
scripts/cloudrun/deploy.sh --project my-proj --region europe-west1 --domain demo.example.com \
  --release-tag v1.2.0 --iap-members group:platform-admins@example.com
# dev/smoke only: build and deploy unsigned local images (refused when .env has DEMO_MODE=0)
scripts/cloudrun/deploy.sh --project my-proj --region europe-west1 --domain demo.example.com --allow-unsigned
```

**Image source (M8, `docs/supply-chain.md`).** deploy.sh needs one of: `--release FILE` (a
`keelson-release.env` from `.github/workflows/release.yml`, its Sigstore bundle next to it),
`--release-tag vX.Y.Z` (the same, downloaded from the GitHub Release) or `--allow-unsigned`.
Before **any** gcloud call it verifies, with cosign against `infra/supply-chain/signing.env`, the
release file's signature, `RELEASE_GIT_SHA` = the checkout's `HEAD` (`--allow-sha-mismatch` to
override), `RELEASE_DOMAIN` = `--domain` (domain overlays are built only when the repository
variable `KEELSON_PUBLIC_DOMAIN` is set) and, for every image it will deploy, the keyless signature
of `release.yml` (annotation `git_sha`, `domain` for the overlays), the SPDX SBOM and the SLSA v1
provenance. Any failure exits 2 with nothing deployed; `--verify-only` stops after the check.

What happens (each step checks before it creates):

1. **APIs + network**: `run`, `artifactregistry`, `sqladmin`, `servicenetworking`, `compute`,
   `secretmanager`, `memorystore`, `networkconnectivity`, `serviceconsumermanagement`, `iap`,
   `logging`, `cloudtrace`. VPC `keelson-vpc`, subnet `keelson-run` (`10.20.0.0/24`, Private
   Google Access), Cloud Router + NAT, Private Services Access range `keelson-psa` (Cloud SQL),
   service connection policy `keelson-memorystore` (service class `gcp-memorystore`).
2. **Images**: Artifact Registry repo `keelson`. Signed release: nothing is built; a **remote
   repository** `ghcr` proxies ghcr.io and every service runs
   `<region>-docker.pkg.dev/<project>/ghcr/rahulkar/keelson/<image>@<verified digest>` (same digest,
   same bytes). `--allow-unsigned`: builds `engine` (+ overlay), `optimizer`, `merxa-api`,
   `keelson-web`, `merxa-web`, `keycloak`, `litellm`, `kc-config`, `db-init` for `linux/amd64`,
   pushes and **deploys by digest** (`--tag`, default a UTC timestamp; `--skip-build` reuses pushed
   tags). Phoenix (`--enable-phoenix`) is mirrored from its digest-pinned `versions.env` reference.
3. **Data**: Cloud SQL instance(s) (`POSTGRES_18`, `ENTERPRISE`, private IP, `ENCRYPTED_ONLY`),
   `postgres` password = `PG_CORE_SUPERPASSWORD`; Memorystore for Valkey (10-15 min the first time).
4. **Secrets + identities**: one Secret Manager secret per secret (section 5); a new version is
   added only when the value changed. Service accounts `keelson-engine`, `keelson-web`,
   `keelson-optimizer`, `merxa-api`, `merxa-web`, `keelson-litellm`, `keelson-keycloak`,
   `keelson-jobs` (+ `keelson-phoenix`) with log/metric/trace writer roles, and
   `secretAccessor` on exactly the secrets their manifests reference.
5. **Cloud Run**, in dependency order: `keelson-db-init-*` jobs → keycloak, litellm, optimizer,
   merxa-api (`services replace` waits for readiness) → `keelson-kc-config` job →
   `keelson-bootstrap` job → engine-api, engine-worker pool, keelson-web, merxa-web.
6. **Load balancer**: NEGs + backend services, Cloud Armor `keelson-auth-edge`, IAP on
   `litellm-bs` (+ `phoenix-bs`), URL maps, HTTPS proxy + certificate, forwarding rules 443/80,
   a `_Default` log-sink exclusion for request URLs with `code=`/`session_state=`/`ticket=`.
7. **You**: create DNS `A` records for the printed hosts → the printed IP. The managed
   certificate turns `ACTIVE` 15-60 min after DNS resolves; then open `https://keelson.<domain>`.

Other entry points: `--render-only DIR` (render manifests only), `--enable-phoenix`,
`--enable-basemap --basemap-dir DIR` (M8 self-hosted base map; `KEELSON_BASEMAP` / the build arg
`KEELSON_CSP_MAP_ORIGINS` are taken from the environment or `.env`),
`--sql-isolation`, `--env-file`, `--secrets-dir`; knobs `SQL_TIER`, `VALKEY_NODE_TYPE`,
`WORKER_INSTANCES`, `SUBNET_RANGE`. Updating one service after a code change: re-run the script
(idempotent), or build/push the image and `gcloud run services replace` the rendered manifest.

Verify parity locally before deploying: `scripts/cloudrun/parity-check.sh` (section 8).

## 4. What differs from local

| Topic | Local (compose) | Cloud Run |
|---|---|---|
| TLS / scheme | `http://*.localhost` | `https://*.<domain>`; `Secure` cookies; HTTP redirects to HTTPS |
| Host routing | Caddy | ALB URL map; unknown host → redirect instead of 404 |
| Hostname aliases inside containers | `auth.localhost`/`keelson.localhost` resolve to Caddy | real DNS; engine services reach the public hosts through Cloud NAT and the ALB |
| Service-to-service | compose DNS (`http://engine-api:8000`) | deterministic `https://<svc>-<project-number>.<region>.run.app` through Direct VPC egress (internal ingress) |
| Admin consoles | Caddy `forward_auth` → engine-api (Keelson `platform-admin` role) | **IAP** (Google identity + `roles/iap.httpsResourceAccessor`, `--iap-members`); the Keelson role is no longer checked at the edge. LiteLLM's own UI login (`admin` / `LITELLM_UI_PASSWORD`) still applies |
| Keycloak admin | blocked unless `DEV_ALLOW_KC_ADMIN=1` | Cloud Armor rule (same switch); admin API only via the run.app URL from inside the VPC |
| Connector egress | Squid `egress:3128` (deny private ranges, service names, metadata) **at connect time** | **no forward proxy** (Cloud Run cannot host a CONNECT proxy): `CONNECTOR_PROXY_URL=""`, traffic leaves via Cloud NAT. The engine's "public IPs only" check (`connector_admin.check_url`) runs when a connector URL is saved, not when it is used. **Known gap, see "Connector egress (SSRF)" in section 5** |
| Valkey auth | `--requirepass VALKEY_PASSWORD` | Memorystore `auth-disabled` + no TLS, reachable only from the VPC (PSC). `VALKEY_PASSWORD` is stored in Secret Manager but not consumed. Option: `--authorization-mode=iam-auth` + TLS once the engine supports it |
| Postgres extensions | PostGIS 3.6.4, pgvector 0.8.7, pgcrypto 1.4 | Cloud SQL's: PostGIS 3.6.0, pgvector 0.8.5, pgcrypto 1.3 (pinned in `versions.env`) |
| Filesystem | writable container FS + volumes | in-memory writable FS; `/tmp` counts against memory (engine-worker: Parquet staging → 2 GiB); merxa snapshots on an in-memory volume (lost on restart; the engine re-requests a snapshot) |
| Bind mounts | `models.lock`, `data/packs`, `infra/tenants.yaml`, realm files, LiteLLM config | baked into overlay images (`infra/cloudrun/images`) |
| Secrets | files in `/run/secrets` + `.env` | env vars from Secret Manager; `read_secret()` falls back to env `NAME` when `/run/secrets/<name>` is absent |
| Access logs | Caddy redacts `code`, `session_state`, `ticket`, credential headers | Cloud Run/ALB logs cannot redact: request logs with those query params are **excluded** from `_Default`; headers are never logged by Google |
| Client IP | Caddy overwrites `X-Forwarded-For` | Google front end **appends**; uvicorn with `--forwarded-allow-ips '*'` takes the left-most (client-supplied) entry (no service uses the client IP today) |
| Phoenix | container on the `obs` profile | optional service on Cloud SQL, or Cloud Trace |
| Self-hosted base map (M8) | `basemap` volume filled by `basemap-fetch`, Caddy `file_server` (ranges, `manifest.json` 204 when empty) | optional public bucket + Cloud CDN (`--enable-basemap`); the bucket is filled from a local export, not fetched in GCP; an org policy enforcing public access prevention blocks it (then leave the flag off: `auto` falls back to OpenFreeMap) |
| Scale | one container each | autoscaling (engine-api 1-4, web 0-4/0-2, optimizer 0-3); Keycloak and merxa-api pinned to 1 |

## 5. Security

* **Supply chain** (M8, `docs/supply-chain.md`): only images whose signature, SBOM and provenance
  verify against the release identity (this repository's `release.yml` on `main` or a `vX.Y.Z`
  tag, push trigger) are deployed; verification fails closed (GHCR or Rekor unreachable = no
  deploy). Third-party images are pinned by digest in `infra/cloudrun/versions.env`; the LiteLLM
  base is verified with BerriAI's vendored cosign key in CI. Binary Authorization enforcement is
  deferred (`docs/contracts/129-m8-supply-chain.md`).
* **Ingress**: nothing is reachable on `*.run.app` from the internet. ALB-served services use
  `internal-and-cloud-load-balancing`; the optimizer is `internal`. Cloud Run IAM invoker checks
  are disabled (`run.googleapis.com/invoker-iam-disabled`) because the apps authenticate
  themselves (BFF session, service JWTs, master key); ingress is the network boundary.
* **Internal callers** (engine-api, engine-worker, bootstrap, kc-config, both web apps) use Direct
  VPC egress with `all-traffic`, so their calls to run.app URLs count as internal; merxa-api,
  litellm, keycloak use `private-ranges-only` (only Cloud SQL through the VPC).
* **Consoles**: LiteLLM (and Phoenix) only via the ALB with IAP. Keycloak `/admin` and the master
  realm are denied at the edge (Cloud Armor rule 1000 on `keycloak-bs`). `request.path` is the raw
  path while Keycloak decodes it, so the rule matches `request.path.urlDecodeUni().lower()`: any
  case, repeated slashes, `;matrix` parameters, `%xx`/`%uxxxx` encodings, and it denies every path
  with a `.`/`..` segment (Keycloak never needs one). deploy.sh re-applies the expression on every
  run (`rules update` when the rule exists). The corpus of bypass attempts it must block, and of
  public realm paths it must allow, is in `scripts/cloudrun/tests/test_cloudrun.py`. The
  master-realm admin credentials stay the second line of defence, as locally.
* **Realms over TLS**: the dev realm files say `sslRequired: none` (Caddy serves plain HTTP); the
  kc-config overlay rewrites it to `external` (Keycloak's production default). Every request
  reaches Keycloak over TLS anyway (ALB or run.app, `X-Forwarded-Proto: https`).
* **Forwarded headers**: the Google front end sets `X-Forwarded-Proto` and appends to
  `X-Forwarded-For` but passes a client's `X-Forwarded-Host` through, unlike Caddy, which wrote
  all of them. Keycloak (`KC_PROXY_HEADERS=xforwarded`) is safe because `KC_HOSTNAME` is a full
  URL (issuer and front-channel URLs are fixed); only the backchannel URLs in a client's own
  discovery response and the client IP in Keycloak events can be influenced by the client.
* **Secrets**: every secret of `.env.example` and `secrets/` becomes a Secret Manager secret
  (`keelson-<kebab>`; e.g. `SESSION_SECRET` → `keelson-session-secret`, `secrets/keelson_data_key`
  → `keelson-data-key`), plus `keelson-gemini-api-key` (from the environment), the DSNs
  `keelson-database-url`, `keelson-adk-database-url`, `keelson-merxa-database-url`,
  `keelson-litellm-database-url`, `keelson-valkey-url`, and `MERXA_WEBHOOK_SECRET` /
  `MERXA_SNAPSHOT_SECRET` (read by compose, missing from `.env.example`). Values go to
  `gcloud secrets versions add --data-file=-` on stdin; they never appear in arguments, the log or
  the rendered manifests (tested). Exception: the Cloud SQL `postgres` password is passed to
  `gcloud sql users set-password` as an argument on the deploying machine (masked in the log).
  Manifests reference `key: latest`; pin versions for production change control.
* **Connector egress (SSRF) — open, must be closed before real tenants**: locally Squid re-checks
  every connector/snapshot request's *resolved* destination. On Cloud Run nothing does, and the
  engine's own check only runs when a URL is saved. Two consequences for a tenant admin who can
  register connector URLs: (1) DNS rebinding (public IP when saved, private IP when used) reaches
  the VPC (Cloud SQL, Memorystore with auth disabled) and the metadata server
  (`169.254.169.254`, the engine service account token; connectors may carry static headers such
  as `Metadata-Flavor`); (2) any `*.run.app` URL resolves to public Google addresses and passes
  the check, but from the engine (Direct VPC egress, all-traffic) it counts as **internal** traffic,
  so it reaches the internal-ingress services (keycloak incl. `/admin`, litellm, optimizer) past the
  ALB, Cloud Armor and IAP. Fix (engine, owner EN): enforce the policy at connect time (resolve,
  check, connect to the checked address) and refuse `*.run.app`, `*.internal`, `*.goog` and
  metadata hosts; or put Secure Web Proxy (explicit proxy mode, a gateway security policy that
  allows only the connector hosts) in the VPC and set `CONNECTOR_PROXY_URL` to it. Until then keep
  tenant-admin rights to the platform operators (the demo default).
* **Least privilege**: one service account per workload; `secretAccessor` per secret derived from
  the manifests (e.g. the web apps get none, the optimizer only its shared secret, only LiteLLM
  sees the Gemini key and the salt key).
* **Never rotate `LITELLM_SALT_KEY`** (as locally). Other DB passwords: add a secret version,
  re-run the db-init job (`gcloud run jobs execute keelson-db-init-keelson-pg`), redeploy consumers.
* **Containers**: non-root images, no root FS writes needed (verified with a read-only root FS by
  `parity-check.sh`).
* Demo mode on a real domain: the persona switcher needs header `X-Demo-Access: <DEMO_ACCESS_SECRET>`
  (not localhost); set `DEMO_MODE=0` / `DEMO_ENABLED=false` in `.env` for non-demo deployments.

## 6. Costs and scale (smoke deploy, list prices, order of magnitude)

| Item | Setting | Rough monthly cost |
|---|---|---|
| Cloud SQL | 1 × `db-custom-1-3840`, 10 GB SSD, zonal | ~$50 (×3 with `--sql-isolation`) |
| Memorystore for Valkey | `shared-core-nano`, 1 node | ~$20-40 |
| Always-on Cloud Run | engine-api, keycloak, merxa-api, litellm (min 1; keycloak + merxa-api CPU always on) + worker pool 1 × 2 vCPU | ~$150-250 |
| Scale-to-zero services | web apps, optimizer | per request |
| Load balancer | forwarding rules + data | ~$20 |
| Cloud NAT, Artifact Registry, Secret Manager, logging | | a few $ |

Tear down: delete the forwarding rules, proxies, URL maps, backend services, NEGs, certificate,
IP, Cloud Run services/jobs/worker pool, Memorystore, Cloud SQL (deletion protection must be
removed first), secrets, NAT/router, PSA peering, VPC. Scale knobs: engine-api `maxScale`,
`WORKER_INSTANCES` (runs are claimed with leases, so more workers are safe), optimizer `maxScale`
(CPU-bound, one solve per instance). Keycloak cannot scale out with `--cache=local` (sessions in
memory); scaling it needs the distributed cache (`--cache=ispn` + JDBC-ping) and is out of scope.
merxa-api is a mock vendor pinned to one instance; to scale it, move snapshots to a Cloud Storage
volume (`csi: gcsfuse.run.googleapis.com`) and the webhook worker to a separate worker pool.

## 7. Tests

`scripts/cloudrun/tests/run.sh` (no GCP needed): shellcheck; pytest over the rendered manifests
and the dry run (fake `gcloud`/`docker` on PATH prove nothing executes; random fake secrets prove
nothing leaks; secret wiring vs `.env.example`/compose; ingress, ports, probes, Keycloak flags,
origins, URL map vs Caddyfile, least-privilege grants, command order; the signed-release gate with
a fake cosign: refusal without a release, `--allow-unsigned` with `DEMO_MODE=0`, verification before
the first gcloud call, failed signature / wrong domain / SHA mismatch); `dbinit.sh` (db-init image
against throwaway Postgres 18: superuser and Cloud-SQL-like non-superuser admin, idempotency,
rotation, pin drift); `parity-check.sh --selftest`. `scripts/cloudrun/tests/overlays.sh
--with-keycloak` builds the overlays, starts the optimized Keycloak and imports both rewritten
realms with kc-config.

## 8. Parity check

`scripts/cloudrun/parity-check.sh` runs every app image like Cloud Run: `PORT=8080`, `K_*`
variables, read-only root FS + tmpfs `/tmp` (plus the in-memory volumes the manifests declare),
all capabilities dropped, no-new-privileges, the image's own non-root `USER`, a throwaway network
with throwaway pg-core + valkey, one random loopback port for the probe. HTTP images must answer
2xx on `/healthz` (`/api/healthz`) within 60 s; engine-worker must still run after 25 s with a clean
log. Options: `--only`, `--no-build` (otherwise missing images are built with `docker compose
build`), `--with-keycloak` (also the `kc.sh build` image with `start --optimized --cache=local`),
`--selftest`, `--image svc=ref` (check a specific build instead of `keelson-<svc>:latest`).

Last run (2026-10-06 22:20 IST, review): fresh builds of the current M3 sources (engine incl. the
`0004_scn` migration, keelson-web, merxa-web, merxa-api as throwaway tags via `--image`;
optimizer `:latest` built 21:54) - all PASS; Keycloak 63 s. Earlier run (images of the running
stack):

| Image | Result | Time to healthy |
|---|---|---|
| optimizer | PASS (`/healthz` 200, uid 10001) | 4 s |
| engine-api | PASS (`/api/healthz` 200, uid 10001) | 5 s |
| engine-worker | PASS (running, no FS errors, uid 10001) | 25 s settle |
| merxa-api | PASS (`/healthz` 200 after migrate + `small` seed, uid 10001) | 9 s |
| keelson-web | PASS (`/healthz` 200, uid 10001) | 2 s |
| merxa-web | PASS (`/healthz` 200, uid 10001) | 2 s |
| keycloak (Cloud Run image) | PASS (`/realms/master` 200, uid 1000; first boot creates the schema) | 57-163 s on 1 vCPU |

Finding that changed the manifests: Keycloak 26.8 opens port 8080 while still bootstrapping and
answers 503 until ready, so its startup probe is an HTTP probe on `/realms/master`, not TCP.
