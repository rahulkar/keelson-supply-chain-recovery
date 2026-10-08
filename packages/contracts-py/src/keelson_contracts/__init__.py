"""Keelson shared contracts.

Submodules are imported explicitly by consumers (e.g.
``from keelson_contracts.blocks import make_block``); keep this package
import-light so services only pay for what they use.

``common`` (provenance, units, ISO weeks, canonical hashing, ids) · ``blocks`` ·
``agui`` · ``runs`` · ``llm`` · ``scenario`` · ``changeset`` · ``receipts`` ·
``connectors`` · ``optimizer`` · ``scn`` (``scn/1.0`` rows + snapshot tools) · M5:
``policy`` (approval rules, CEL context, decision, quorum) · ``demo`` (demo reset log) · M6:
``signals`` · M7: ``admin`` (four-eyes change requests, users) · ``agents`` (custom specialists,
playground) · ``mapping`` (tier-A read/action mappings, safe expression grammar) · ``commit``
(per-op commit ledger).
"""

__version__ = "0.1.0"
