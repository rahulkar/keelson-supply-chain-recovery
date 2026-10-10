"""release_manifest.py (keelson-release.env) and compose-release.sh (verified local stack override)."""

from __future__ import annotations

import hashlib
import os
import posixpath
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import ROOT, fake_cosign

import registry as reg
import release_manifest as rm

GHCR = "ghcr.io/rahulkar/keelson"
SHA = "ab" * 20
COMMON = ["--repository", "rahulkar/keelson-supply-chain-recovery", "--git-sha", SHA, "--ref", "refs/heads/main",
          "--run-id", "123", "--created-at", "2026-10-10T08:00:00Z"]
REGISTRY = reg.load(ROOT)


def digest(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


def refs_dir(tmp: Path, *, overlays: bool = True, extra: dict[str, str] | None = None) -> Path:
    d = tmp / "refs"
    d.mkdir(parents=True, exist_ok=True)
    for img in REGISTRY.release_images:
        if img.domain_overlay and not overlays:
            continue
        (d / f"{img.name}.ref").write_text(f"{img.release_key}={GHCR}/{img.name}@{digest(img.name)}\n", encoding="utf-8")
    for k, v in (extra or {}).items():
        (d / f"{k.lower()}.ref").write_text(f"{k}={v}\n", encoding="utf-8")
    return d


def manifest(tmp: Path, *args: str, overlays: bool = True, extra: dict[str, str] | None = None) -> tuple[int, str]:
    out = tmp / "keelson-release.env"
    rc = rm.main(["--refs-dir", str(refs_dir(tmp, overlays=overlays, extra=extra)), "--out", str(out), *COMMON, *args])
    return rc, out.read_text(encoding="utf-8") if out.exists() else ""


def test_deterministic_sorted_output(tmp_path: Path) -> None:
    rc, text = manifest(tmp_path, "--domain", "Demo.Example.com")
    assert rc == 0
    lines = text.splitlines()
    assert lines[:7] == ["RELEASE_SCHEMA=keelson.release/1", "RELEASE_REPOSITORY=rahulkar/keelson-supply-chain-recovery",
                         f"RELEASE_GIT_SHA={SHA}", "RELEASE_REF=refs/heads/main", "RELEASE_RUN_ID=123",
                         "RELEASE_CREATED_AT=2026-10-10T08:00:00Z", "RELEASE_DOMAIN=demo.example.com"]
    images = lines[7:]
    assert images == sorted(images) and len(images) == len(REGISTRY.release_images)
    assert f"RELEASE_IMAGE_ENGINE_CLOUDRUN={GHCR}/engine-cloudrun@{digest('engine-cloudrun')}" in images
    rc2, text2 = manifest(tmp_path / "again", "--domain", "demo.example.com")
    assert text2 == text and text.endswith("\n") and "\r" not in text


def test_without_domain_there_are_no_overlays(tmp_path: Path) -> None:
    rc, text = manifest(tmp_path, overlays=False)
    assert rc == 0 and "RELEASE_DOMAIN=\n" in text and "CLOUDRUN=" in text  # keycloak/litellm overlays are not domain overlays
    assert "ENGINE_CLOUDRUN" not in text and "KC_CONFIG_CLOUDRUN" not in text


@pytest.mark.parametrize("args,overlays,extra,needle", [
    ((), True, None, "ENGINE_CLOUDRUN: a domain overlay needs --domain"),
    (("--domain", "demo.example.com"), False, None, "ENGINE_CLOUDRUN (engine-cloudrun) is missing from the release"),
    (("--domain", "bad_domain"), True, None, "invalid domain"),
    (("--git-sha", "abc"), False, None, "invalid git sha"),
    (("--ref", "refs/heads/feature"), False, None, "invalid ref"),
    (("--created-at", "yesterday"), False, None, "invalid created-at"),
    ((), False, {"UNKNOWN": f"{GHCR}/unknown@{digest('u')}"}, "UNKNOWN is not a release image"),
    ((), False, {"PG_CORE": f"{GHCR}/pg-core@{digest('p')}"}, "PG_CORE is not a release image"),
])
def test_validation(tmp_path: Path, capsys: pytest.CaptureFixture[str], args: tuple[str, ...], overlays: bool,
                    extra: dict[str, str] | None, needle: str) -> None:
    rc, text = manifest(tmp_path, *args, overlays=overlays, extra=extra)
    assert rc == 2 and text == ""
    assert needle in capsys.readouterr().err


def test_ref_must_be_the_named_image_in_the_release_registry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    d = refs_dir(tmp_path, overlays=False)
    (d / "optimizer.ref").write_text(f"OPTIMIZER={GHCR}/engine@{digest('optimizer')}\n", encoding="utf-8")
    assert rm.main(["--refs-dir", str(d), "--out", str(tmp_path / "o.env"), *COMMON]) == 2
    assert f"is not {GHCR}/optimizer@sha256:<digest>" in capsys.readouterr().err
    (d / "optimizer.ref").write_text(f"OPTIMIZER={GHCR}/optimizer:latest\n", encoding="utf-8")
    assert rm.main(["--refs-dir", str(d), "--out", str(tmp_path / "o.env"), *COMMON]) == 2


def test_conflicting_duplicates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    d = refs_dir(tmp_path, overlays=False)
    (d / "dup.ref").write_text(f"OPTIMIZER={GHCR}/optimizer@{digest('other')}\n", encoding="utf-8")
    assert rm.main(["--refs-dir", str(d), "--out", str(tmp_path / "o.env"), *COMMON]) == 2
    assert "OPTIMIZER appears twice with different digests" in capsys.readouterr().err


# ───────────────────────────── the manifest round-trips through verify-images.sh / compose-release.sh ─────
def run_bash(script: str, *args: str, tmp: Path, fail: str = "") -> tuple[subprocess.CompletedProcess[str], list[str]]:
    binary, log = fake_cosign(tmp / "fake")
    env = dict(os.environ, COSIGN_BIN=str(binary), FAKE_COSIGN_LOG=str(log), FAKE_COSIGN_FAIL=fail)
    res = subprocess.run(["bash", str(ROOT / "scripts" / "supply-chain" / script), *args], capture_output=True,
                         text=True, env=env, timeout=120)
    return res, (log.read_text(encoding="utf-8").splitlines() if log.exists() else [])


@pytest.fixture()
def release(tmp_path: Path) -> Path:
    rc, _ = manifest(tmp_path, "--domain", "demo.example.com")
    assert rc == 0
    rel = tmp_path / "keelson-release.env"
    Path(f"{rel}.sigstore.json").write_text("{}", encoding="utf-8")
    return rel


def test_manifest_verifies(release: Path, tmp_path: Path) -> None:
    res, calls = run_bash("verify-images.sh", "--release", str(release), tmp=tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert len(calls) == 1 + 3 * len(REGISTRY.release_images)


def _reset_loader() -> type[yaml.SafeLoader]:
    class L(yaml.SafeLoader):
        pass
    L.add_constructor("!reset", lambda loader, node: None)
    return L


def test_compose_release_override(release: Path, tmp_path: Path) -> None:
    out = tmp_path / "compose.release.yaml"
    res, calls = run_bash("compose-release.sh", "--release", str(release), "--out", str(out), tmp=tmp_path)
    assert res.returncode == 0, res.stderr
    doc = yaml.load(out.read_text(encoding="utf-8"), Loader=_reset_loader())  # noqa: S506 (SafeLoader subclass)
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]

    def dockerfile(build: str | dict[str, str]) -> str:
        ctx, df = (build, "Dockerfile") if isinstance(build, str) else (build.get("context", "."),
                                                                           build.get("dockerfile", "Dockerfile"))
        return posixpath.normpath(posixpath.join(ctx, df))

    # every compose service built from a release image is replaced; pg-core and other local-only builds
    # (e.g. a tool image that is not a release image) keep their build
    release_dfs = {i.dockerfile for i in REGISTRY.release_images if not i.overlay_of}
    built = {s for s, d in compose.items() if "build" in d and dockerfile(d["build"]) in release_dfs}
    assert "pg-core" not in built and {"engine-api", "keelson-web", "signal-hub-api"} <= built
    assert set(doc["services"]) == built
    for svc, d in doc["services"].items():
        assert d["build"] is None and d["image"].startswith(f"{GHCR}/") and "@sha256:" in d["image"], svc
    assert doc["services"]["engine-worker"]["image"] == f"{GHCR}/engine@{digest('engine')}"
    assert "build: !reset null" in out.read_text(encoding="utf-8")
    # each distinct image verified once, bound to the release commit
    sigs = [c for c in calls if c.startswith("verify ")]
    assert len(sigs) == len({d["image"] for d in doc["services"].values()})
    assert all(f"-a git_sha={SHA}" in c for c in sigs)


@pytest.mark.parametrize("fail", ["sigstore.json", digest("merxa-web")])
def test_compose_release_writes_nothing_on_failure(release: Path, tmp_path: Path, fail: str) -> None:
    out = tmp_path / "compose.release.yaml"
    res, _ = run_bash("compose-release.sh", "--release", str(release), "--out", str(out), tmp=tmp_path, fail=fail)
    assert res.returncode == 1 and not out.exists() and "refusing" in res.stderr
    assert not list(tmp_path.glob("compose.release.yaml.tmp*"))
