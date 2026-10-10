"""renovate.json: presets, every 60-build-guide cap, the gated groups, and regex managers that match the
real repository files (scripts/supply-chain/tests/renovate_extract.sh runs Renovate's own extraction)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from conftest import ROOT

import check_pins

REPO = check_pins.Repo(ROOT)

CFG: dict[str, Any] = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
RULES: list[dict[str, Any]] = CFG["packageRules"]


def rules_for(name: str, key: str = "matchPackageNames") -> list[dict[str, Any]]:
    return [r for r in RULES if name in r.get(key, []) or name in r.get("matchDepNames", [])]


def effective(name: str, option: str, **match: str) -> Any:
    """The value Renovate ends up with for `option` (later packageRules win)."""
    value = CFG.get(option)
    for r in RULES:
        names = r.get("matchPackageNames", []) + r.get("matchDepNames", [])
        if names and name not in names:
            continue
        if not names and not ({"matchDatasources", "matchManagers", "matchUpdateTypes"} & set(r)):
            continue
        if "matchDatasources" in r and match.get("datasource") not in r["matchDatasources"]:
            continue
        if "matchUpdateTypes" in r and match.get("update") not in r["matchUpdateTypes"]:
            continue
        if "matchManagers" in r and match.get("manager") not in r["matchManagers"]:
            continue
        if "matchFileNames" in r and match.get("file") not in r["matchFileNames"]:
            continue
        if option in r:
            value = r[option]
    return value


def test_presets_and_policy() -> None:
    assert {"config:recommended", "docker:pinDigests", "helpers:pinGitHubActionDigestsToSemver"} <= set(CFG["extends"])
    assert CFG["minimumReleaseAge"] == "3 days" and CFG["dependencyDashboard"] is True
    assert CFG["vulnerabilityAlerts"]["enabled"] is True and CFG["osvVulnerabilityAlerts"] is True
    assert CFG["lockFileMaintenance"]["enabled"] is True
    assert CFG["internalChecksFilter"] == "strict"
    assert set(CFG["enabledManagers"]) == {"npm", "pep621", "pip_requirements", "dockerfile", "docker-compose",
                                           "github-actions", "custom.regex"}


def test_ignore_paths_keep_tests_dirs_but_skip_fixtures() -> None:
    """Renovate's default ignorePaths skip **/tests/**, which would hide scripts/cloudrun/tests/*.sh pins."""
    paths = CFG["ignorePaths"]
    assert "**/tests/**" not in paths and "**/test/**" not in paths
    assert "scripts/supply-chain/tests/fixtures/**" in paths and "**/node_modules/**" in paths


@pytest.mark.parametrize("name,allowed", [
    ("motion", "<14"), ("@tanstack/react-table", "<9"), ("typescript", "<6.1"), ("@types/node", "<25"),
    ("maplibre-gl", ">=6.4.1"),
])
def test_npm_caps(name: str, allowed: str) -> None:
    assert effective(name, "allowedVersions", datasource="npm") == allowed


@pytest.mark.parametrize("name,allowed", [
    ("python", "/^3\\.14\\./"), ("node", "/^24\\./"), ("postgres", "/^18\\./"), ("valkey/valkey", "/^8\\./"),
    ("quay.io/keycloak/keycloak", "/^26\\./"), ("caddy", "/^2\\./"),
])
def test_image_caps(name: str, allowed: str) -> None:
    assert effective(name, "allowedVersions", datasource="docker") == allowed


def test_engine_fastapi_cap_after_the_web_stack_group() -> None:
    idx_group = next(i for i, r in enumerate(RULES) if r.get("groupName") == "python web stack")
    idx_cap = next(i for i, r in enumerate(RULES) if r.get("matchFileNames") == ["services/engine/pyproject.toml"])
    assert idx_cap > idx_group
    assert effective("fastapi", "allowedVersions", datasource="pypi", file="services/engine/pyproject.toml") == "<0.142"
    assert effective("fastapi", "allowedVersions", datasource="pypi", file="services/optimizer/pyproject.toml") is None


@pytest.mark.parametrize("version,allowed", [("1.82.7", False), ("1.82.8", False), ("1.82.6", True),
                                             ("1.82.70", True), ("1.104.0", True)])
def test_litellm_banned_versions(version: str, allowed: bool) -> None:
    for name in ("litellm", "ghcr.io/berriai/litellm-non_root"):
        spec = effective(name, "allowedVersions")
        assert spec.startswith("!/") and spec.endswith("/")
        assert (re.search(spec[2:-1], version) is None) is allowed


