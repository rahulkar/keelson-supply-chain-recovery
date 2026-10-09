"""Static + dry-run tests for the Cloud Run artifacts (infra/cloudrun, scripts/cloudrun).

Run (no gcloud / GCP needed):  scripts/cloudrun/tests/run.sh
Every deploy.sh invocation here is --dry-run or --render-only with a fake gcloud/docker on PATH
that records any call, so the tests also prove that nothing is executed.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "scripts" / "cloudrun" / "deploy.sh"
CR = ROOT / "infra" / "cloudrun"
DOMAIN = "demo.example.com"
PROJECT = "keelson-demo-123"
REGION = "europe-west1"

# .env.example keys that are configuration, not secrets.
NON_SECRET = {
    "PUBLIC_BASE_DOMAIN", "HTTP_PORT", "DEMO_MODE", "DEMO_ENABLED", "SEED_PROFILE",
    "BRAND_ENGINE_NAME", "BRAND_VENDOR_NAME", "DEV_ALLOW_KC_ADMIN",
}
LB_SERVICES = {"engine-api", "keelson-web", "merxa-api", "merxa-web", "keycloak", "litellm", "phoenix"}
SECRETISH = re.compile(r"(SECRET|PASSWORD|_KEY$|DATABASE_URL$|^VALKEY_URL$|^PGPASSWORD$)")
# Caddy hosts of optional local compose profiles that are never deployed to Cloud Run
# (M8: the adapter sidecar demo "Fernhill ERP", profile adapters, docs/contracts/122).
LOCAL_ONLY_HOSTS = {"fernhill"}


# ───────────────────────────── helpers ─────────────────────────────
def secret_id(env_name: str) -> str:
    s = env_name.lower().replace("_", "-")
    return s if s.startswith("keelson-") else f"keelson-{s}"


def bash_array(name: str) -> list[str]:
    text = DEPLOY.read_text(encoding="utf-8")
    m = re.search(rf"^{name}=\((.*?)\)", text, re.S | re.M)
    assert m, name
    return m.group(1).split()


@pytest.fixture(scope="session")
def fakebin(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """gcloud / docker / jq stand-ins that only record that they were called."""
    d = tmp_path_factory.mktemp("fakebin")
    marker = d / "called.log"
    for tool in ("gcloud", "docker", "jq"):
        p = d / tool
        p.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{marker}"\nexit 97\n', encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d, marker


@pytest.fixture(scope="session")
def fake_env(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, dict[str, str]]:
    """A .env + secrets/ with unique random values (to prove none of them is printed)."""
    d = tmp_path_factory.mktemp("fakeenv")
    values: dict[str, str] = {}
    lines = []
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0]
        if key in NON_SECRET:
            lines.append(line)
            continue
        values[key] = "v" + secrets.token_hex(12)
        lines.append(f"{key}={values[key]}")
    for key in ("MERXA_WEBHOOK_SECRET", "MERXA_SNAPSHOT_SECRET", "SIGNAL_HUB_SHARED_SECRET", "SIGNALS_DB_PASSWORD"):
        values[key] = "v" + secrets.token_hex(12)
        lines.append(f"{key}={values[key]}")
    (d / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sd = d / "secrets"
    sd.mkdir()
    for f in ("keelson_client_assertion_key.pem", "keelson_receipt_key.pem", "keelson_data_key"):
        values[f] = "file" + secrets.token_hex(16)
        (sd / f).write_text(values[f] + "\n", encoding="utf-8")
    return d / ".env", sd, values


def deploy(*args: str, fakebin: tuple[Path, Path], env_file: Path | None = None,
           secrets_dir: Path | None = None, extra_env: dict[str, str] | None = None,
           check: bool = True) -> subprocess.CompletedProcess[str]:
    bindir, _ = fakebin
    env = {k: v for k, v in os.environ.items() if k not in {"PROJECT", "REGION", "DOMAIN", "DRY_RUN"}}
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["GEMINI_API_KEY"] = "gemini-" + "x" * 8
    env.update(extra_env or {})
    cmd = ["bash", str(DEPLOY), *args]
    if env_file:
        cmd += ["--env-file", str(env_file)]
    if secrets_dir:
        cmd += ["--secrets-dir", str(secrets_dir)]
    res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    if check:
        assert res.returncode == 0, res.stdout + res.stderr
    return res


def render(tmp: Path, fakebin: tuple[Path, Path], *extra: str) -> dict[str, dict]:
    out = tmp / "render"
    deploy("--project", PROJECT, "--region", REGION, "--domain", DOMAIN, "--tag", "t1",
           "--render-only", str(out), *extra, fakebin=fakebin)
    docs = {}
    for p in sorted(out.rglob("*.yaml")):
        text = p.read_text(encoding="utf-8")
        assert "${" not in text or not re.search(r"\$\{[A-Z][A-Z0-9_]*\}", text), p
        docs[str(p.relative_to(out)).replace(os.sep, "/")] = yaml.safe_load(text)
    return docs


def containers(doc: dict) -> list[dict]:
    spec = doc["spec"]["template"]["spec"]
    if doc["kind"] == "Job":
        spec = spec["template"]["spec"]
    return spec["containers"]


def pod_spec(doc: dict) -> dict:
    spec = doc["spec"]["template"]["spec"]
    return spec["template"]["spec"] if doc["kind"] == "Job" else spec


def env_of(doc: dict) -> dict[str, dict]:
    return {e["name"]: e for e in containers(doc)[0].get("env", [])}


@pytest.fixture(scope="session")
def rendered(tmp_path_factory: pytest.TempPathFactory, fakebin: tuple[Path, Path]) -> dict[str, dict]:
    return render(tmp_path_factory.mktemp("r"), fakebin)


@pytest.fixture(scope="session")
def dry(fakebin: tuple[Path, Path], fake_env: tuple[Path, Path, dict[str, str]],
        tmp_path_factory: pytest.TempPathFactory) -> subprocess.CompletedProcess[str]:
    env_file, sd, _ = fake_env
    tmp = tmp_path_factory.mktemp("dry")
    return deploy(PROJECT, REGION, "--domain", DOMAIN, "--dry-run", "--tag", "t1",
                  "--iap-members", "group:platform-admins@example.com",
                  fakebin=fakebin, env_file=env_file, secrets_dir=sd, extra_env={"TMPDIR": str(tmp)})


# ───────────────────────────── secrets coverage ─────────────────────────────
def test_every_env_example_secret_is_wired() -> None:
    keys = [ln.split("=", 1)[0] for ln in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
            if ln and not ln.startswith("#") and "=" in ln]
    wired = set(bash_array("ENV_SECRETS"))
    missing = [k for k in keys if k not in NON_SECRET and k not in wired]
    assert not missing, f".env.example secrets not wired in deploy.sh ENV_SECRETS: {missing}"


def test_compose_env_secrets_are_wired() -> None:
    """compose.yaml reads secrets from .env that .env.example does not list (webhook/snapshot)."""
    refs = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", (ROOT / "compose.yaml").read_text(encoding="utf-8")))
    secretish = {r for r in refs if SECRETISH.search(r) and r != "GEMINI_API_KEY"}
    wired = set(bash_array("ENV_SECRETS")) | set(bash_array("OPTIONAL_SECRETS"))
    assert secretish <= wired, secretish - wired


def test_compose_secret_files_are_wired() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    files = {Path(v["file"]).name for v in compose["secrets"].values()}
    assert files == set(bash_array("FILE_SECRETS"))


def test_secret_like_env_is_never_a_literal(rendered: dict[str, dict]) -> None:
    for name, doc in rendered.items():
        if name.startswith("lb/"):
            continue
        for c in containers(doc):
            for e in c.get("env", []):
                if SECRETISH.search(e["name"]) and e["name"] not in {"KC_DB_USERNAME"}:
                    assert "valueFrom" in e and "value" not in e, f"{name}: {e['name']} must come from Secret Manager"


def test_every_referenced_secret_is_created(rendered: dict[str, dict], dry: subprocess.CompletedProcess[str]) -> None:
    created = set(re.findall(r"^\+ gcloud secrets create (\S+)", dry.stdout, re.M))
    refs = {e["valueFrom"]["secretKeyRef"]["name"]
            for n, d in rendered.items() if not n.startswith("lb/")
            for c in containers(d) for e in c.get("env", []) if "valueFrom" in e}
    assert refs <= created, refs - created
    # and every .env.example secret + secret file gets its own Secret Manager secret
    for k in bash_array("ENV_SECRETS") + bash_array("OPTIONAL_SECRETS"):
        assert secret_id(k) in created, k
    for f in bash_array("FILE_SECRETS"):
        assert secret_id(f.removesuffix(".pem").upper()) in created, f
    assert "keelson-gemini-api-key" in created


def test_secret_access_is_least_privilege(rendered: dict[str, dict], dry: subprocess.CompletedProcess[str]) -> None:
    grants = set(re.findall(
        r"^\+ gcloud secrets add-iam-policy-binding (\S+) --member=serviceAccount:(\S+) --role=roles/secretmanager.secretAccessor",
        dry.stdout, re.M))
    expected = set()
    for n, d in rendered.items():
        if n.startswith("lb/"):
            continue
        sa = pod_spec(d)["serviceAccountName"]
        for c in containers(d):
            for e in c.get("env", []):
                if "valueFrom" in e:
                    expected.add((e["valueFrom"]["secretKeyRef"]["name"], sa))
    assert grants == expected
    by_sa: dict[str, set[str]] = {}
    for sid, sa in grants:
        by_sa.setdefault(sa.split("@")[0], set()).add(sid)
    assert "keelson-web" not in by_sa and "merxa-web" not in by_sa  # web apps hold no secrets
    assert by_sa["keelson-optimizer"] == {"keelson-optimizer-shared-secret"}
    assert "keelson-litellm-salt-key" not in by_sa["keelson-engine"]
    assert "keelson-gemini-api-key" in by_sa["keelson-litellm"] and "keelson-gemini-api-key" not in by_sa["keelson-engine"]


# ───────────────────────────── dry run behaviour ─────────────────────────────
def test_dry_run_executes_nothing(dry: subprocess.CompletedProcess[str], fakebin: tuple[Path, Path]) -> None:
    _, marker = fakebin
    assert not marker.exists(), marker.read_text() if marker.exists() else ""
    assert dry.stdout.count("\n+ ") > 100


def test_dry_run_never_prints_secret_values(dry: subprocess.CompletedProcess[str],
                                            fake_env: tuple[Path, Path, dict[str, str]]) -> None:
    _, _, values = fake_env
    out = dry.stdout + dry.stderr
    leaked = [k for k, v in values.items() if v in out]
    assert not leaked, leaked
    assert "gemini-xxxxxxxx" not in out
    assert "--password=[PG_CORE_SUPERPASSWORD]" in out
    assert re.search(r"secrets versions add keelson-session-secret --data-file=- .*# stdin: \.env SESSION_SECRET", out)


def test_dry_run_infrastructure_commands(dry: subprocess.CompletedProcess[str]) -> None:
    out = dry.stdout
    pins = dict(ln.split("=", 1) for ln in (CR / "versions.env").read_text().splitlines() if ln and not ln.startswith("#"))
    sql = re.search(r"^\+ gcloud sql instances create keelson-pg .*$", out, re.M)
    assert sql
    for flag in ("--database-version=POSTGRES_18", "--no-assign-ip", "--ssl-mode=ENCRYPTED_ONLY", "--edition=ENTERPRISE",
                 f"--network=projects/{PROJECT}/global/networks/keelson-vpc"):
        assert flag in sql.group(0), flag
    mem = re.search(r"^\+ gcloud memorystore instances create keelson-valkey .*$", out, re.M)
    assert mem
    for flag in ("--mode=cluster-disabled", f"--engine-version={pins['VALKEY_ENGINE_VERSION']}",
                 "--authorization-mode=auth-disabled", "--engine-configs=maxmemory-policy=noeviction",
                 "--persistence-config-mode=aof"):
        assert flag in mem.group(0), flag
    assert "--service-class=gcp-memorystore" in out
    assert "--enable-private-ip-google-access" in out and "routers nats create keelson-nat" in out
    assert f"artifacts repositories create keelson --repository-format=docker --location={REGION}" in out
    assert re.search(r"docker build --platform linux/amd64 -f \S+/infra/cloudrun/images/keycloak\.Dockerfile", out)
    assert "url-maps import keelson-urlmap --global" in out
    assert "backend-services update litellm-bs --global --iap=enabled" in out
    assert "--member=group:platform-admins@example.com --role=roles/iap.httpsResourceAccessor" in out
    assert "backend-services update keycloak-bs --global --security-policy=keelson-auth-edge" in out
    assert "--action=deny-403" in out
    assert "--load-balancing-scheme=EXTERNAL_MANAGED --address=keelson-ip --target-https-proxy=keelson-https-proxy --ports=443" in out
    assert re.search(r"ssl-certificates create keelson-cert-\d+ --global --domains=keelson\.demo\.example\.com"
                     r"\\,merxa\.demo\.example\.com\\,auth\.demo\.example\.com\\,gateway\.demo\.example\.com ", out)


def test_dry_run_order(dry: subprocess.CompletedProcess[str]) -> None:
    out = dry.stdout

    def pos(s: str) -> int:
        i = out.find(s)
        assert i >= 0, s
        return i

    assert pos("gcloud sql instances create") < pos("secrets create keelson-database-url")
    assert pos("jobs execute keelson-db-init-keelson-pg") < pos("services/keycloak.yaml")
    assert pos("services/keycloak.yaml") < pos("jobs execute keelson-kc-config")
    assert pos("services/litellm.yaml") < pos("jobs execute keelson-bootstrap")
    assert pos("services/merxa-api.yaml") < pos("jobs execute keelson-bootstrap")
    assert pos("jobs execute keelson-kc-config") < pos("jobs execute keelson-bootstrap") < pos("services/engine-api.yaml")
    assert pos("worker-pools replace") > pos("jobs execute keelson-bootstrap")
    assert pos("add-iam-policy-binding keelson-session-secret") < pos("services/engine-api.yaml")
    assert pos("services replace") < pos("network-endpoint-groups create")


@pytest.mark.parametrize("args,needle", [
    ((), "PROJECT is required"),
    ((PROJECT,), "REGION is required"),
    ((PROJECT, REGION), "DOMAIN is required"),
    ((PROJECT, REGION, "--domain", "keelson.localhost"), "localhost"),
    ((PROJECT, REGION, "--domain", "Bad_Domain"), "invalid domain"),
    (("Bad Project", REGION, "--domain", DOMAIN), "invalid project"),
    ((PROJECT, REGION, "--domain", DOMAIN, "--frobnicate"), "unknown option"),
])
def test_argument_validation(args: tuple[str, ...], needle: str, fakebin: tuple[Path, Path], tmp_path: Path) -> None:
    empty_env = tmp_path / "none.env"
    res = deploy(*args, "--dry-run", fakebin=fakebin, env_file=empty_env, check=False)
    assert res.returncode == 2 and needle in res.stderr, res.stderr


def test_help(fakebin: tuple[Path, Path]) -> None:
    res = deploy("--help", fakebin=fakebin)
    assert "--dry-run" in res.stdout and "--render-only" in res.stdout and "shellcheck" not in res.stdout


def test_apply_mode_requires_tools(fakebin: tuple[Path, Path], fake_env: tuple[Path, Path, dict[str, str]]) -> None:
    """Without --dry-run the script stops before any change when GEMINI_API_KEY is missing."""
    env_file, sd, _ = fake_env
    res = deploy(PROJECT, REGION, "--domain", DOMAIN, fakebin=fakebin, env_file=env_file, secrets_dir=sd,
                 extra_env={"GEMINI_API_KEY": "", "PROJECT_NUMBER": "123"}, check=False)
    assert res.returncode == 2 and "GEMINI_API_KEY" in res.stderr
    _, marker = fakebin
    assert not marker.exists()


# ───────────────────────────── manifests ─────────────────────────────
def test_manifest_set(rendered: dict[str, dict]) -> None:
    assert set(rendered) == {
        "services/engine-api.yaml", "services/keelson-web.yaml", "services/optimizer.yaml",
        "services/merxa-api.yaml", "services/merxa-web.yaml", "services/litellm.yaml", "services/keycloak.yaml",
        "workerpools/engine-worker.yaml", "jobs/keelson-bootstrap.yaml", "jobs/keelson-kc-config.yaml",
        "jobs/keelson-db-init-keelson-pg.yaml", "lb/url-map.yaml", "lb/http-redirect.yaml",
        # M6 Signal Hub (infra/cloudrun/signal-hub.sh): API service, bootstrap job, one poll job per adapter
        "signal-hub/signal-hub-api.yaml", "signal-hub/signal-hub-bootstrap.yaml",
        *(f"signal-hub/signal-hub-poll-{a}.yaml" for a in ("gdacs", "usgs", "rss", "extraction", "sweep")),
    }
    for name, doc in rendered.items():
        if name.startswith("services/"):
            assert doc["apiVersion"] == "serving.knative.dev/v1" and doc["kind"] == "Service"
            assert doc["metadata"]["name"] == Path(name).stem
            assert doc["metadata"]["labels"]["cloud.googleapis.com/location"] == REGION
    assert rendered["workerpools/engine-worker.yaml"]["kind"] == "WorkerPool"
    assert rendered["workerpools/engine-worker.yaml"]["apiVersion"] == "run.googleapis.com/v1"


def test_ingress(rendered: dict[str, dict]) -> None:
    ingress = {Path(n).stem: d["metadata"]["annotations"]["run.googleapis.com/ingress"]
               for n, d in rendered.items() if n.startswith("services/")}
    assert ingress.pop("optimizer") == "internal"
    # Nothing is reachable on run.app from the internet; LB-fronted services only via the ALB.
    assert set(ingress.values()) == {"internal-and-cloud-load-balancing"}
    assert set(ingress) <= LB_SERVICES


def test_ports_probes_and_resources(rendered: dict[str, dict]) -> None:
    want_port = {"litellm": 4000, "phoenix": 6006}
    for name, doc in rendered.items():
        if not name.startswith("services/"):
            continue
        svc = Path(name).stem
        c = containers(doc)[0]
        port = c["ports"][0]["containerPort"]
        assert port == want_port.get(svc, 8080), svc
        probe = c.get("startupProbe")
        assert probe, svc
        target = probe.get("httpGet") or probe.get("tcpSocket")
        assert target["port"] == port, svc
        assert "PORT" not in env_of(doc), f"{svc}: PORT is reserved on Cloud Run"
        assert c["resources"]["limits"]["memory"]


def test_keycloak_optimized_single_instance(rendered: dict[str, dict]) -> None:
    kc = rendered["services/keycloak.yaml"]
    ann = kc["spec"]["template"]["metadata"]["annotations"]
    assert ann["autoscaling.knative.dev/minScale"] == ann["autoscaling.knative.dev/maxScale"] == "1"
    assert ann["run.googleapis.com/cpu-throttling"] == "false"
    c = containers(kc)[0]
    assert c["args"] == ["start", "--optimized", "--cache=local"]
    # Keycloak answers 503 while bootstrapping: the probe must be HTTP, not TCP.
    assert c["startupProbe"]["httpGet"]["path"] == "/realms/master"
    env = env_of(kc)
    assert env["KC_HOSTNAME"]["value"] == f"https://auth.{DOMAIN}"
    assert env["KC_PROXY_HEADERS"]["value"] == "xforwarded"
    assert env["KC_DB_URL"]["value"].startswith("jdbc:postgresql://") and "sslmode=require" in env["KC_DB_URL"]["value"]
    df = (CR / "images" / "keycloak.Dockerfile").read_text()
    assert "kc.sh build" in df and 'CMD ["start", "--optimized", "--cache=local"]' in df


def test_public_origins_from_domain(rendered: dict[str, dict]) -> None:
    eng = env_of(rendered["services/engine-api.yaml"])
    assert eng["OIDC_ISSUER"]["value"] == f"https://auth.{DOMAIN}/realms/keelson"
    assert eng["MERXA_ISSUER"]["value"] == f"https://auth.{DOMAIN}/realms/merxa"
    assert eng["PUBLIC_URL"]["value"] == f"https://keelson.{DOMAIN}"  # https => Secure cookies
    assert eng["PUBLIC_BASE_DOMAIN"]["value"] == DOMAIN
    assert eng["MERXA_MCP_URL"]["value"] == f"https://merxa.{DOMAIN}/mcp"
    assert eng["KEYCLOAK_INTERNAL_URL"]["value"] == f"https://keycloak-000000000000.{REGION}.run.app"
    mx = env_of(rendered["services/merxa-api.yaml"])
    assert mx["OIDC_ISSUER"]["value"] == f"https://auth.{DOMAIN}/realms/merxa"
    assert mx["MCP_RESOURCE"]["value"] == f"https://merxa.{DOMAIN}/mcp"
    assert mx["TRUSTED_RECEIPT_ISSUERS"]["value"] == f"https://keelson.{DOMAIN}"
    assert mx["KEELSON_JWKS_URL"]["value"] == f"https://keelson.{DOMAIN}/api/.well-known/jwks.json"
    assert env_of(rendered["services/litellm.yaml"])["PROXY_BASE_URL"]["value"] == f"https://gateway.{DOMAIN}"
    text = yaml.safe_dump(rendered)
    assert "localhost" not in text


def test_engine_env_identical(rendered: dict[str, dict]) -> None:
    api = containers(rendered["services/engine-api.yaml"])[0]["env"]
    assert containers(rendered["workerpools/engine-worker.yaml"])[0]["env"] == api
    assert containers(rendered["jobs/keelson-bootstrap.yaml"])[0]["env"] == api
    names = {e["name"] for e in api}
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    compose_env = set(compose["services"]["engine-api"]["environment"])
    # Every compose engine variable is set on Cloud Run (files -> env; CONNECTOR_* explicitly).
    assert compose_env <= names, compose_env - names
    assert {"KEELSON_CLIENT_ASSERTION_KEY", "KEELSON_RECEIPT_KEY", "KEELSON_DATA_KEY"} <= names


def test_engine_commands(rendered: dict[str, dict]) -> None:
    assert "command" not in containers(rendered["services/engine-api.yaml"])[0]  # image CMD uses $PORT
    assert containers(rendered["workerpools/engine-worker.yaml"])[0]["command"] == ["python", "-m", "keelson_engine.worker"]
    assert containers(rendered["jobs/keelson-bootstrap.yaml"])[0]["command"] == ["python", "-m", "keelson_engine.bootstrap"]
    wp = rendered["workerpools/engine-worker.yaml"]
    assert wp["metadata"]["annotations"]["run.googleapis.com/manualInstanceCount"] == "1"


def test_vpc_egress(rendered: dict[str, dict]) -> None:
    def egress(n: str) -> str | None:
        return rendered[n]["spec"]["template"]["metadata"]["annotations"].get("run.googleapis.com/vpc-access-egress")
    # callers of internal-ingress services need all traffic through the VPC
    for n in ("services/engine-api.yaml", "workerpools/engine-worker.yaml", "jobs/keelson-bootstrap.yaml",
              "services/keelson-web.yaml", "services/merxa-web.yaml", "jobs/keelson-kc-config.yaml"):
        assert egress(n) == "all-traffic", n
    assert egress("services/optimizer.yaml") is None
    for n in ("services/merxa-api.yaml", "services/litellm.yaml", "services/keycloak.yaml"):
        assert egress(n) == "private-ranges-only", n


def test_merxa_api_single_instance_with_memory_volume(rendered: dict[str, dict]) -> None:
    d = rendered["services/merxa-api.yaml"]
    ann = d["spec"]["template"]["metadata"]["annotations"]
    assert ann["autoscaling.knative.dev/maxScale"] == "1"
    vols = {v["name"]: v for v in d["spec"]["template"]["spec"]["volumes"]}
    assert vols["snapshots"]["emptyDir"]["medium"] == "Memory"
    assert containers(d)[0]["volumeMounts"] == [{"name": "snapshots", "mountPath": "/data/snapshots"}]


def test_db_init_job(rendered: dict[str, dict]) -> None:
    job = rendered["jobs/keelson-db-init-keelson-pg.yaml"]
    env = env_of(job)
    assert env["DB_ROLES"]["value"] == "keelson adk keycloak litellm merxa"
    for role in ("KEELSON", "ADK", "KEYCLOAK", "LITELLM", "MERXA"):
        assert env[f"{role}_DB_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == secret_id(f"{role}_DB_PASSWORD")
    pins = dict(ln.split("=", 1) for ln in (CR / "versions.env").read_text().splitlines() if ln and not ln.startswith("#"))
    for k in ("PIN_POSTGIS", "PIN_VECTOR", "PIN_BTREE_GIST", "PIN_PGCRYPTO"):
        assert env[k]["value"] == pins[k]
    assert env["PGSSLMODE"]["value"] == "require"


def test_sql_isolation(tmp_path: Path, fakebin: tuple[Path, Path]) -> None:
    docs = render(tmp_path, fakebin, "--sql-isolation")
    roles = {n: env_of(d)["DB_ROLES"]["value"] for n, d in docs.items() if "db-init" in n}
    assert roles == {
        "jobs/keelson-db-init-keelson-pg.yaml": "keelson adk keycloak",
        "jobs/keelson-db-init-keelson-pg-litellm.yaml": "litellm",
        "jobs/keelson-db-init-merxa-pg.yaml": "merxa",
    }


def test_url_map(rendered: dict[str, dict]) -> None:
    um = rendered["lb/url-map.yaml"]
    bs = f"projects/{PROJECT}/global/backendServices/"
    hosts = {h["hosts"][0]: h["pathMatcher"] for h in um["hostRules"]}
    assert hosts == {f"keelson.{DOMAIN}": "keelson", f"merxa.{DOMAIN}": "merxa", f"auth.{DOMAIN}": "auth",
                     f"gateway.{DOMAIN}": "gateway"}
    pm = {m["name"]: m for m in um["pathMatchers"]}
    assert pm["keelson"]["defaultService"] == bs + "keelson-web-bs"
    api = pm["keelson"]["pathRules"][0]
    assert api["paths"] == ["/api", "/api/*"] and api["service"] == bs + "engine-api-bs"  # the /api bypass
    assert pm["merxa"]["defaultService"] == bs + "merxa-web-bs"
    assert set(pm["merxa"]["pathRules"][0]["paths"]) == {"/v1/*", "/mcp", "/mcp/*", "/mcp-lite", "/mcp-lite/*", "/.well-known/*"}
    assert pm["merxa"]["pathRules"][0]["service"] == bs + "merxa-api-bs"
    assert pm["auth"]["defaultService"] == bs + "keycloak-bs"
    assert pm["gateway"]["defaultService"] == bs + "litellm-bs"
    assert um["defaultUrlRedirect"]["hostRedirect"] == f"keelson.{DOMAIN}"
    # URL map path rules allow '*' only at the end after '/'
    for m in um["pathMatchers"]:
        for r in m.get("pathRules", []):
            for p in r["paths"]:
                assert p.startswith("/") and ("*" not in p or p.endswith("/*")), p
    assert rendered["lb/http-redirect.yaml"]["defaultUrlRedirect"]["httpsRedirect"] is True


def test_phoenix_optional(tmp_path: Path, fakebin: tuple[Path, Path]) -> None:
    docs = render(tmp_path, fakebin, "--enable-phoenix")
    assert "services/phoenix.yaml" in docs
    hosts = [h["hosts"][0] for h in docs["lb/url-map.yaml"]["hostRules"]]
    assert f"traces.{DOMAIN}" in hosts
    assert "phoenix" in env_of(docs["jobs/keelson-db-init-keelson-pg.yaml"])["DB_ROLES"]["value"].split()


def test_caddy_routes_covered() -> None:
    """Every host Caddy serves has a URL map host rule (local-only profile hosts excepted)."""
    caddy = (ROOT / "infra" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    caddy_hosts = {h.split(".")[0] for h in re.findall(r"^http://([a-z]+)\.localhost", caddy, re.M)}
    um = (CR / "lb" / "url-map.yaml").read_text(encoding="utf-8")
    um_hosts = set(re.findall(r"hosts: \[([a-z]+)\.\$\{DOMAIN\}\]", um))
    assert caddy_hosts - LOCAL_ONLY_HOSTS == um_hosts


# ───────────────────────────── overlay images ─────────────────────────────
REWRITE = CR / "images" / "rewrite-hosts.sh"


def test_rewrite_hosts_realms_and_tenants(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "infra" / "keycloak" / "config", tmp_path / "config")
    shutil.copy(ROOT / "infra" / "tenants.yaml", tmp_path / "tenants.yaml")
    shutil.copytree(ROOT / "data" / "packs", tmp_path / "packs")
    res = subprocess.run(["sh", str(REWRITE), DOMAIN, str(tmp_path / "config"), str(tmp_path / "tenants.yaml"),
                          str(tmp_path / "packs")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    for p in tmp_path.rglob("*.yaml"):
        text = p.read_text(encoding="utf-8")
        assert ".localhost" not in text, p
        yaml.safe_load(re.sub(r"\$\(env:[A-Z_]+\)", "x", text))
    keelson = (tmp_path / "config" / "keelson.yaml").read_text(encoding="utf-8")
    assert f"redirectUris: [https://keelson.{DOMAIN}/api/auth/callback]" in keelson
    merxa = (tmp_path / "config" / "merxa.yaml").read_text(encoding="utf-8")
    assert f"jwks.url: https://keelson.{DOMAIN}/api/.well-known/jwks.json" in merxa
    assert f"included.custom.audience: https://merxa.{DOMAIN}/mcp" in merxa
    tenants = (tmp_path / "tenants.yaml").read_text(encoding="utf-8")
    assert f"mcp_url: https://merxa.{DOMAIN}/mcp" in tenants and f"issuer: https://auth.{DOMAIN}/realms/merxa" in tenants


@pytest.mark.parametrize("domain", ["", "localhost", "a.localhost", "UPPER.example.com", "x;rm -rf /", "nodot"])
def test_rewrite_hosts_rejects_bad_domains(domain: str, tmp_path: Path) -> None:
    f = tmp_path / "t.yaml"
    f.write_text("url: http://merxa.localhost/mcp\n", encoding="utf-8")
    res = subprocess.run(["sh", str(REWRITE), domain, str(f)], capture_output=True, text=True)
    assert res.returncode == 2
    assert f.read_text(encoding="utf-8") == "url: http://merxa.localhost/mcp\n"


def test_rewrite_hosts_fails_on_leftover(tmp_path: Path) -> None:
    f = tmp_path / "t.yaml"
    f.write_text("url: http://other.localhost/x\n", encoding="utf-8")
    res = subprocess.run(["sh", str(REWRITE), DOMAIN, str(f)], capture_output=True, text=True)
    assert res.returncode == 1 and "other.localhost" in res.stderr


def test_engine_overlay_bakes_compose_mounts() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    df = (CR / "images" / "engine.Dockerfile").read_text(encoding="utf-8")
    for svc in ("bootstrap", "engine-api", "engine-worker"):
        for vol in compose["services"][svc].get("volumes", []):
            target = vol.split(":")[1]
            assert f" {target}" in df, f"{svc} mounts {target}; the Cloud Run engine image must contain it"
    litellm = (CR / "images" / "litellm.Dockerfile").read_text(encoding="utf-8")
    assert "COPY infra/litellm/config.yaml /app/config.yaml" in litellm
    kcc = (CR / "images" / "kc-config.Dockerfile").read_text(encoding="utf-8")
    assert "/config" in kcc and "rewrite-hosts" in kcc


def test_kc_config_job_matches_compose(rendered: dict[str, dict]) -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    want = set(compose["services"]["kc-config"]["environment"]) - {"KEYCLOAK_URL"}
    env = env_of(rendered["jobs/keelson-kc-config.yaml"])
    assert want <= set(env), want - set(env)
    assert env["KEYCLOAK_URL"]["value"] == f"https://keycloak-000000000000.{REGION}.run.app"


# ───────────────────────────── apply mode against fake tools ─────────────────────────────
FAKE_GCLOUD = r"""#!/bin/sh
# Fake gcloud: records every call, pretends nothing exists yet, answers the few queries
# deploy.sh makes, and (like real tools may) drains stdin, which would break `while read`
# loops whose commands do not detach stdin.
echo "gcloud $*" >> "$FAKE_DIR/calls.log"
case "$*" in
  "secrets versions add "*) cat > "$FAKE_DIR/secret_$4"; exit 0 ;;
