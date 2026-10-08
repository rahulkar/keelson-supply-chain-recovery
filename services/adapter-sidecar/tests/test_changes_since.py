"""Tier C change feed: own applies listed with versions; drift, reset, pruning and unknown
watermarks ask for a full resync; paging; Keelson's ``ChangesSince`` parses every answer."""

from __future__ import annotations

import pytest
from conftest import ADMIN_KEY, FakeKeelson, Running
from fastmcp.exceptions import ToolError
from helpers import TO_ITEM, alloc_item, apply, call, prepare
from keelson_contracts.scn import ChangesSince
from sqlalchemy import text

from adapter_sidecar import journal
from adapter_sidecar.db import begin_write


async def test_own_applies_are_listed(fernhill: Running, keelson: FakeKeelson) -> None:
    d = fernhill.data_dir
    assert d is not None
    async with fernhill.client() as c:
        snap = await call(c, "scn.snapshot_open", {"entities": ["allocation"]})
        validated = await prepare(c, [TO_ITEM, alloc_item(d, "AL-0003")])
        await apply(c, keelson, validated)
        res = await call(c, "scn.changes_since", {"watermark": snap["watermark"]})
    parsed = ChangesSince.model_validate(res)
    assert parsed.requires_sync and not parsed.full_resync_required and not parsed.has_more
    keys = {(ch.entity, ch.key) for ch in parsed.changes}
    assert ("allocation", "AL-0003") in keys
    assert ("inventory_position", "dc/FH-RTM/FH-PARKA-NVY-M") in keys
    assert ("transfer_order_line", "TR-00002#1") in keys and ("transfer_order_line", "TR-00002") in keys
    alloc = next(ch for ch in parsed.changes if ch.key == "AL-0003")
    assert alloc.version == 2 and alloc.op == "upsert" and alloc.at is not None
    again = ChangesSince.model_validate(
        (await fernhill.sidecar.changes_since(parsed.next_watermark)))
    assert not again.requires_sync and again.next_watermark == parsed.next_watermark


async def test_reset_and_unknown_watermarks_resync(fernhill: Running) -> None:
    async with fernhill.client() as c:
        snap = await call(c, "scn.snapshot_open", {"entities": ["allocation"]})
        async with fernhill.http() as http:
            r = await http.post("/v1/admin/reset", headers={"X-Admin-Key": ADMIN_KEY})
            assert r.status_code == 200
        reset = await call(c, "scn.changes_since", {"watermark": snap["watermark"]})
        assert reset["full_resync_required"] is True
        future = await call(c, "scn.changes_since", {"watermark": "j:999"})
        assert future["full_resync_required"] is True and future["next_watermark"] == reset["next_watermark"]
        for bad in ("ox:1", "j:", "j:-1", "1", "j:1x"):
            with pytest.raises(ToolError, match="invalid_watermark"):
                await c.call_tool("scn.changes_since", {"watermark": bad})


async def test_paging_and_retention(fernhill: Running) -> None:
    state = fernhill.sidecar.state
    async with begin_write(state) as conn:
        for i in range(journal.MAX_CHANGES + 5):
            await conn.execute(text("INSERT INTO journal (at, kind, entity, op, key, version) VALUES "
                                    "('2026-10-08T00:00:00Z', 'change', 'allocation', 'upsert', :k, 1)"),
                               {"k": f"AL-{i}"})
    first = ChangesSince.model_validate(await fernhill.sidecar.changes_since("j:0"))
    assert first.has_more and len(first.changes) == journal.MAX_CHANGES and first.next_watermark == "j:2000"
    rest = ChangesSince.model_validate(await fernhill.sidecar.changes_since(first.next_watermark))
    assert not rest.has_more and len(rest.changes) == 5
    async with begin_write(state) as conn:
        assert await journal.prune(conn, 100) == journal.MAX_CHANGES + 5 - 100
    pruned = ChangesSince.model_validate(await fernhill.sidecar.changes_since("j:3"))
    assert pruned.full_resync_required  # rows after j:3 were dropped
    recent = ChangesSince.model_validate(await fernhill.sidecar.changes_since("j:1990"))
    assert not recent.full_resync_required and len(recent.changes) == 15