@pytest.mark.parametrize("group,members", [
    ("adk stack", {"google-adk", "google-genai"}),
    ("litellm", {"litellm", "ghcr.io/berriai/litellm-non_root"}),
    ("mcp", {"mcp", "fastmcp"}),
    ("ag-ui", {"@ag-ui/core", "ag-ui-protocol"}),
])
def test_gated_groups_never_automerge(group: str, members: set[str]) -> None:
    rule = next(r for r in RULES if r.get("groupName") == group)
    assert set(rule["matchPackageNames"]) == members
    assert rule["dependencyDashboardApproval"] is True and rule["automerge"] is False
    assert rule["minimumReleaseAge"] == "7 days"
    assert "needs-spike" in rule["addLabels"]
    # no later rule re-enables automerge for these packages
    later = RULES[RULES.index(rule) + 1:]
    assert not any(r.get("automerge") for r in later if set(r.get("matchPackageNames", [])) & members)
    for m in members:
        for update in ("minor", "patch", "digest"):
            assert effective(m, "automerge", update=update, datasource="docker" if "/" in m and "." in m else "pypi") is False


def regex_managers() -> list[dict[str, Any]]:
    return [m for m in CFG["customManagers"] if m["customType"] == "regex"]


def py_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern.replace("(?<", "(?P<"))


def files_for(manager: dict[str, Any]) -> list[str]:
    pats = [re.compile(p.strip("/")) for p in manager["managerFilePatterns"]]
    files = REPO.files()
    return [f for f in files if any(p.search(f) for p in pats)]


def matches(manager: dict[str, Any], file: str) -> list[dict[str, str]]:
    text = (ROOT / file).read_text(encoding="utf-8")
    out = []
    for s in manager["matchStrings"]:
        out += [m.groupdict() for m in py_regex(s).finditer(text)]
    return out


def test_regex_manager_for_pin_files() -> None:
    m = next(x for x in regex_managers() if "versions\\.env" in " ".join(x["managerFilePatterns"]))
    found = matches(m, "infra/cloudrun/versions.env")
    assert {d["depName"] for d in found} == {"quay.io/keycloak/keycloak", "ghcr.io/berriai/litellm-non_root",
                                              "adorsys/keycloak-config-cli", "postgres", "arizephoenix/phoenix"}
    assert all(d["currentDigest"].startswith("sha256:") for d in found)
    if (ROOT / "infra/supply-chain/signing.env").exists():
        assert [d["depName"] for d in matches(m, "infra/supply-chain/signing.env")] == ["ghcr.io/sigstore/cosign/cosign"]


def test_regex_manager_for_corepack_pnpm() -> None:
    m = next(x for x in regex_managers() if x.get("depNameTemplate") == "pnpm" and x["datasourceTemplate"] == "npm"
             and "Dockerfile" in " ".join(x["managerFilePatterns"]))
    files = files_for(m)
    assert set(files) >= {"apps/keelson-web/Dockerfile", "apps/merxa-web/Dockerfile"}
    want = json.loads((ROOT / "package.json").read_text())["packageManager"].split("@")[1]
    for f in ("apps/keelson-web/Dockerfile", "apps/merxa-web/Dockerfile"):
        assert [d["currentValue"] for d in matches(m, f)] == [want]


def test_every_pinned_script_image_is_managed() -> None:
    """Each name:tag@sha256 literal that check_pins sees in scripts/workflows/images.json is updatable."""
    m = next(x for x in regex_managers() if "images\\.json" in " ".join(x["managerFilePatterns"]))
    checker = check_pins.Checker(ROOT)
    checker.run()
    managed = set(files_for(m))
    seen = {(r.file, r.name) for r in checker.refs if r.digest}
    script_files = set(REPO.script_files()) | {"infra/supply-chain/images.json"}
    scripts = {(f, n) for f, n in seen if f in script_files}
    assert scripts, "no pinned script images found"
    for f, name in sorted(scripts):
        assert f in managed, f"{f} is not covered by the script-image regex manager"
        assert name in {d["depName"] for d in matches(m, f)}, (f, name)


def test_native_versions_manager() -> None:
    ms = [x for x in regex_managers() if "versions\\.psd1" in " ".join(x["managerFilePatterns"])]
    got = {x["depNameTemplate"]: matches(x, "scripts/native/versions.psd1")[0]["currentValue"] for x in ms}
    assert got == {"pnpm": "10.34.6", "litellm": "1.104.0"}


def test_local_engine_image_is_disabled() -> None:
    assert effective("keelson-engine-api", "enabled") is False