esac
cat > /dev/null
case "$*" in
  "projects describe "*) echo 123456789012 ;;
  "secrets versions access"*) exit 1 ;;
  "sql instances describe "*--format=json*) echo '{"ipAddresses":[{"type":"PRIVATE","ipAddress":"10.9.0.5"}]}' ;;
  "memorystore instances describe "*--format=json*) echo '{"endpoints":[{"connections":[{"pscAutoConnection":{"connectionType":"CONNECTION_TYPE_PRIMARY","ipAddress":"10.20.1.9"}}]}]}' ;;
  "compute addresses describe keelson-ip"*) echo 203.0.113.7 ;;
  "services vpc-peerings list"*) : ;;
  "logging sinks describe"*) echo '{}' ;;
  "compute security-policies rules describe 1000 "*) [ -n "${FAKE_RULE_EXISTS:-}" ] && exit 0; exit 1 ;;
  *" describe "*) exit 1 ;;
esac
exit 0
"""
FAKE_DOCKER = r"""#!/bin/sh
echo "docker $*" >> "$FAKE_DIR/calls.log"
if [ "$1" = inspect ]; then
  for last; do :; done
  echo "${last%:*}@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
fi
exit 0
"""
FAKE_JQ = r"""#!/bin/sh
in="$(cat)"
case "$in" in
  *ipAddresses*) echo 10.9.0.5 ;;
  *endpoints*) echo 10.20.1.9 ;;
