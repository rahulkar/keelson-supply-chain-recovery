"""GitHub workflow structure (.github/): SHA-pinned actions, least privilege, no injection, release triggers,
matrices from images.json, the signing identity, and the Grype exception policy."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import ROOT

import matrix
import registry as reg

WORKFLOWS = {p.name: p for p in sorted((ROOT / ".github" / "workflows").glob("*.yml"))}
ACTION = ROOT / ".github" / "actions" / "sign-attest" / "action.yml"
SIGNING = dict(ln.split("=", 1) for ln in (ROOT / "infra" / "supply-chain" / "signing.env").read_text(encoding="utf-8").splitlines()
               if ln and not ln.startswith("#"))


def load(p: Path) -> dict[str, Any]:
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    if True in doc:  # YAML 1.1 reads the key `on` as a boolean
        doc["on"] = doc.pop(True)
    return doc


DOCS = {name: load(p) for name, p in WORKFLOWS.items()}


def steps(doc: dict[str, Any]) -> list[dict[str, Any]]:
    if "runs" in doc:
        return doc["runs"]["steps"]
    return [s for job in doc["jobs"].values() for s in job.get("steps", [])]


def all_files() -> dict[str, dict[str, Any]]:
    return {**DOCS, "sign-attest/action.yml": load(ACTION)}


def test_workflows_exist() -> None:
    assert set(WORKFLOWS) == {"ci.yml", "release.yml"}


@pytest.mark.parametrize("name", ["ci.yml", "release.yml", "sign-attest/action.yml"])
def test_uses_are_sha_pinned_with_a_version_comment(name: str) -> None:
    path = ACTION if name.startswith("sign-attest") else WORKFLOWS[name]
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", line)
        if not m:
            continue
        target, rest = m.groups()
        if target.startswith("./"):
            assert target == "./.github/actions/sign-attest", target
            continue
        assert re.fullmatch(r"[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}", target), line
        assert re.search(r"#\s*v\d+\.\d+\.\d+", rest), line


def test_top_level_permissions_are_empty_and_jobs_least_privilege() -> None:
    for name, doc in DOCS.items():
        assert doc["permissions"] == {}, name
        for job_id, job in doc["jobs"].items():
            perms = job.get("permissions")
            assert isinstance(perms, dict) and perms.get("contents") in ("read", "write"), (name, job_id)
            writes = {k for k, v in perms.items() if v == "write"}
            if name == "ci.yml":
                assert writes <= {"security-events"}, (job_id, writes)
            if "id-token" in writes:
                assert name == "release.yml" and job_id in {"build-sign", "overlays", "manifest"}, (name, job_id)
            if "contents" in writes:
                assert (name, job_id) == ("release.yml", "publish")
            if "packages" in writes:
                assert name == "release.yml" and job_id in {"build-sign", "overlays"}, (name, job_id)


def test_no_dangerous_triggers() -> None:
    for name, doc in DOCS.items():
        triggers = doc["on"] if isinstance(doc["on"], dict) else {t: None for t in doc["on"]}
        assert not {"pull_request_target", "workflow_run", "issue_comment"} & set(triggers), name


def test_release_triggers_only_on_main_and_semver_tags() -> None:
    on = DOCS["release.yml"]["on"]
    assert set(on) == {"push"}
    assert on["push"] == {"branches": ["main"], "tags": ["v[0-9]+.[0-9]+.[0-9]+"]}
    assert DOCS["release.yml"]["jobs"]["setup"]["if"] == (
        f"github.repository == '{SIGNING['SIGNER_REPOSITORY']}' && vars.RELEASE_ENABLED == 'true'")
    # every other job hangs off setup, so the opt-in gates the whole release
    for name, job in DOCS["release.yml"]["jobs"].items():
        if name != "setup":
            needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
            assert "setup" in needs or needs == ["manifest"], name
    assert DOCS["release.yml"]["concurrency"]["cancel-in-progress"] is False


def test_no_expression_interpolation_in_scripts() -> None:
    """Values reach run: scripts through env only (no template injection)."""
    for name, doc in all_files().items():
        for s in steps(doc):
            if "run" in s:
                assert "${{" not in s["run"], (name, s.get("name"), s["run"][:120])


def test_checkouts_never_persist_credentials() -> None:
    for name, doc in DOCS.items():
        for s in steps(doc):
            if s.get("uses", "").startswith("actions/checkout@"):
                assert s.get("with", {}).get("persist-credentials") is False, (name, s)


def test_release_images_are_single_manifests_built_without_a_shared_cache() -> None:
    build = [s for s in steps(load(ACTION)) if s.get("uses", "").startswith("docker/build-push-action@")]
    assert len(build) == 1
    w = build[0]["with"]
    assert w["provenance"] is False and w["sbom"] is False and w["no-cache"] is True and w["push"] is True
    assert w["platforms"] == "linux/amd64" and "cache-from" not in w and "cache-to" not in w
    text = ACTION.read_text(encoding="utf-8")
    assert 'cosign sign --yes "${annotations[@]}" "$REF"' in text
    assert 'cosign attest --yes --type spdxjson --predicate sbom.spdx.json "$REF"' in text
    assert "push-to-registry: true" in text and "fail-build: true" in text and "severity-cutoff: critical" in text


def test_ci_matrices_come_from_the_registry() -> None:
    ci = DOCS["ci.yml"]["jobs"]
    for job, key in (("python", "python"), ("web", "web"), ("images", "images")):
        assert ci[job]["strategy"]["matrix"]["include"] == f"${{{{ fromJSON(needs.setup.outputs.{key}) }}}}"
    rel = DOCS["release.yml"]["jobs"]
    assert rel["build-sign"]["strategy"]["matrix"]["include"] == "${{ fromJSON(needs.setup.outputs.images) }}"
    assert rel["overlays"]["strategy"]["matrix"]["include"] == "${{ fromJSON(needs.setup.outputs.overlays) }}"
    m = matrix.matrices(reg.load(ROOT))
    covered = {r["suite"] for r in m["python"]} | {r["suite"] for r in m["web"]}
    assert covered == {s.id for s in reg.load(ROOT).suites}
    assert {r["name"] for r in m["images"]} | {r["name"] for r in m["overlays"]} == {i.name for i in reg.load(ROOT).release_images}
    for row in m["python"] + m["web"]:
        json.dumps(row)


@pytest.mark.parametrize("identity,ok", [
    ("https://github.com/rahulkar/keelson-supply-chain-recovery/.github/workflows/release.yml@refs/heads/main", True),
    ("https://github.com/rahulkar/keelson-supply-chain-recovery/.github/workflows/release.yml@refs/tags/v1.2.3", True),
    ("https://github.com/rahulkar/keelson-supply-chain-recovery/.github/workflows/release.yml@refs/tags/v10.20.30", True),
    ("https://github.com/rahulkar/keelson-supply-chain-recovery/.github/workflows/release.yml@refs/heads/feature", False),
    ("https://github.com/rahulkar/keelson-supply-chain-recovery/.github/workflows/release.yml@refs/heads/main.evil", False),
    ("https://github.com/rahulkar/keelson-supply-chain-recovery/.github/workflows/release.yml@refs/pull/7/merge", False),
    ("https://github.com/rahulkar/keelson-supply-chain-recovery/.github/workflows/ci.yml@refs/heads/main", False),
    ("https://github.com/rahulkar/keelson-supply-chain-recovery/.github/workflows/release.yml@refs/tags/v1.2.3-rc1", False),
    ("https://github.com/rahulkar/keelson-supply-chain-recovery-fork/.github/workflows/release.yml@refs/heads/main", False),
    ("https://github.com/attacker/keelson-supply-chain-recovery/.github/workflows/release.yml@refs/heads/main", False),
    ("https://github.com/rahulkar/keelson-supply-chain-recoveryX.github/workflows/release.yml@refs/heads/main", False),
    ("https://github.com/rahulkar/keelson-supply-chain-recovery/.github/workflows/releaseXyml@refs/heads/main", False),
])
def test_signer_identity_regexp(identity: str, ok: bool) -> None:
    rx = SIGNING["SIGNER_IDENTITY_REGEXP"]
    assert rx.startswith("^") and rx.endswith("$")
    assert (re.search(rx, identity) is not None) is ok


def test_signing_policy_matches_the_workflows() -> None:
    assert SIGNING["SIGNER_MODE"] == "keyless" and SIGNING["SIGNER_KEY"] == ""
    assert SIGNING["SIGNER_OIDC_ISSUER"] == "https://token.actions.githubusercontent.com"
    assert SIGNING["SIGNER_TRIGGER"] == "push" and SIGNING["REQUIRE_PROVENANCE"] == "1"
    assert SIGNING["RELEASE_REGISTRY"].startswith("ghcr.io/")
    assert re.fullmatch(r"ghcr\.io/sigstore/cosign/cosign:v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", SIGNING["COSIGN_IMAGE"])
    installers = [s for doc in all_files().values() for s in steps(doc)
                  if s.get("uses", "").startswith("sigstore/cosign-installer@")]
    want = SIGNING["COSIGN_IMAGE"].split(":")[1].split("@")[0]
    assert installers and all(s["with"]["cosign-release"] == want for s in installers)


def test_grype_exceptions_have_a_reason_and_a_future_expiry() -> None:
    cfg = yaml.safe_load((ROOT / ".grype.yaml").read_text(encoding="utf-8"))
    assert isinstance(cfg.get("ignore"), list)
    today = dt.date.today()
    for rule in cfg["ignore"]:
        reason = rule.get("reason", "")
        m = re.search(r"expires (\d{4}-\d{2}-\d{2})", reason)
        assert rule.get("vulnerability") and m and "accepted by" in reason, rule
        assert dt.date.fromisoformat(m.group(1)) >= today, f"expired Grype exception: {rule}"


def test_codeowners_cover_the_supply_chain_controls() -> None:
    owners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    for path in ("/.github/", "/renovate.json", "/infra/supply-chain/", "/scripts/supply-chain/", "/scripts/cloudrun/",
                 "/infra/cloudrun/versions.env", "/models.lock"):
        assert re.search(rf"^{re.escape(path)}\s+@\w", owners, re.M), path
