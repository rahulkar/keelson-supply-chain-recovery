"""Assertions over a `renovate --platform=local --dry-run=extract` JSON log (renovate_extract.sh).

    python scripts/supply-chain/tests/renovate_extract_check.py <renovate.jsonl>

Checks that Renovate can update every pin check_pins.py requires: each pinned image reference is
extracted in its file, no reference is extracted twice by different managers, the five uv projects,
the three npm projects and every pinned GitHub Action are found, nothing comes from the checker's
fixtures, and Renovate logged no errors. Exit status = number of failed checks.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "supply-chain"))
import check_pins  # noqa: E402


def load(log: Path) -> tuple[dict[str, list[dict]], list[str]]:
    package_files: dict[str, list[dict]] = {}
    errors = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("msg") == "Extracted dependencies":
            package_files = d["packageFiles"]
        if d.get("level", 0) >= 50:
            errors.append(d.get("msg", ""))
    return package_files, errors


def main(argv: list[str]) -> int:
    package_files, errors = load(Path(argv[1]))
    failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failed
        failed += not ok
        print(json.dumps({"check": name, "ok": ok, "detail": "" if ok else detail[:600]}))

    check("extraction_logged", bool(package_files), "no 'Extracted dependencies' line in the log")
    check("no_renovate_errors", not errors, "; ".join(errors))
    extracted: dict[tuple[str, str], set[str]] = defaultdict(set)  # (file, depName) -> managers
    by_ref: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    files_by_manager: dict[str, set[str]] = defaultdict(set)
    for manager, files in package_files.items():
        for pf in files:
            files_by_manager[manager].add(pf["packageFile"])
            for dep in pf.get("deps", []):
                extracted[(pf["packageFile"], dep.get("depName", ""))].add(manager)
                if dep.get("datasource") == "docker" and dep.get("currentDigest"):
                    key = (pf["packageFile"], dep["depName"], dep.get("currentValue", ""), dep["currentDigest"])
                    by_ref[key].add(manager)
    checker = check_pins.Checker(ROOT)
    checker.run()
    missing = sorted({f"{r.file}: {r.name}" for r in checker.refs
                      if r.digest and (r.file, r.name) not in extracted})
    check("every_pinned_image_is_extracted", not missing, ", ".join(missing))
    dupes = sorted(f"{k[0]}: {k[1]} ({', '.join(sorted(v))})" for k, v in by_ref.items() if len(v) > 1)
    check("no_duplicate_managers", not dupes, ", ".join(dupes))
    fixtures = sorted(f for fs in files_by_manager.values() for f in fs if f.startswith("scripts/supply-chain/tests/fixtures"))
    check("nothing_from_fixtures", not fixtures, ", ".join(fixtures))
    uv_projects = sorted(str(p.parent.relative_to(ROOT).as_posix()) + "/pyproject.toml"
                         for p in ROOT.glob("**/uv.lock") if "node_modules" not in p.parts)
    pep = sorted(files_by_manager.get("pep621", set()))
    check("uv_projects", set(uv_projects) <= set(pep), f"expected {uv_projects}, got {pep}")
    npm = files_by_manager.get("npm", set())
    want_npm = {"package.json", "apps/merxa-web/package.json", "e2e/playwright/package.json", "apps/keelson-web/package.json"}
    check("npm_projects", want_npm <= npm, f"missing {sorted(want_npm - npm)}")
    uses = []
    for wf in sorted((ROOT / ".github").glob("**/*.y*ml")):
        for line in wf.read_text(encoding="utf-8").splitlines():
            s = line.strip().removeprefix("- ").strip()
            if s.startswith("uses:") and "./" not in s:
                uses.append((wf.relative_to(ROOT).as_posix(), "/".join(s.split()[1].split("@")[0].split("/")[:2])))
    gha_missing = sorted({f"{f}: {u}" for f, u in uses if (f, u) not in extracted})
    check("github_actions", bool(uses) and not gha_missing, ", ".join(gha_missing) or ("" if uses else "no uses: found"))
    print(f"renovate extract: {failed} failed check(s)", file=sys.stderr)
    return failed


if __name__ == "__main__":
    sys.exit(main(sys.argv))
