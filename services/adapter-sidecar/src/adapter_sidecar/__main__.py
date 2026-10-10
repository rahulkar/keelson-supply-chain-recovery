"""``python -m adapter_sidecar <command>`` (contract 122 §6.3).

* ``serve`` (default) — run the service (uvicorn, one worker).
* ``check [--config PATH]`` — validate the config (static rules), connect to the backend (a CSV
  directory is checked on a temporary copy, so a running sidecar is never disturbed), probe every
  entity SQL / view, build a dry ``scn/1.0`` snapshot into a temporary directory and run Keelson's
  two FK checks on it. Exit 0 when everything passes, 1 otherwise.
* ``snapshot --out DIR [--config PATH]`` — the same dry export, copied to ``DIR`` (Parquet parts per
  entity plus ``manifest.json``) for debugging.
* ``hash-key`` — print the sha256 of a key read from stdin (or generate a new 48-character key
  when stdin is empty) — record or compare keys without storing them.

``check`` / ``snapshot`` use the deployment environment (``DATA_DIR``, ``*_env`` secrets) but a
throw-away state store and snapshot directory; ``PUBLIC_URL`` defaults to ``http://sidecar.check``.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import secrets
import shutil
import string
import sys
import tempfile
from pathlib import Path
from typing import Any

from adapter_sidecar.errors import ConfigError

_ALNUM = string.ascii_letters + string.digits


def _settings(config: str | None, work: Path) -> Any:
    from adapter_sidecar.settings import Settings

    env = dict(os.environ)
    if config:
        env["SIDECAR_CONFIG"] = config
    env.setdefault("PUBLIC_URL", "http://sidecar.check")
    env["MCP_ALLOWED_HOSTS"] = ""
    env.pop("MCP_RESOURCE", None)
    env["STATE_DB_URL"] = f"sqlite+aiosqlite:///{work / 'state.db'}"
    env["SNAPSHOT_DIR"] = str(work / "snapshots")
    env["DEMO_MODE"] = "0"
    return Settings.from_env(env)


async def _dry_export(config: str | None, out: Path | None) -> int:
    import pyarrow.parquet as pq

    from adapter_sidecar.config import load_config
    from adapter_sidecar.scn_export import fk_problems
    from adapter_sidecar.service import Sidecar

    with tempfile.TemporaryDirectory(prefix="sidecar-check-") as tmp:
        work = Path(tmp)
        settings = _settings(config, work)
        cfg = load_config(settings.config_path)
        print(f"config ok: {cfg.system.name} ({cfg.backend.kind}); {len(cfg.tables)} table(s), "
              f"{len(cfg.entities)} mapped entities, ops {sorted(cfg.ops) or '-'}; writable={cfg.writable}")
        if cfg.backend.kind == "csv" and settings.data_dir is not None:
            copy = work / "data"
            copy.mkdir()
            for table in cfg.tables.values():
                src = settings.data_dir / (table.file or "")
                if src.is_file():
                    shutil.copyfile(src, copy / src.name)
            settings = dataclasses.replace(settings, data_dir=copy)
        sidecar = Sidecar(settings, cfg)
        try:
            await sidecar.start()
            manifest = await sidecar.snapshots.open_snapshot(list(_entities()), created_by="check")
            rows: dict[str, list[dict[str, Any]]] = {}
            for entity in ("item", "location", "po_line", "inventory_position"):
                for f in sorted((settings.snapshot_dir / manifest["snapshot_id"] / entity).glob("part-*.parquet")):
                    rows.setdefault(entity, []).extend(pq.read_table(f).to_pylist())
            for entity, info in manifest["entities"].items():
                mapped = "mapped" if entity in cfg.entities else "empty"
                print(f"  {entity:<20} {info['rows']:>8} row(s) in {info['parts']} part(s)  [{mapped}]")
            problems = fk_problems(rows)
            for p in problems:
                print(f"FK check failed: {p}")
            if not problems:
                print("FK checks ok: po_line.sku_code → item(sku); inventory_position.location → location")
            if out is not None:
                src = settings.snapshot_dir / manifest["snapshot_id"]
                out.mkdir(parents=True, exist_ok=True)
                for entity_dir in src.iterdir():
                    shutil.copytree(entity_dir, out / entity_dir.name, dirs_exist_ok=True)
                (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                print(f"snapshot written to {out}")
            return 1 if problems else 0
        finally:
            await sidecar.close()


def _entities() -> tuple[str, ...]:
    from keelson_contracts.scn import SCN_ENTITIES

    return SCN_ENTITIES


def _hash_key() -> int:
    raw = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
    if not raw:
        raw = "".join(secrets.choice(_ALNUM) for _ in range(48))
        print(f"new key: {raw}")
    if len(raw) < 32:
        print("warning: keys shorter than 32 characters are refused by the sidecar", file=sys.stderr)
    print(f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adapter_sidecar", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    check = sub.add_parser("check")
    check.add_argument("--config")
    snap = sub.add_parser("snapshot")
    snap.add_argument("--config")
    snap.add_argument("--out", required=True)
    sub.add_parser("hash-key")
    args = parser.parse_args(argv)
    if args.cmd in (None, "serve"):
        from adapter_sidecar.app import run

        run()
        return 0
    if args.cmd == "hash-key":
        return _hash_key()
    try:
        return asyncio.run(_dry_export(args.config, Path(args.out) if args.cmd == "snapshot" else None))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
