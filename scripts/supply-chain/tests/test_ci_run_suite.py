"""ci_run_suite.py (local == CI suite runs) and matrix.py (GitHub matrices), both from images.json.

ci_run_suite.py is exercised with --dry-run (the exact docker commands) and with a fake `docker` on
PATH that records every call (exit codes, health wait, cleanup on success and on harness failure).
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import pytest
from conftest import D, ROOT, VALKEY, write_tree

import ci_run_suite
import matrix
import registry as reg

UV_PY = f"ghcr.io/astral-sh/uv:0.12.23-python3.14-trixie-slim@{D['e']}"
REGISTRY = {
    "schema": "keelson.images/1",
    "images": [
        {"name": "api", "dockerfile": "services/api/Dockerfile", "context": ".", "target": "runtime", "release": True,
         "deploy_var": "IMAGE_API",
         "suites": [{"id": "api-db", "target": "test", "db": "pg-core", "valkey": True, "memory": "2g",
                     "env": {"Z_DSN": "postgresql://postgres:test@e1-test-pg:5432/postgres", "A_FLAG": "1"},
                     "cmd": ["python", "-m", "pytest", "-q", "tests"],
                     "deselect": [{"nodeid": "tests/test_x.py::test_flaky", "reason": "known environment failure"}]},
                    {"id": "api-plain", "target": "test"}]},
        {"name": "web", "dockerfile": "apps/web/Dockerfile", "context": ".", "target": "runtime", "release": True,
         "suites": [{"id": "web-test", "kind": "build", "target": "test"}]},
        {"name": "api-overlay", "dockerfile": "infra/cloudrun/images/api.Dockerfile", "context": ".", "target": None,
         "release": True, "overlay_of": "api", "overlay_arg": "API_IMAGE", "domain_overlay": True},
        {"name": "pg-core", "dockerfile": "infra/postgres/core/Dockerfile", "context": "infra/postgres/core",
         "target": None, "release": False},
    ],
    "suites": [{"id": "lib", "image": UV_PY, "workdir": "packages/lib", "memory": "1g",
                "env": {"UV_LINK_MODE": "copy"}, "cmd": ["sh", "-c", "uv sync --frozen && uv run pytest -q"]}],
}
TREE = {
    "infra/supply-chain/images.json": json.dumps(REGISTRY, indent=1),
    "compose.yaml": f"name: t\nservices:\n  valkey:\n    # cache\n    image: {VALKEY}\n    command: [valkey-server]\n",
}

FAKE_DOCKER = r"""#!/bin/sh
# Records argv (one call per line). `run --rm` exits $FAKE_DOCKER_RUN_RC; a call whose arguments
# contain $FAKE_DOCKER_FAIL fails; health checks report healthy; image inspect prints a CMD.
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if [ -n "${FAKE_DOCKER_FAIL:-}" ]; then
  case "$*" in *"$FAKE_DOCKER_FAIL"*) echo "fake failure: $*" >&2; exit 1 ;; esac
fi
case "$1 $2" in
  "inspect -f") echo healthy; exit 0 ;;
  "image inspect") echo '["python","-m","pytest","-q"]'; exit 0 ;;
  "run --rm") exit "${FAKE_DOCKER_RUN_RC:-0}" ;;
