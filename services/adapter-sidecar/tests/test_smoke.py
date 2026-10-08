"""The Fernhill sidecar end to end over MCP: snapshot, change set, apply, change feed, rollback."""

from __future__ import annotations

from conftest import FakeKeelson
from helpers import TO_ITEM, Running, alloc_item, allocation, apply, call, prepare, rollback, stock


async def test_end_to_end_csv(fernhill: Running, keelson: FakeKeelson) -> None:
    d = fernhill.data_dir
    assert d is not None
    before_rtm = stock(d, "FH-RTM", "FH-PARKA-NVY-M")
    before_alloc = allocation(d, "AL-0003")
    async with fernhill.client() as c:
        names = [t.name for t in await c.list_tools()]
        assert names[0] == "get_system_info" and "apply_change_set" in names
        snap = await call(c, "scn.snapshot_open", {"entities": ["location", "item", "allocation"]})
        assert snap["watermark"] == "j:0" and snap["entities"]["allocation"]["rows"] > 0
        validated = await prepare(c, [TO_ITEM, alloc_item(d)])
        assert validated["status"] == "validated", validated
        applied = await apply(c, keelson, validated)
        assert applied["status"] == "applied", applied
        changes = await call(c, "scn.changes_since", {"watermark": snap["watermark"]})
        assert changes["changes"] and not changes.get("full_resync_required")
        rb = await rollback(c, keelson, validated)
        assert rb["status"] == "rolled_back", rb
    after_rtm = stock(d, "FH-RTM", "FH-PARKA-NVY-M")
    assert after_rtm["reserved"] == before_rtm["reserved"]
    assert allocation(d, "AL-0003")["qty"] == before_alloc["qty"]
