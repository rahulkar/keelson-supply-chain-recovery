"""upgrade_gate.py: direct and transitive gated changes, replay-affecting files, safe label parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_tree

import upgrade_gate as ug


def uv(**pkgs: str) -> str:
    return "version = 1\n\n" + "".join(f'[[package]]\nname = "{k.replace("_", "-")}"\nversion = "{v}"\n\n'
                                       for k, v in pkgs.items())


def pnpm(**pkgs: str) -> str:
    body = "".join(f"  '{k}@{v}':\n    resolution: {{integrity: x}}\n\n" for k, v in pkgs.items())
    return f"lockfileVersion: '9.0'\n\nimporters:\n  .: {{}}\n\npackages:\n\n{body}snapshots:\n\n  zod@99.0.0: {{}}\n"


ENGINE = dict(google_adk="2.11.0", google_genai="2.28.0", litellm="1.104.0", fastapi="0.141.1",
              opentelemetry_api="1.42.1", pytest="9.1.1")
BASE = {
    "services/engine/uv.lock": uv(**ENGINE),
    "services/optimizer/uv.lock": uv(ortools="9.15.6755", protobuf="6.33.6"),
    "pnpm-lock.yaml": pnpm(**{"@ag-ui/core": "1.0.2", "motion": "13.5.1"}),
    "models.lock": "models:\n  investigate: gemini-flash\n",
    "infra/litellm/config.yaml": "litellm_settings: {}\n",
    "infra/cloudrun/versions.env": f"LITELLM_IMAGE=ghcr.io/berriai/litellm-non_root:1.104.0@sha256:{'a' * 64}\n",
}


@pytest.fixture()
def trees(tmp_path: Path) -> tuple[Path, Path]:
    return write_tree(tmp_path / "base", BASE), write_tree(tmp_path / "head", BASE)


def run(trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, labels: object = None) -> tuple[int, str]:
    if labels is None:
        monkeypatch.delenv("PR_LABELS", raising=False)
    else:
        monkeypatch.setenv("PR_LABELS", labels if isinstance(labels, str) else json.dumps(labels))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    out = tmp_path / "summary.md"
    rc = ug.main(["--base-dir", str(trees[0]), "--head-dir", str(trees[1]), "--summary", str(out)])
    return rc, out.read_text(encoding="utf-8")


def test_no_change_passes(trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rc, md = run(trees, monkeypatch, tmp_path)
    assert rc == 0 and "nothing to attest" in md


def test_dev_tooling_bump_passes(trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    write_tree(trees[1], {"services/engine/uv.lock": uv(**dict(ENGINE, pytest="9.2.0")),
                          "pnpm-lock.yaml": pnpm(**{"@ag-ui/core": "1.0.2", "motion": "13.6.0"})})
    rc, md = run(trees, monkeypatch, tmp_path)
    assert rc == 0
    assert "| services/engine/uv.lock | pytest | 9.1.1 | 9.2.0 | - |" in md


@pytest.mark.parametrize("change,needs", [
    ({"services/engine/uv.lock": uv(**dict(ENGINE, litellm="1.105.0"))}, {"spike-verified", "replays-rerecorded"}),
    ({"services/engine/uv.lock": uv(**dict(ENGINE, google_genai="2.29.0"))}, {"spike-verified", "replays-rerecorded"}),
    ({"services/engine/uv.lock": uv(**dict(ENGINE, opentelemetry_api="1.43.0"))}, {"spike-verified"}),
    ({"services/engine/uv.lock": uv(**dict(ENGINE, mcp="2.4.0"))}, {"spike-verified"}),
    ({"services/signal-hub/uv.lock": uv(fastmcp="4.1.0")}, {"spike-verified"}),
    ({"pnpm-lock.yaml": pnpm(**{"@ag-ui/core": "1.1.0", "motion": "13.5.1"})}, {"spike-verified"}),
    ({"models.lock": "models:\n  investigate: gemini-flash-002\n"}, {"replays-rerecorded"}),
    ({"infra/litellm/config.yaml": "litellm_settings: {drop_params: true}\n"}, {"replays-rerecorded"}),
    ({"infra/cloudrun/versions.env": f"LITELLM_IMAGE=ghcr.io/berriai/litellm-non_root:1.105.0@sha256:{'b' * 64}\n"},
     {"spike-verified"}),
])
def test_gated_changes_need_labels(trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                   change: dict[str, str], needs: set[str]) -> None:
    write_tree(trees[1], change)
    rc, md = run(trees, monkeypatch, tmp_path)
    assert rc == 1 and "**Blocked**" in md
    for label in needs:
        assert f"`{label}`" in md.split("\n")[2]
    rc, md = run(trees, monkeypatch, tmp_path, labels=sorted(needs))
    assert rc == 0 and "**Passed**" in md


def test_transitive_change_is_caught_without_direct_bump(trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Path) -> None:
    """A lock-file maintenance run that only moves google-genai under google-adk is still gated."""
    write_tree(trees[1], {"services/engine/uv.lock": uv(**dict(ENGINE, google_genai="2.30.1"))})
    rep = ug.evaluate(ug.dir_paths(trees[1]), ug.dir_reader(trees[0]), ug.dir_reader(trees[1]))
    assert rep.required() == {"spike-verified", "replays-rerecorded"}
    assert ug.Change("services/engine/uv.lock", "google-genai", "2.28.0", "2.30.1") in rep.changes


def test_opentelemetry_outside_the_engine_is_not_gated(trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path) -> None:
    write_tree(trees[1], {"services/signal-hub/uv.lock": uv(opentelemetry_api="1.50.0")})
    assert run(trees, monkeypatch, tmp_path)[0] == 0


def test_new_and_removed_lockfiles(trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (trees[1] / "services/optimizer/uv.lock").unlink()
    write_tree(trees[1], {"services/adapter-sidecar/uv.lock": uv(mcp="2.3.0")})
    rep = ug.evaluate(ug.dir_paths(trees[0]) + ug.dir_paths(trees[1]), ug.dir_reader(trees[0]), ug.dir_reader(trees[1]))
    assert ug.Change("services/optimizer/uv.lock", "ortools", "9.15.6755", "-") in rep.changes
    assert rep.spike == ["mcp in services/adapter-sidecar/uv.lock"]


@pytest.mark.parametrize("raw,expected", [
    ('["spike-verified", "replays-rerecorded"]', {"spike-verified", "replays-rerecorded"}),
    ('[{"name": "spike-verified"}]', {"spike-verified"}),
    ('["a\\"; rm -rf / #", "$(touch /tmp/pwned)", "`id`"]', {'a"; rm -rf / #', "$(touch /tmp/pwned)", "`id`"}),
    ("not json", set()),
    ('{"name": "spike-verified"}', set()),
    ("", set()),
    ('[1, null, ["x"]]', set()),
])
def test_labels_are_parsed_as_json_only(raw: str, expected: set[str]) -> None:
    assert ug.parse_labels(raw) == expected


def test_hostile_labels_and_versions_are_inert_in_the_summary(trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    write_tree(trees[1], {"services/engine/uv.lock": uv(**dict(ENGINE, litellm="1.105.0|`x`<b>"))})
    rc, md = run(trees, monkeypatch, tmp_path, labels='["$(id)", "spike-verified`"]')
    assert rc == 1
    assert "1.105.0\\|'x'&lt;b&gt;" in md and "<b>" not in md


def test_summary_checklists(trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    write_tree(trees[1], {"services/engine/uv.lock": uv(**dict(ENGINE, google_adk="2.12.0"))})
    _, md = run(trees, monkeypatch, tmp_path)
    assert "### `spike-verified` checklist" in md and "S1 live" in md and "S2 connectors" in md and "S3 durability" in md
    assert "### `replays-rerecorded` checklist" in md and "e2e/api_m7_offline.py" in md
    assert "| services/engine/uv.lock | google-adk | 2.11.0 | 2.12.0 | spike-verified, replays-rerecorded |" in md


def test_invalid_base_ref_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    assert ug.main(["--base=--upload-pack=evil"]) == 2
    assert "invalid --base ref" in capsys.readouterr().err