esac
"""


def apply_with_fakes(tmp_path: Path, env_file: Path, sd: Path, values: dict[str, str],
                     extra_env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], Path, dict[str, str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("gcloud", FAKE_GCLOUD), ("docker", FAKE_DOCKER), ("jq", FAKE_JQ)):
        p = bindir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in {"PROJECT", "REGION", "DOMAIN", "DRY_RUN"}}
    env.update(PATH=f"{bindir}:{env['PATH']}", FAKE_DIR=str(tmp_path), TMPDIR=str(tmp_path),
               GEMINI_API_KEY="gemini-" + secrets.token_hex(8))
    env.update(extra_env or {})
    values = dict(values, GEMINI_API_KEY=env["GEMINI_API_KEY"])
    res = subprocess.run(["bash", str(DEPLOY), PROJECT, REGION, "--domain", DOMAIN, "--tag", "t9",
                          "--iap-members", "group:ops@example.com", "--env-file", str(env_file),
                          "--secrets-dir", str(sd)],
                         capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=300)
    return res, tmp_path, values


@pytest.fixture()
def applied(tmp_path: Path, fake_env: tuple[Path, Path, dict[str, str]]) -> tuple[subprocess.CompletedProcess[str], Path, dict[str, str]]:
    env_file, sd, values = fake_env
    return apply_with_fakes(tmp_path, env_file, sd, values)


def test_apply_mode_with_fake_tools(applied: tuple[subprocess.CompletedProcess[str], Path, dict[str, str]]) -> None:
    res, tmp, values = applied
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-3000:]
    out = res.stdout + res.stderr
    assert not [k for k, v in values.items() if v in out], "a secret value reached the log"
    calls = (tmp / "calls.log").read_text(encoding="utf-8")
    # every value went to Secret Manager on stdin, DSNs built from the queried private IPs
    def secret(sid: str) -> str:
        return (tmp / f"secret_{sid}").read_text(encoding="utf-8")
    assert secret("keelson-session-secret") == values["SESSION_SECRET"]
    assert secret("keelson-data-key") == values["keelson_data_key"]
    assert secret("keelson-gemini-api-key") == values["GEMINI_API_KEY"]
    assert secret("keelson-database-url") == \
        f"postgresql+asyncpg://keelson:{values['KEELSON_DB_PASSWORD']}@10.9.0.5:5432/keelson?ssl=require"
    assert secret("keelson-litellm-database-url") == \
        f"postgresql://litellm:{values['LITELLM_DB_PASSWORD']}@10.9.0.5:5432/litellm?sslmode=require"
    assert secret("keelson-valkey-url") == "redis://10.20.1.9:6379/0"
    for k in bash_array("ENV_SECRETS"):
        assert secret(secret_id(k)) == values[k], k
    # secretAccessor grants: one per (secret, service account) pair of the manifests; a command
    # that drained the loop's stdin would have cut this list short
    grants = re.findall(r"^gcloud secrets add-iam-policy-binding (\S+) --member=serviceAccount:(\S+) ", calls, re.M)
    work = Path(re.search(r"^# rendered manifests: (\S+)", res.stdout, re.M).group(1))
    expected = set()
    manifests = [*work.glob("services/*.yaml"), *work.glob("workerpools/*.yaml"), *work.glob("jobs/*.yaml"),
                 *work.glob("signal-hub/*.yaml")]  # M6: signal_hub_deploy grants its own manifests
    for p in manifests:
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        for c in containers(d):
            for e in c.get("env", []):
                if "valueFrom" in e:
                    expected.add((e["valueFrom"]["secretKeyRef"]["name"], pod_spec(d)["serviceAccountName"]))
    assert set(grants) == expected and len(grants) == len(expected)
    # deployed by digest, URLs from the real project number, private IPs rendered
    eng = yaml.safe_load((work / "services" / "engine-api.yaml").read_text(encoding="utf-8"))
    assert containers(eng)[0]["image"].endswith("@sha256:" + "0123456789abcdef" * 4)
    assert env_of(eng)["LITELLM_BASE_URL"]["value"] == f"https://litellm-123456789012.{REGION}.run.app"
    kc = yaml.safe_load((work / "services" / "keycloak.yaml").read_text(encoding="utf-8"))
    assert env_of(kc)["KC_DB_URL"]["value"] == "jdbc:postgresql://10.9.0.5:5432/keycloak?sslmode=require"
    job = yaml.safe_load((work / "jobs" / "keelson-db-init-keelson-pg.yaml").read_text(encoding="utf-8"))
    assert env_of(job)["PGHOST"]["value"] == "10.9.0.5"
    for step in ("run jobs execute keelson-db-init-keelson-pg", "run jobs execute keelson-kc-config",
                 "run jobs execute keelson-bootstrap", "run worker-pools replace",
                 "compute url-maps import keelson-urlmap", "iap web add-iam-policy-binding"):
        assert step in calls, step
    assert "203.0.113.7" in res.stdout  # DNS instructions


# ───────────────────────────── review regressions (CR review) ─────────────────────────────
def test_dsn_passwords_are_percent_encoded(tmp_path: Path, fake_env: tuple[Path, Path, dict[str, str]]) -> None:
    """A DB password with URL delimiters (@ / : ? # % space +) must not break the derived DSNs."""
    from urllib.parse import unquote, urlsplit

    env_file, sd, values = fake_env
    nasty = {"KEELSON_DB_PASSWORD": "p@ss/w:rd?x#y%z +q", "LITELLM_DB_PASSWORD": r"l1t%41&b=c;d'e\f$h",
             "MERXA_DB_PASSWORD": "m@m"}
    text = env_file.read_text(encoding="utf-8")
    for k, v in nasty.items():
        text = re.sub(rf"^{k}=.*$", lambda _m, k=k, v=v: f"{k}={v}", text, flags=re.M)
    own_env = tmp_path / "nasty.env"
    own_env.write_text(text, encoding="utf-8")
    res, tmp, _ = apply_with_fakes(tmp_path, own_env, sd, dict(values, **nasty))
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-3000:]
    for sid, role in (("keelson-database-url", "KEELSON"), ("keelson-litellm-database-url", "LITELLM"),
                      ("keelson-merxa-database-url", "MERXA")):
        url = (tmp / f"secret_{sid}").read_text(encoding="utf-8")
        parts = urlsplit(url)
        assert parts.hostname == "10.9.0.5" and parts.port == 5432, url
        assert parts.username == role.lower()
        assert unquote(parts.password or "") == nasty[f"{role}_DB_PASSWORD"], url
    # the role password secrets themselves stay raw (db-init sets them with %L)
    assert (tmp / "secret_keelson-db-password").read_text(encoding="utf-8") == nasty["KEELSON_DB_PASSWORD"]


