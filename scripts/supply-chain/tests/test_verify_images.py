"""verify-images.sh / lib/verify.sh with a fake cosign: refs refused or verified with the pinned identity,
SBOM + provenance attestations, release files, key mode, the compose upstream check and the exit code."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT, fake_cosign

import registry as reg

SCRIPT = ROOT / "scripts" / "supply-chain" / "verify-images.sh"
SIGNING = ROOT / "infra" / "supply-chain" / "signing.env"
GHCR = "ghcr.io/rahulkar/keelson"
SHA = "1f" * 20
IDENTITY = ["--certificate-oidc-issuer", "https://token.actions.githubusercontent.com",
            "--certificate-identity-regexp",
            r"^https://github\.com/rahulkar/keelson-supply-chain-recovery/\.github/workflows/release\.yml"
            r"@refs/(heads/main|tags/v[0-9]+\.[0-9]+\.[0-9]+)$",
            "--certificate-github-workflow-repository", "rahulkar/keelson-supply-chain-recovery",
            "--certificate-github-workflow-trigger", "push"]


def digest(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


def run(tmp: Path, *args: str, fail: str = "", env: dict[str, str] | None = None) -> tuple[int, list[dict], list[str], str]:
    binary, log = fake_cosign(tmp / "fake")
    e = {k: v for k, v in os.environ.items() if k != "COSIGN_BIN"}
    e.update(COSIGN_BIN=str(binary), FAKE_COSIGN_LOG=str(log), FAKE_COSIGN_FAIL=fail,
             GEMINI_API_KEY="gemini-secret-value-123", **(env or {}))
    res = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=e, timeout=120)
    lines = [json.loads(x) for x in res.stdout.splitlines() if x.startswith("{")]
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert "gemini-secret-value-123" not in res.stdout + res.stderr
    return res.returncode, lines, calls, res.stderr


def write_release(d: Path, *, domain: str = "demo.example.com", lines: list[str] | None = None) -> Path:
    images = [i for i in reg.load(ROOT).release_images if domain or not i.domain_overlay]
    body = lines if lines is not None else [
        "RELEASE_SCHEMA=keelson.release/1", "RELEASE_REPOSITORY=rahulkar/keelson-supply-chain-recovery",
        f"RELEASE_GIT_SHA={SHA}", "RELEASE_REF=refs/tags/v1.2.3", "RELEASE_RUN_ID=7",
        "RELEASE_CREATED_AT=2026-10-10T08:00:00Z", f"RELEASE_DOMAIN={domain}",
        *(f"RELEASE_IMAGE_{i.release_key}={GHCR}/{i.name}@{digest(i.name)}" for i in images)]
    d.mkdir(parents=True, exist_ok=True)
    (d / "keelson-release.env").write_text("\n".join(body) + "\n", encoding="utf-8")
    (d / "keelson-release.env.sigstore.json").write_text("{}", encoding="utf-8")
    return d / "keelson-release.env"


def test_tag_refs_are_refused_without_calling_cosign(tmp_path: Path) -> None:
    rc, lines, calls, _ = run(tmp_path, f"{GHCR}/engine:v1.2.3", "ghcr.io/x/y")
    assert rc == 2 and calls == []
    assert [x["status"] for x in lines] == ["FAIL", "FAIL"]
    assert all(x["signature"] == "refused" and "not a digest reference" in x["detail"] for x in lines)


def test_digest_ref_is_verified_with_the_pinned_identity(tmp_path: Path) -> None:
    ref = f"{GHCR}/optimizer@{digest('optimizer')}"
    rc, lines, calls, _ = run(tmp_path, "-a", f"git_sha={SHA}", ref)
    assert rc == 0, lines
    assert lines == [{"image": ref, "digest": digest("optimizer"), "signature": "pass", "sbom": "pass",
                      "provenance": "pass", "status": "PASS", "detail": ref}]
    ident = " ".join(IDENTITY)
    assert calls == [f"verify {ident} -a git_sha={SHA} {ref}",
                     f"verify-attestation {ident} --type spdxjson {ref}",
                     f"verify-attestation {ident} --type slsaprovenance1 {ref}"]


def test_provenance_fallback_pins_the_release_identity(tmp_path: Path) -> None:
    """When cosign cannot read the attest-build-provenance bundle, `gh attestation verify` must enforce the
    same certificate identity (repository, release.yml AND the main/vX.Y.Z ref), not just the workflow path."""
    ghbin = tmp_path / "ghbin"
    ghbin.mkdir()
    (ghbin / "gh").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_GH_LOG"\nexit "${FAKE_GH_RC:-0}"\n',
                              encoding="utf-8")
    (ghbin / "gh").chmod(0o755)
    log = tmp_path / "gh.log"
    env = {"PATH": f"{ghbin}:{os.environ['PATH']}", "FAKE_GH_LOG": str(log)}
    ref = f"{GHCR}/optimizer@{digest('optimizer')}"
    rc, lines, _, _ = run(tmp_path / "ok", ref, fail="slsaprovenance1", env=env)
    assert rc == 0 and lines[0]["provenance"] == "pass", lines
    args = log.read_text(encoding="utf-8").split()
    assert args[:3] == ["attestation", "verify", f"oci://{ref}"]
    assert args[args.index("--repo") + 1] == "rahulkar/keelson-supply-chain-recovery"
    assert args[args.index("--cert-identity-regex") + 1] == IDENTITY[3]
    assert args[args.index("--cert-oidc-issuer") + 1] == IDENTITY[1]
    assert args[args.index("--predicate-type") + 1] == "https://slsa.dev/provenance/v1"
    assert "--bundle-from-oci" in args and "--signer-workflow" not in args
    rc, lines, _, _ = run(tmp_path / "bad", ref, fail="slsaprovenance1", env={**env, "FAKE_GH_RC": "1"})
    assert rc == 1 and lines[0]["provenance"] == "fail"


@pytest.mark.parametrize("fail,field", [(f"{GHCR}/b@", "signature"), ("spdxjson", "sbom"), ("slsaprovenance1", "provenance")])
def test_failures_are_counted(tmp_path: Path, fail: str, field: str) -> None:
    refs = [f"{GHCR}/{n}@{digest(n)}" for n in ("a", "b", "c")]
    rc, lines, _, err = run(tmp_path, *refs, fail=fail)
    failed = [x for x in lines if x["status"] == "FAIL"]
    assert rc == len(failed) == (1 if field == "signature" else 3)
    assert all(x[field] == "fail" for x in failed)
    assert f"verify-images: {rc} failed" in err


def test_release_file(tmp_path: Path) -> None:
    rel = write_release(tmp_path / "r")
    rc, lines, calls, _ = run(tmp_path, "--release", str(rel))
    assert rc == 0, lines
    assert lines[0]["image"] == "keelson-release.env" and lines[0]["status"] == "PASS"
    assert calls[0].startswith("verify-blob ") and f"--bundle {rel}.sigstore.json {rel}" in calls[0]
    images = {i.release_key: i for i in reg.load(ROOT).release_images}
    assert {x["image"] for x in lines[1:]} == set(images)
    for img in images.values():
        ref = f"{GHCR}/{img.name}@{digest(img.name)}"
        sig = next(c for c in calls if c.startswith("verify ") and c.endswith(ref))
        assert f"-a git_sha={SHA}" in sig
        assert ("-a domain=demo.example.com" in sig) is img.domain_overlay


@pytest.mark.parametrize("bad,needle", [
    ("RELEASE_IMAGE_ENGINE=ghcr.io/rahulkar/keelson/engine:latest", "invalid release entry RELEASE_IMAGE_ENGINE"),
    (f"RELEASE_IMAGE_ENGINE=docker.io/library/engine@{digest('x')}", "invalid release entry"),
    ("RELEASE_REF=refs/heads/feature", "invalid release entry RELEASE_REF"),
    ("RELEASE_REF=refs/pull/1/merge", "invalid release entry RELEASE_REF"),
    ("RELEASE_REPOSITORY=evil/keelson-supply-chain-recovery", "invalid release entry RELEASE_REPOSITORY"),
    ("RELEASE_GIT_SHA=abc", "invalid release entry RELEASE_GIT_SHA"),
    ("RELEASE_DOMAIN=$(id).example.com", "invalid release entry RELEASE_DOMAIN"),
    ("RELEASE_UNKNOWN=x", "invalid release entry RELEASE_UNKNOWN"),
    ("RELEASE_SCHEMA=keelson.release/1", "duplicate key RELEASE_SCHEMA"),
    ("EXTRA LINE", "bad line in release file"),
])
def test_release_file_is_validated_strictly(tmp_path: Path, bad: str, needle: str) -> None:
    rel = write_release(tmp_path / "r")
    text = rel.read_text(encoding="utf-8")
    key = bad.split("=", 1)[0]
    if key.startswith("RELEASE_") and key in text and key != "RELEASE_SCHEMA":
        text = "\n".join(bad if ln.startswith(key + "=") else ln for ln in text.splitlines()) + "\n"
    else:
        text += bad + "\n"
    rel.write_text(text, encoding="utf-8")
    rc, lines, calls, _ = run(tmp_path, "--release", str(rel))
    assert rc == 1 and lines[0]["status"] == "FAIL" and needle in lines[0]["detail"], lines
    assert len(calls) == 1  # only the bundle was checked; no image is trusted from a bad file


def test_release_signature_failure(tmp_path: Path) -> None:
    rel = write_release(tmp_path / "r")
    rc, lines, calls, _ = run(tmp_path, "--release", str(rel), fail="sigstore.json")
    assert rc == 1 and len(lines) == 1 and "release signature" in lines[0]["detail"]


def test_missing_bundle(tmp_path: Path) -> None:
    rel = write_release(tmp_path / "r")
    Path(str(rel) + ".sigstore.json").unlink()
    rc, lines, calls, _ = run(tmp_path, "--release", str(rel))
    assert rc == 1 and "bundle" in lines[0]["detail"] and calls == []


def test_key_mode_and_no_provenance(tmp_path: Path) -> None:
    pub = tmp_path / "test.pub"
    pub.write_text("-----BEGIN PUBLIC KEY-----\nx\n-----END PUBLIC KEY-----\n", encoding="utf-8")
    env = tmp_path / "signing.env"
    env.write_text(SIGNING.read_text(encoding="utf-8").replace("SIGNER_MODE=keyless", "SIGNER_MODE=key")
                   .replace("SIGNER_KEY=", f"SIGNER_KEY={pub}").replace("REQUIRE_PROVENANCE=1", "REQUIRE_PROVENANCE=0"),
                   encoding="utf-8")
    ref = f"{GHCR}/a@{digest('a')}"
    rc, lines, calls, err = run(tmp_path, "--signing-env", str(env), ref)
    assert rc == 0 and lines[0]["provenance"] == "skipped"
    assert calls == [f"verify --key {pub} --insecure-ignore-tlog=true {ref}",
                     f"verify-attestation --key {pub} --insecure-ignore-tlog=true --type spdxjson {ref}"]
    assert "WARNING: SIGNER_MODE=key" in err


@pytest.mark.parametrize("edit,needle", [
    (("SIGNER_MODE=keyless", "SIGNER_MODE=none"), "SIGNER_MODE must be keyless or key"),
    (("SIGNER_IDENTITY_REGEXP=^", "SIGNER_IDENTITY_REGEXP="), "must be anchored"),
    (("REQUIRE_PROVENANCE=1", "REQUIRE_PROVENANCE=1\nEVIL=1"), "unknown key EVIL"),
    (("COSIGN_IMAGE=ghcr.io/sigstore/cosign/cosign:v3.1.3@", "COSIGN_IMAGE=ghcr.io/sigstore/cosign/cosign:v3.1.3#"),
     "COSIGN_IMAGE must be pinned by digest"),
])
def test_bad_signing_policy_is_a_usage_error(tmp_path: Path, edit: tuple[str, str], needle: str) -> None:
    env = tmp_path / "signing.env"
    env.write_text(SIGNING.read_text(encoding="utf-8").replace(*edit), encoding="utf-8")
    rc, lines, calls, err = run(tmp_path, "--signing-env", str(env), f"{GHCR}/a@{digest('a')}")
    assert rc == 2 and needle in err and calls == [] and lines == []


def test_compose_mode(tmp_path: Path) -> None:
    rc, lines, calls, _ = run(tmp_path, "--compose")
    assert rc == 0, lines
    by_status = {x["image"].split(":")[0]: x["status"] for x in lines}
    assert by_status["ghcr.io/berriai/litellm-non_root"] == "PASS"
    assert {s for n, s in by_status.items() if n != "ghcr.io/berriai/litellm-non_root"} == {"PINNED"}
    assert len(calls) == 1 and calls[0].startswith("verify --key ")
    assert calls[0].split()[2].endswith("infra/supply-chain/keys/litellm-cosign.pub")


def test_usage_errors(tmp_path: Path) -> None:
    assert run(tmp_path)[0] == 2
    assert run(tmp_path, "--compose", "x@sha256:" + "a" * 64)[0] == 2


def test_upstream_mode(tmp_path: Path) -> None:
    ref = f"ghcr.io/berriai/litellm-non_root:1.104.0@{digest('litellm')}"
    rc, lines, calls, _ = run(tmp_path, "--upstream", ref, "infra/supply-chain/keys/litellm-cosign.pub")
    assert rc == 0 and lines[0]["status"] == "PASS"
    assert calls == [f"verify --key {ROOT}/infra/supply-chain/keys/litellm-cosign.pub {ref}"]
    rc, lines, _, _ = run(tmp_path / "2", "--upstream", "ghcr.io/berriai/litellm-non_root:1.104.0", "k.pub")
    assert rc == 1 and lines[0]["signature"] == "refused"
