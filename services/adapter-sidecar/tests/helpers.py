"""Shared test helpers: tool calls, the Fernhill change sets, reading back CSV rows."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from conftest import FakeKeelson, Running, key
from fastmcp import Client

TO_ITEM = {
    "op": "transfer_order.create",
    "target_ref": None,
    "expected_version": None,
    "payload": {"from_dc": "FH-RTM", "to_dc": "FH-LDS", "ship_date": "2026-10-20",
                "lines": [{"sku": "FH-PARKA-NVY-M", "qty": 60}, {"sku": "FH-PARKA-NVY-L", "qty": 40}]},
}


def csv_rows(data_dir: Path, name: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO((data_dir / name).read_text(encoding="utf-8"))))


def stock(data_dir: Path, site: str, sku: str) -> dict[str, str]:
    return next(r for r in csv_rows(data_dir, "stock.csv") if r["site"] == site and r["sku"] == sku)


def allocation(data_dir: Path, code: str) -> dict[str, str]:
    return next(r for r in csv_rows(data_dir, "store_allocations.csv") if r["code"] == code)


def alloc_item(data_dir: Path, code: str = "AL-0003", delta: int = -2) -> dict[str, Any]:
    row = allocation(data_dir, code)
    return {"op": "allocation.set_qty", "target_ref": code, "expected_version": int(row["rev"]),
            "payload": {"qty": int(row["qty"]) + delta}}


async def call(c: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    res = await c.call_tool(tool, args)
    assert isinstance(res.structured_content, dict)
    return res.structured_content


async def prepare(c: Client, items: list[dict[str, Any]], *, title: str = "test") -> dict[str, Any]:
    """create → add → validate; returns the validation result (+ ``change_set_id``)."""
    created = await call(c, "create_change_set", {"title": title, "idempotency_key": key()})
    await call(c, "add_change_set_items", {"change_set_id": created["change_set_id"], "items": items,
                                           "idempotency_key": key()})
    return await call(c, "validate_change_set", {"change_set_id": created["change_set_id"], "idempotency_key": key()})


async def apply(c: Client, keelson: FakeKeelson, validated: dict[str, Any], *, approval_ref: str = "APR-1",
                policy: bool = True, idem: str | None = None, **receipt_kw: Any) -> dict[str, Any]:
    receipt = keelson.receipt(change_set_ref=validated["change_set_id"], change_set_hash=validated["preview_hash"],
                              approval_id=approval_ref, policy=policy, **receipt_kw)
    return await call(c, "apply_change_set", {
        "change_set_id": validated["change_set_id"], "preview_hash": validated["preview_hash"],
        "approval_ref": approval_ref, "approval_receipt": receipt, "idempotency_key": idem or key()})


async def rollback(c: Client, keelson: FakeKeelson, validated: dict[str, Any], *, approval_ref: str = "APR-RB-1",
                   policy: bool = False, idem: str | None = None, **receipt_kw: Any) -> dict[str, Any]:
    receipt = keelson.receipt(change_set_ref=validated["change_set_id"], change_set_hash=validated["preview_hash"],
                              approval_id=approval_ref, policy=policy, **receipt_kw)
    return await call(c, "rollback_change_set", {
        "change_set_id": validated["change_set_id"], "approval_ref": approval_ref, "approval_receipt": receipt,
        "idempotency_key": idem or key()})


__all__ = ["Running", "TO_ITEM", "alloc_item", "allocation", "apply", "call", "csv_rows", "prepare", "rollback",
           "stock"]