def _kc_edge_expression() -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    block = re.search(r"^KC_EDGE_PATH=.*?^KC_EDGE_EXPRESSION=.*?$", text, re.S | re.M)
    assert block, "KC_EDGE_PATH / KC_EDGE_EXPRESSION not found in deploy.sh"
    res = subprocess.run(["bash", "-c", block.group(0) + '\nprintf %s "$KC_EDGE_EXPRESSION"'],
                         capture_output=True, text=True, check=True)
    return res.stdout


def _url_decode_uni(path: str) -> str:
    """Cloud Armor urlDecodeUni(): %## -> byte, %u#### -> code point, '+' -> space."""
    from urllib.parse import unquote_to_bytes

    path = re.sub(r"%u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), path)
    return unquote_to_bytes(path.replace("+", " ")).decode("utf-8", "replace")


def _kc_edge_blocks(expr: str, raw_path: str) -> bool:
    terms = expr.split(" || ")
    assert len(terms) >= 2
    hit = False
    for t in terms:
        m = re.fullmatch(r"request\.path\.urlDecodeUni\(\)\.lower\(\)\.matches\('(.*)'\)", t)
        assert m, t
        assert "\\" not in m.group(1), "keep the RE2 pattern free of CEL string escapes"
        hit |= re.search(m.group(1), _url_decode_uni(raw_path).lower()) is not None
    return hit


@pytest.mark.parametrize("path", [
    "/admin", "/admin/", "/admin/master/console/", "/ADMIN/realms/keelson/users", "//admin/x",
    "/admin;jsessionid=1/realms", "/%61dmin/realms/keelson", "/%41DMIN/realms",
    "/realms/master", "/realms/master/", "/realms/master/protocol/openid-connect/token",
    "/realms//master/account", "/Realms/MASTER/account", "/realms/%6daster/account",
    "/realms%2fmaster/account", "/realms;x=1/master/account", "/realms/master;x=1/account",
    "/realms/./master/account", "/realms/keelson/../master/account", "/x/../admin/realms",
    "/realms/%2e%2e/admin", "/%2e/admin", "/realms/%u006daster/account",
])
def test_kc_edge_rule_blocks_admin_and_master(path: str) -> None:
    assert _kc_edge_blocks(_kc_edge_expression(), path), path


@pytest.mark.parametrize("path", [
    "/realms/keelson", "/realms/keelson/.well-known/openid-configuration",
    "/realms/merxa/protocol/openid-connect/auth", "/realms/keelson/protocol/openid-connect/token",
    "/realms/keelson/login-actions/authenticate", "/realms/keelson/account",
    "/resources/abc12/login/keycloak.v2/css/login.css", "/realms/masterclass/x",
    "/realms/keelson/administrator", "/robots.txt", "/",
])
def test_kc_edge_rule_allows_public_realm_paths(path: str) -> None:
    assert not _kc_edge_blocks(_kc_edge_expression(), path), path


def test_kc_edge_rule_created_then_updated(tmp_path: Path, fake_env: tuple[Path, Path, dict[str, str]],
                                           dry: subprocess.CompletedProcess[str]) -> None:
    """A deploy against an existing rule 1000 must replace its expression (not skip it)."""
    assert "rules create 1000 --security-policy=keelson-auth-edge" in dry.stdout
    assert "urlDecodeUni" in dry.stdout
    env_file, sd, values = fake_env
    res, tmp, _ = apply_with_fakes(tmp_path, env_file, sd, values, {"FAKE_RULE_EXISTS": "1"})
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-3000:]
    calls = (tmp / "calls.log").read_text(encoding="utf-8")
    line = re.search(r"^gcloud compute security-policies rules update 1000 .*$", calls, re.M)
    assert line and "--expression=" + _kc_edge_expression() in line.group(0)
    assert "security-policies rules create 1000" not in calls
