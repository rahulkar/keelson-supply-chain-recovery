"""CLI: ``python -m merxa_api.seed --profile full [--reset] [--check-story] [--no-db]``.

``--check-story`` prints the story projection of the loaded database and, computed offline
from the generated datasets, of **both** profiles (``small`` and ``full``).
``--no-db`` skips the database entirely (generation + story only).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from merxa_api.config import Settings
from merxa_api.db import create_engine
from merxa_api.logs import configure_logging
from merxa_api.migrate import migrate
from merxa_api.seed.generator import PROFILES, UnsupportedProfile, generate
from merxa_api.seed.loader import db_fingerprint, golden, seed
from merxa_api.seed.story import inputs_from_dataset, load_inputs, story_report


def _generated_story() -> dict[str, object]:
    return {p: story_report(inputs_from_dataset(generate(p))) for p in PROFILES}


async def _run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    configure_logging(settings.log_level, stream=sys.stderr)
    gs = golden(args.profile)
    report: dict[str, object] = {
        "profile": args.profile,
        "dataset_sha256": gs.fingerprint,
        "rows": gs.counts,
        "generate_ms": gs.generate_ms,
    }
    if not args.no_db:
        engine = create_engine(settings.database_url, pool_size=2)
        try:
            await migrate(engine)
            started = time.perf_counter()
            counts = await seed(engine, args.profile, reset_first=args.reset)
            report["loaded"] = bool(counts) or "already seeded (use --reset to reload)"
            report["load_ms"] = round((time.perf_counter() - started) * 1000)
            async with engine.connect() as conn:
                if args.profile == "small":
                    report["tables_sha256"] = await db_fingerprint(conn)
                if args.check_story:
                    report["story_db"] = story_report(await load_inputs(conn))
        finally:
            await engine.dispose()
    if args.check_story:
        report["story_generated"] = _generated_story()
    print(json.dumps(report, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m merxa_api.seed", description=__doc__)
    parser.add_argument("--profile", default="full", choices=PROFILES)
    parser.add_argument("--reset", action="store_true", help="wipe domain tables before loading")
    parser.add_argument("--check-story", action="store_true", help="print the story projections (both profiles)")
    parser.add_argument("--no-db", action="store_true", help="do not touch the database")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except UnsupportedProfile as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
