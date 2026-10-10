"""check_pins.py: one passing and failing fixture per rule (P1-P9), the exit code, and the real repo."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import CHECKOUT, D, PY, ROOT, UV, VALKEY, write_tree

import check_pins


def findings(root: Path) -> list[check_pins.Finding]:
    return check_pins.Checker(root).run()


def rules(root: Path) -> list[tuple[str, str]]:
    return [(f.rule, f.message) for f in findings(root)]


def edit(root: Path, rel: str, old: str, new: str) -> None:
    p = root / rel
    text = p.read_text(encoding="utf-8")
    assert old in text, (rel, old)
    p.write_text(text.replace(old, new), encoding="utf-8")


def test_good_tree_is_clean(good_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert findings(good_tree) == []
    assert check_pins.main(["--root", str(good_tree)]) == 0
    assert "0 violation(s)" in capsys.readouterr().err


# ───────────────────────────── P1 Dockerfiles ─────────────────────────────
def test_p1_unpinned_arg_default_and_inexact_tag(good_tree: Path) -> None:
    edit(good_tree, "services/app/Dockerfile", f"ARG PYTHON_IMAGE={PY}", "ARG PYTHON_IMAGE=python:3.14-slim-trixie")
    got = rules(good_tree)
    assert ("P1", "ARG PYTHON_IMAGE default (used by FROM) python:3.14-slim-trixie is not pinned by digest "
                  "(run: scripts/supply-chain/pin-image.sh python:3.14-slim-trixie)") in got
    assert any(r == "P6" and "not an exact version" in m for r, m in got)


def test_p1_syntax_copy_from_and_missing_arg(good_tree: Path) -> None:
    edit(good_tree, "services/app/Dockerfile", f"# syntax=docker/dockerfile:1.7.1@{D['c']}", "# syntax=docker/dockerfile:1.7")
    edit(good_tree, "services/app/Dockerfile", f"COPY --from={UV}", "COPY --from=ghcr.io/astral-sh/uv:latest")
    edit(good_tree, "services/app/Dockerfile", "FROM ${PYTHON_IMAGE} AS runtime", "FROM ${OTHER} AS runtime")
    got = rules(good_tree)
    assert any(r == "P1" and m.startswith("# syntax frontend docker/dockerfile:1.7 is not pinned") for r, m in got)
    assert any(r == "P1" and m.startswith("COPY --from ghcr.io/astral-sh/uv:latest is not pinned") for r, m in got)
    assert any(r == "P6" and "floating tag 'latest'" in m for r, m in got)
    assert ("P1", "FROM ${OTHER} has no ARG default (give it a pinned default)") in got


def test_p1_stage_names_and_local_images_are_fine(good_tree: Path) -> None:
    write_tree(good_tree, {"infra/x/overlay.Dockerfile": "ARG BASE=keelson-engine-api:latest\nFROM ${BASE}\nFROM scratch\n"})
    got = rules(good_tree)
    assert [r for r, _ in got] == ["P9"]  # only: not registered


# ───────────────────────────── P2 / P3 ─────────────────────────────
def test_p2_compose_image(good_tree: Path) -> None:
    edit(good_tree, "compose.yaml", f"image: {VALKEY}", "image: caddy:2-alpine")
    got = rules(good_tree)
    assert ("P2", "service cache image caddy:2-alpine is not pinned by digest "
                  "(run: scripts/supply-chain/pin-image.sh caddy:2-alpine)") in got
    assert any(r == "P6" and "caddy:2-alpine" in m for r, m in got)


def test_p3_pin_files(good_tree: Path) -> None:
    write_tree(good_tree, {"infra/supply-chain/signing.env": "COSIGN_IMAGE=ghcr.io/sigstore/cosign/cosign:v3.1.3\n"})
    assert ("P3", "COSIGN_IMAGE ghcr.io/sigstore/cosign/cosign:v3.1.3 is not pinned by digest "
                  "(run: scripts/supply-chain/pin-image.sh ghcr.io/sigstore/cosign/cosign:v3.1.3)") in rules(good_tree)


# ───────────────────────────── P4 / P5 ─────────────────────────────
def test_p4_script_literals_assignments_and_powershell(good_tree: Path) -> None:
    write_tree(good_tree, {
        "scripts/a.sh": "docker run --rm valkey/valkey:8.1.10-alpine true\nCADDY_IMAGE=\"caddy:2.11.7-alpine\"\n",
        "scripts/b.ps1": "$image = 'python:3.14.8-slim-trixie'\r\n",
        "e2e/c.sh": "docker run koalaman/shellcheck:stable -x a.sh\n",
    })
    got = {(f.rule, f.file, f.line) for f in findings(good_tree)}
    assert {("P4", "scripts/a.sh", 1), ("P4", "scripts/a.sh", 2), ("P4", "scripts/b.ps1", 1),
            ("P4", "e2e/c.sh", 1), ("P6", "e2e/c.sh", 1)} <= got
    assert not any(f == "scripts/run.sh" or f == "scripts/run.ps1" for _, f, _ in got)  # comments, URLs, ${PREFIX}


@pytest.mark.parametrize("uses,needle", [
    ("actions/checkout@v7", "is not pinned to a full 40-hex commit SHA"),
    (CHECKOUT, "needs a '# vX.Y.Z' version comment"),
    ("docker://alpine:3.22", "docker:// actions are forbidden"),
])
def test_p5_workflow_uses(good_tree: Path, uses: str, needle: str) -> None:
    edit(good_tree, ".github/workflows/ci.yml", f"uses: {CHECKOUT} # v7.0.1", f"uses: {uses}")
    got = [m for r, m in rules(good_tree) if r == "P5"]
    assert len(got) == 1 and needle in got[0]


def test_p4_registry_tool_suite_images(good_tree: Path) -> None:
    """The toolchain suites of images.json run pinned images like the scripts: same pin rules (P4/P6/P8)."""
    reg_path = good_tree / "infra/supply-chain/images.json"
    data = json.loads(reg_path.read_text())
    data["suites"] = [{"id": "tools", "image": PY, "workdir": ".", "cmd": ["true"]}]
    reg_path.write_text(json.dumps(data, indent=1))
    assert findings(good_tree) == []
    data["suites"][0]["image"] = f"python:latest@{D['e']}"
    reg_path.write_text(json.dumps(data, indent=1))
    got = [(f.rule, f.file, f.message) for f in findings(good_tree)]
    assert [(r, f) for r, f, _ in got] == [("P6", "infra/supply-chain/images.json")]
    assert "suite tools image python:latest@" in got[0][2] and "floating tag 'latest'" in got[0][2]
    data["suites"][0]["image"] = f"python:3.14.8-slim-trixie@{D['f']}"  # stale digest: the scripts pin D['a']
    reg_path.write_text(json.dumps(data, indent=1))
    got = [m for r, m in rules(good_tree) if r == "P8"]
    assert len(got) == 1 and got[0].startswith("one pin per image: python is pinned to 2 different tag/digest pairs")
    # another variant of a pinned repository (uv's python image next to the Dockerfiles' plain uv) is fine
    data["suites"][0]["image"] = f"ghcr.io/astral-sh/uv:0.12.23-python3.14-trixie-slim@{D['e']}"
    reg_path.write_text(json.dumps(data, indent=1))
    assert findings(good_tree) == []


def test_p6_floating_tag_even_with_digest(good_tree: Path) -> None:
    edit(good_tree, "compose.yaml", f"image: {VALKEY}", f"image: valkey/valkey:latest@{D['e']}")
    edit(good_tree, "scripts/run.sh", f"docker run --rm {VALKEY}", f"docker run --rm valkey/valkey:latest@{D['e']}")
    got = rules(good_tree)
    assert [r for r, _ in got] == ["P6", "P6"] and "floating tag 'latest'" in got[0][1]


# ───────────────────────────── P7 caps ─────────────────────────────
def test_p7_litellm_banned_versions_in_any_uv_lock(good_tree: Path) -> None:
    write_tree(good_tree, {"services/x/uv.lock": '[[package]]\nname = "litellm"\nversion = "1.82.8"\n'})
    got = [m for r, m in rules(good_tree) if r == "P7"]
    assert got == ["litellm 1.82.8 violates the cap !=1.82.8: plan §14: litellm 1.82.7/1.82.8 are banned (compromised releases)"]


def test_p7_litellm_ban_sees_every_locked_version(good_tree: Path) -> None:
    """uv.lock lists a package once per resolved version (resolution forks): a banned version is caught
    even when an allowed version of the same package follows it."""
    entry = ('[[package]]\nname = "litellm"\nversion = "{}"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
             'resolution-markers = [\n    "{}",\n]\n\n')
    write_tree(good_tree, {"services/x/uv.lock": entry.format("1.82.7", "sys_platform == 'linux'")
                           + entry.format("1.104.0", "sys_platform != 'linux'")})
    got = [m for r, m in rules(good_tree) if r == "P7"]
    assert got == ["litellm 1.82.7 violates the cap !=1.82.7: plan §14: litellm 1.82.7/1.82.8 are banned (compromised releases)"]


def test_p7_pnpm_caps(good_tree: Path) -> None:
    write_tree(good_tree, {"pnpm-lock.yaml": (
        "lockfileVersion: '9.0'\n\nimporters:\n  .: {}\n\npackages:\n\n"
        "  motion@14.0.0:\n    resolution: {integrity: x}\n\n"
        "  '@tanstack/react-table@8.21.3':\n    resolution: {integrity: x}\n\n"
        "  typescript@6.1.0:\n    resolution: {integrity: x}\n\n"
        "snapshots:\n\n  motion@99.0.0: {}\n")})
    got = sorted(m.split(" violates")[0] for r, m in rules(good_tree) if r == "P7")
    assert got == ["motion 14.0.0", "typescript 6.1.0"]


def test_p7_conditional_engine_caps(good_tree: Path) -> None:
    lock = ('[[package]]\nname = "google-adk"\nversion = "2.11.0"\n\n'
            '[[package]]\nname = "fastapi"\nversion = "0.142.2"\n\n'
            '[[package]]\nname = "opentelemetry-api"\nversion = "1.43.0"\n')
    write_tree(good_tree, {"services/engine/uv.lock": lock})
    assert len([r for r, _ in rules(good_tree) if r == "P7"]) == 2
    write_tree(good_tree, {"services/engine/uv.lock": lock.replace('"2.11.0"', '"2.12.0"')})
    assert [r for r, _ in rules(good_tree) if r == "P7"] == []  # the cap belongs to google-adk 2.11


def test_p7_image_caps(good_tree: Path) -> None:
    edit(good_tree, "scripts/run.sh", f'IMAGE="{PY}"', f'IMAGE="python:3.15.0-slim-trixie@{D["e"]}"')
    got = [m for r, m in rules(good_tree) if r == "P7"]
    assert got == ["python:3.15.0-slim-trixie violates the cap <3.15: requires-python <3.15 in every uv project"]


# ───────────────────────────── P8 consistency ─────────────────────────────
def test_p8_one_pin_per_image(good_tree: Path) -> None:
    edit(good_tree, "scripts/run.ps1", f"$image = '{PY}'", f"$image = 'python:3.14.8-slim-trixie@{D['f']}'")
    got = [m for r, m in rules(good_tree) if r == "P8"]
    assert len(got) == 1 and got[0].startswith("one pin per image: python is pinned to 2 different tag/digest pairs")


def test_p8_tool_versions_and_overlay_defaults(good_tree: Path) -> None:
    write_tree(good_tree, {
        "scripts/native/versions.psd1": "@{\n    uv = @{\n        Version = '0.12.24'\n    }\n    pnpm = @{ Version = '10.34.6' }\n}\n",
        "package.json": json.dumps({"packageManager": "pnpm@10.30.0"}),
        "infra/cloudrun/images/x.Dockerfile": f"ARG VALKEY_IMAGE={VALKEY}\nFROM ${{VALKEY_IMAGE}}\n",
        "infra/cloudrun/versions.env": f"VALKEY_ENGINE_VERSION=VALKEY_8_0\nVALKEY_IMAGE=valkey/valkey:8.1.11-alpine@{D['e']}\n",
    })
    got = [m for r, m in rules(good_tree) if r == "P8"]
    assert any(m.startswith("uv 0.12.24 differs from the Dockerfile uv image tags ['0.12.23']") for m in got)
    assert any(m.startswith("pnpm versions differ: 10.30.0: package.json; 10.34.6: scripts/native/versions.psd1") for m in got)
    assert any(m.startswith("ARG VALKEY_IMAGE default valkey/valkey:8.1.10-alpine@") for m in got)


def test_p8_pnpm_release_age_equals_renovate(good_tree: Path) -> None:
    pkg = json.dumps({"packageManager": "pnpm@10.34.6"})
    write_tree(good_tree, {
        "renovate.json": json.dumps({"minimumReleaseAge": "3 days"}),
        "package.json": pkg, "pnpm-workspace.yaml": "packages:\n  - apps/a\nminimumReleaseAge: 4320  # 3 days\n",
        "apps/merxa-web/package.json": pkg,
    })
    got = [(f.file, f.message) for f in findings(good_tree) if f.rule == "P8"]
    assert got == [("apps/merxa-web/pnpm-workspace.yaml",
                    "pnpm minimumReleaseAge is not set (renovate.json minimumReleaseAge 3 days = 4320 minutes)")]
    write_tree(good_tree, {"apps/merxa-web/pnpm-workspace.yaml": "minimumReleaseAge: 1440\n"})
    got2 = [(f.file, f.line, f.message) for f in findings(good_tree) if f.rule == "P8"]
    assert got2 == [("apps/merxa-web/pnpm-workspace.yaml", 1,
                     "pnpm minimumReleaseAge 1440 differs from renovate.json minimumReleaseAge 3 days (= 4320 minutes)")]
    write_tree(good_tree, {"apps/merxa-web/pnpm-workspace.yaml": "minimumReleaseAge: 4320\n"})
    assert [f for f in findings(good_tree) if f.rule == "P8"] == []
    write_tree(good_tree, {"renovate.json": json.dumps({"minimumReleaseAge": "72 hours"})})
    assert [m for r, m in rules(good_tree) if r == "P8"] == [
        "minimumReleaseAge '72 hours' is not 'N days' (pnpm needs the same minimum)"]


# ───────────────────────────── P9 registry coverage ─────────────────────────────
def test_p9_unregistered_dockerfile_and_compose_build(good_tree: Path) -> None:
    write_tree(good_tree, {"services/sidecar/Dockerfile": f"FROM {PY} AS test\nFROM {PY}\n"})
    edit(good_tree, "compose.yaml", "  cache:", "  sidecar:\n    build: ./services/sidecar\n  cache:")
    got = [(f.file, f.message) for f in findings(good_tree) if f.rule == "P9"]
    assert ("services/sidecar/Dockerfile", "Dockerfile services/sidecar/Dockerfile is not registered in "
            "infra/supply-chain/images.json: add an entry (name, dockerfile, context, target, release, suites; "
            "docs/supply-chain.md)") in got
    assert ("compose.yaml", "service sidecar builds services/sidecar/Dockerfile, which is not registered in "
            "infra/supply-chain/images.json") in got


def test_p9_test_stage_without_suite_and_bad_target(good_tree: Path) -> None:
    reg_path = good_tree / "infra/supply-chain/images.json"
    data = json.loads(reg_path.read_text())
    data["images"][0]["suites"] = []
    data["images"][0]["target"] = "missing"
    reg_path.write_text(json.dumps(data))
    got = [m for r, m in rules(good_tree) if r == "P9"]
    assert "stage 'test' has no CI suite in infra/supply-chain/images.json (image 'app')" in got
    assert "image 'app': target 'missing' is not a stage of services/app/Dockerfile" in got


def test_p9_invalid_registry_is_reported_not_raised(good_tree: Path) -> None:
    (good_tree / "infra/supply-chain/images.json").write_text('{"schema": "x", "images": [{"name": "Bad Name"}]}')
    got = [m for r, m in rules(good_tree) if r == "P9"]
    assert "schema must be 'keelson.images/1'" in got
    assert any("'dockerfile' is required" in m for m in got)


def test_p9_deploy_vars_and_release_map(good_tree: Path) -> None:
    write_tree(good_tree, {"scripts/cloudrun/deploy.sh": (
        "RENDER_VARS=(PROJECT IMAGE_APP IMAGE_OTHER IMAGE_PHOENIX)\n"
        "RELEASE_IMAGE_MAP=(IMAGE_APP:APP)\n"
        '[ "$ENABLE_PHOENIX" = 1 ] && mirror IMAGE_PHOENIX "$PHOENIX_IMAGE"\n')})
    reg_path = good_tree / "infra/supply-chain/images.json"
    data = json.loads(reg_path.read_text())
    data["images"][0].update(release=True, deploy_var="IMAGE_APP")
    reg_path.write_text(json.dumps(data))
    got = [m for r, m in rules(good_tree) if r == "P9"]
    assert got == ["IMAGE_OTHER is deployed but no release image in infra/supply-chain/images.json feeds it (deploy_var)"]


# ───────────────────────────── exit code + real repository ─────────────────────────────
def test_exit_code_is_the_number_of_violations(good_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    edit(good_tree, "compose.yaml", f"image: {VALKEY}", "image: valkey/valkey:8.1.10-alpine")
    write_tree(good_tree, {"scripts/z.sh": "docker run koalaman/shellcheck:v0.11.0 a\n"})
    rc = check_pins.main(["--root", str(good_tree)])
    out = capsys.readouterr()
    lines = [json.loads(x) for x in out.out.splitlines()]
    assert rc == len(lines) == 2
    assert {x["rule"] for x in lines} == {"P2", "P4"} and "2 violation(s) (P2: 1, P4: 1)" in out.err
    assert check_pins.main(["--root", str(good_tree), "--rule", "P2"]) == 1


def test_version_helpers() -> None:
    assert check_pins.satisfies("13.5.1", "<14") and not check_pins.satisfies("14.0.0", "<14")
    assert check_pins.satisfies("6.12.0", ">=6.4.1") and not check_pins.satisfies("6.4.0", ">=6.4.1")
    assert not check_pins.satisfies("1.82.7", "!=1.82.7") and check_pins.satisfies("1.82.70", "!=1.82.7")
    assert check_pins.satisfies("2.11.0", "==2.11.0") and not check_pins.satisfies("2.11.1", "==2.11.0")
    assert check_pins.version_tuple("version-20.20.0") == (20, 20, 0)
    assert check_pins.version_tuple("v1.63.0-noble") == (1, 63, 0)
    assert check_pins.tag_problem("postgis/postgis", "18-3.6") is None
    assert check_pins.tag_problem("postgres", "18-trixie") is not None
    assert check_pins.tag_problem("python", "3.14-slim-trixie") is not None


def test_repository_is_clean() -> None:
    got = findings(ROOT)
    assert got == [], "\n".join(f.as_json() for f in got)