esac
exit 0
"""


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    return write_tree(tmp_path / "repo", TREE)


@pytest.fixture()
def docker_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
    (bindir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    return log


def dry(tree: Path, suite: str, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, list[list[str]], str]:
    rc = ci_run_suite.main([suite, "--root", str(tree), "--dry-run", *extra])
    out = capsys.readouterr()
    cmds = [shlex.split(ln[2:]) for ln in out.out.splitlines() if ln.startswith("+ ")]
    return rc, cmds, out.err


def test_list_shows_every_suite(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ci_run_suite.main(["--list", "--root", str(tree)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [ln.split()[0] for ln in lines] == ["api-db", "api-plain", "web-test", "lib"]
    assert lines[0].split()[1:] == ["run", "api:test", "db=pg-core", "valkey"]
    assert lines[2].split()[1:3] == ["build", "web:test"] and lines[3].split()[2] == UV_PY


def test_unknown_suite_and_invalid_registry(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ci_run_suite.main(["nope", "--root", str(tree)]) == 2
    assert "unknown suite 'nope'" in capsys.readouterr().err
    write_tree(tree, {"infra/supply-chain/images.json": json.dumps({"schema": "x", "images": []})})
    assert ci_run_suite.main(["api-db", "--root", str(tree)]) == 2
    assert "schema must be 'keelson.images/1'" in capsys.readouterr().err


def test_db_suite_dry_run_builds_starts_services_runs_and_cleans_up(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, cmds, _ = dry(tree, "api-db", capsys)
    assert rc == 0
    build, net, pg_build, pg_run, valkey_run, run, *cleanup = cmds
    assert build == ["docker", "build", "-f", "services/api/Dockerfile", "--target", "test", "-t", "keelson-ci/api:test", "."]
    name = net[3].removesuffix("_net")
    assert net[:3] == ["docker", "network", "create"] and name.startswith("keelson_ci_api_db_")
    assert pg_build == ["docker", "build", "-q", "-f", "infra/postgres/core/Dockerfile", "-t", ci_run_suite.PG_IMAGE,
                        "infra/postgres/core"]
    assert pg_run[pg_run.index("--network-alias") + 1] == "e1-test-pg"
    assert "--tmpfs" in pg_run and "fsync=off" in pg_run and f"{name}_pg" in pg_run

    def mib(size: str) -> int:
        return int(size[:-1]) * (1024 if size.endswith("g") else 1)
    # The tmpfs data directory is charged to the container's memory cgroup: the cap must hold the full tmpfs
    # plus the server (engine-runtime measured ~360 MiB of data + ~170 MiB of server on the CI settings).
    tmpfs = pg_run[pg_run.index("--tmpfs") + 1]
    assert tmpfs.startswith("/var/lib/postgresql:size=")
    assert mib(pg_run[pg_run.index("--memory") + 1]) >= mib(tmpfs.split("size=")[1]) + 256
    assert valkey_run[valkey_run.index("--network-alias") + 1] == "e1-test-valkey" and VALKEY in valkey_run
    assert valkey_run[valkey_run.index("--memory") + 1] == "64m"
    # the suite: memory cap, env sorted, the private network, the cmd + deselected node ids
    assert run[:6] == ["docker", "run", "--rm", "--name", name, "--memory"] and run[6] == "2g"
    envs = [run[i + 1] for i, a in enumerate(run) if a == "-e"]
    assert envs == ["PYTHONDONTWRITEBYTECODE=1", "A_FLAG=1", "Z_DSN=postgresql://postgres:test@e1-test-pg:5432/postgres"]
    assert run[run.index("--network") + 1] == f"{name}_net"
    tail = run[run.index("keelson-ci/api:test") + 1:]
    assert tail == ["python", "-m", "pytest", "-q", "tests", "--deselect", "tests/test_x.py::test_flaky"]
    # cleanup in reverse order of creation: valkey, pg, network
    assert cleanup == [["docker", "rm", "-fv", f"{name}_valkey"], ["docker", "rm", "-fv", f"{name}_pg"],
                       ["docker", "network", "rm", f"{name}_net"]]


def test_no_build_and_image_override(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, cmds, _ = dry(tree, "api-db", capsys, "--no-build")
    assert rc == 0 and not any(c[1] == "build" for c in cmds)
    rc, cmds, _ = dry(tree, "api-plain", capsys, "--image", "ghcr.io/x/api@sha256:" + "1" * 64)
    assert rc == 0 and [c[1] for c in cmds] == ["run"]
    assert cmds[0][-1] == "<image CMD>" and "--network" not in cmds[0]


def test_build_suite_only_builds(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, cmds, _ = dry(tree, "web-test", capsys)
    assert rc == 0
    assert cmds == [["docker", "build", "-f", "apps/web/Dockerfile", "--target", "test", "-t", "keelson-ci/web:test", "."]]


def test_tool_suite_mounts_the_repository_read_only(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, cmds, _ = dry(tree, "lib", capsys)
    assert rc == 0 and len(cmds) == 1
    run = cmds[0]
    assert run[run.index("-v") + 1] == f"{tree.resolve().as_posix()}:/work:ro"
    assert run[run.index("-w") + 1] == "/work/packages/lib" and run[run.index("--memory") + 1] == "1g"
    assert run[run.index(UV_PY) + 1:] == ["sh", "-c", "uv sync --frozen && uv run pytest -q"]


def test_real_run_returns_the_suite_status_and_cleans_up(tree: Path, docker_log: Path,
                                                         monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("FAKE_DOCKER_RUN_RC", "3")
    assert ci_run_suite.main(["api-db", "--root", str(tree), "--no-build"]) == 3
    assert "suite api-db: FAIL (3)" in capsys.readouterr().err
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert calls[0].startswith("network create keelson_ci_api_db_")
    assert any(c.startswith("inspect -f {{.State.Health.Status}} keelson_ci_api_db_") for c in calls)
    assert calls[-3].startswith("rm -fv keelson_ci_api_db_") and calls[-3].endswith("_valkey")
    assert calls[-2].endswith("_pg") and calls[-1].startswith("network rm keelson_ci_api_db_")
    # without a cmd the image's own CMD runs
    docker_log.unlink()
    monkeypatch.setenv("FAKE_DOCKER_RUN_RC", "0")
    assert ci_run_suite.main(["api-plain", "--root", str(tree), "--no-build"]) == 0
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert calls[0] == "image inspect -f {{json .Config.Cmd}} keelson-ci/api:test"
    assert calls[1].endswith("keelson-ci/api:test python -m pytest -q")


def test_harness_failure_is_99_and_still_cleans_up(tree: Path, docker_log: Path, monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("FAKE_DOCKER_FAIL", "--network-alias e1-test-valkey")
    assert ci_run_suite.main(["api-db", "--root", str(tree), "--no-build"]) == ci_run_suite.HARNESS_FAILED
    err = capsys.readouterr().err
    assert "fake failure" in err and "FAIL (99)" in err
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert not any(c.startswith("run --rm") for c in calls)
    assert calls[-1].startswith("network rm ") and any(c.startswith("rm -fv ") and c.endswith("_pg") for c in calls)


def test_keep_leaves_containers(tree: Path, docker_log: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ci_run_suite.main(["api-db", "--root", str(tree), "--no-build", "--keep"]) == 0
    assert "kept (--keep): docker network rm keelson_ci_api_db_" in capsys.readouterr().err
    assert not any(c.startswith(("rm -fv", "network rm")) for c in docker_log.read_text(encoding="utf-8").splitlines())


def test_compose_image_reads_the_pinned_service_image(tree: Path) -> None:
    assert ci_run_suite.compose_image(tree, "valkey") == VALKEY
    with pytest.raises(ci_run_suite.HarnessError):
        ci_run_suite.compose_image(tree, "pg-core")


# ───────────────────────────── matrix.py ─────────────────────────────
def test_matrices(tree: Path) -> None:
    m = matrix.matrices(reg.load(tree))
    assert [r["suite"] for r in m["python"]] == ["api-db", "api-plain", "lib"]
    assert m["python"][0] == {"suite": "api-db", "image": "api", "dockerfile": "services/api/Dockerfile", "context": ".",
                              "target": "test", "db": "pg-core"}
    assert m["python"][2] == {"suite": "lib", "image": "", "dockerfile": "", "context": "", "target": "", "db": ""}
    assert m["web"] == [{"suite": "web-test", "image": "web", "dockerfile": "apps/web/Dockerfile", "context": ".",
                         "target": "test"}]
    assert [r["name"] for r in m["images"]] == ["api", "web"]
    assert [(r["name"], r["overlay_of"], r["overlay_arg"]) for r in m["overlays"]] == [("api-overlay", "api", "API_IMAGE")]
    assert m["images"][0]["release_key"] == "API" and m["overlays"][0]["release_key"] == "API_OVERLAY"


def test_matrix_github_output(tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                              capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "gh-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert matrix.main(["--root", str(tree), "--github-output"]) == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert [ln.split("=", 1)[0] for ln in lines] == ["python", "web", "images", "overlays"]
    assert json.loads(lines[1].split("=", 1)[1])[0]["suite"] == "web-test"
    monkeypatch.delenv("GITHUB_OUTPUT")
    assert matrix.main(["--root", str(tree), "--github-output"]) == 2
    assert "GITHUB_OUTPUT is not set" in capsys.readouterr().err


def test_real_registry_suites_are_runnable() -> None:
    r = reg.load(ROOT)
    for s in r.suites:
        if s.image:
            assert s.target, s.id
        else:
            assert s.tool_image and s.workdir and (ROOT / s.workdir).is_dir(), s.id
        if s.db == "pg-core":
            assert any(e.endswith("@e1-test-pg:5432/postgres") for e in s.env.values()), s.id
    assert ci_run_suite.compose_image(ROOT, "valkey").startswith("valkey/valkey:")
    assert all(reg.PINNED_REF_RE.match(s.tool_image or "") for s in r.tool_suites)
