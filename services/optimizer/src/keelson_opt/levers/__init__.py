"""Lever catalogue (``GET /v1/levers``): the five v1 levers of ``80`` §3, with the JSON Schema
of their ``params`` and the change-set ops their actions use (``keelson_contracts``)."""

from __future__ import annotations

from typing import Final

from keelson_contracts.optimizer import LeverInfo, lever_catalogue

REALLOCATE_INVENTORY: Final = "reallocate_inventory"


def catalogue() -> list[LeverInfo]:
    return lever_catalogue()
