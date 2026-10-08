"""Write primitives: the only way a change set changes the system (contract 122 §5.2).

An op is a list of declarative steps over configured tables — ``require`` (precondition on one
row), ``update`` (set / add columns of one existing row), ``insert`` (one new row) and
``adjust`` (add to integer columns, optionally inserting the row first) — each optionally
repeated ``for_each`` element of a payload list. Every statement is generated here with **bound
parameters** and quoted, config-validated identifiers; there is no free SQL in writes.

:class:`Executor` runs the items of a change set **in order, in one backend transaction**, each
item inside a savepoint: ``validate`` runs them and rolls everything back (so later items see the
effects of earlier ones and the preview is exact); ``apply`` runs the same code and commits only
if every item succeeded. Per item:

1. the target row (``target``) is locked; ``check_version`` compares its version column with the
   item's ``expected_version`` (``version_mismatch``); a target table without a version column is
   compared with the before-image captured at validation instead (``row_changed``);
2. a generated code (``code``) is allocated — never reusing one that exists in the table or that
   this sidecar ever issued;
3. every ``require`` step is checked before anything changes;
4. the mutating steps run in order; each changed row is checked against the table's invariants;
5. units and value (``units``) are computed for the auto-commit caps and the preview totals.

Every mutation is recorded as an **effect** ``{kind, table, key, before, after}`` (full row
images): rollback replays them backwards, the change feed and the audit log are derived from them.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from adapter_sidecar.backends import WriteTxn
from adapter_sidecar.backends.staging import canonical_json, check_date, to_json_value
from adapter_sidecar.config import (
    CODE_FORMAT_RE,
    AdjustStep,
    CodeSpec,
    ColumnType,
    InsertStep,
    OpSpec,
    RequireStep,
    SidecarConfig,
    TableSpec,
    UpdateStep,
)
from adapter_sidecar.db import is_sqlite, quote_ident
from adapter_sidecar.templates import parse_ref, render, resolve

Mode = Literal["validate", "apply"]


class StepFailure(Exception):  # noqa: N818 - an item outcome, not a crash
    def __init__(self, code: str, detail: str, *, actual_version: int | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.actual_version = actual_version


@dataclass(frozen=True)
class ItemSpec:
    seq: int
    op: str
    target_ref: str | None
    expected_version: int | None
    payload: dict[str, Any]
    validated_before: Any = None
    """The target row image recorded by validation (compared at apply when there is no version)."""


@dataclass
class Effect:
    item_seq: int
    kind: Literal["update", "insert", "delete"]
    table: str
    key: dict[str, Any]
    before: dict[str, Any] | None
    after: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {"item_seq": self.item_seq, "kind": self.kind, "table": self.table, "key": self.key,
                "before": self.before, "after": self.after}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Effect:
        return cls(int(d["item_seq"]), d["kind"], str(d["table"]), dict(d["key"]), d.get("before"), d.get("after"))


@dataclass
class ItemResult:
    seq: int
    op: str
    target_ref: str | None
    expected_version: int | None
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    reason: str | None = None
    actual_version: int | None = None
    before: Any = None
    after: Any = None
    effects: list[Effect] = field(default_factory=list)
    result_ref: str | None = None
    units: int = 0
    value_cents: int = 0

    def conflict(self) -> dict[str, Any]:
        return {"seq": self.seq, "target_ref": self.target_ref, "expected_version": self.expected_version,
                "actual_version": self.actual_version, "reason": self.reason or "conflict"}


def key_text(table: TableSpec, key: Mapping[str, Any]) -> str:
    return "|".join(str(key.get(k)) for k in table.key)


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StepFailure("invalid_value", f"{what} must be an integer, got {value!r:.40}")
    return value


def coerce(kind: ColumnType, value: Any, what: str) -> Any:
    """A rendered template value → the column's JSON image value (``StepFailure`` if it does not fit)."""
    if value is None:
        return None
    if kind == "text":
        if isinstance(value, str):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    elif kind == "int":
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif kind == "real":
        if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
    elif kind == "date":
        if isinstance(value, str):
            try:
                return check_date(value)
            except ValueError as exc:
                raise StepFailure("invalid_value", f"{what}: {exc}") from None
    elif kind == "bool":
        if isinstance(value, bool):
            return value
    else:  # json
        return value
    raise StepFailure("invalid_value", f"{what} must be {kind}, got {value!r:.40}")


class Executor:
    def __init__(self, cfg: SidecarConfig, txn: WriteTxn, *, mode: Mode, change_set: Mapping[str, Any],
                 code_floors: Mapping[str, int] | None = None) -> None:
        self.cfg = cfg
        self.txn = txn
        self.conn = txn.conn
        self.mode = mode
        self.change_set = {"id": change_set.get("id"), "code": change_set.get("code"), "title": change_set.get("title")}
        self.sqlite = is_sqlite(self.conn)
        self.code_floors = dict(code_floors or {})
        self.codes: dict[str, int] = {}
        """Code counter name → last number allocated in this run (persisted by the caller)."""
        self._provisional: set[str] = set()

    # ───────────────────────────── SQL ─────────────────────────────

    def _q(self, name: str) -> str:
        return quote_ident(self.conn, name)

    def _bind(self, kind: ColumnType, value: Any) -> Any:
        if value is None:
            return None
        if kind == "json":
            return canonical_json(value)
        if kind == "bool":
            return int(bool(value)) if self.sqlite else bool(value)
        if kind == "date" and not self.sqlite:
            return dt.date.fromisoformat(str(value))
        return value

    def _where(self, t: TableSpec, key: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        types = t.column_types
        clauses = [f"{self._q(k)} = :k{i}" for i, k in enumerate(t.key)]
        return " AND ".join(clauses), {f"k{i}": self._bind(types[k], key[k]) for i, k in enumerate(t.key)}

    async def select(self, table: str, key: Mapping[str, Any], *, lock: bool) -> dict[str, Any] | None:
        t = self.cfg.tables[table]
        types = t.column_types
        cols = list(types)
        where, params = self._where(t, key)
        sql = f"SELECT {', '.join(self._q(c) for c in cols)} FROM {self._q(table)} WHERE {where}"
        if lock and not self.sqlite:
            sql += " FOR UPDATE"
        row = (await self.conn.execute(text(sql), params)).first()
        if row is None:
            return None
        return {c: to_json_value(types[c], v) for c, v in zip(cols, row, strict=True)}

    async def _update_row(self, table: str, key: Mapping[str, Any], new: Mapping[str, Any]) -> None:
        t = self.cfg.tables[table]
        types = t.column_types
        cols = [c for c in types if c not in t.key]
        where, params = self._where(t, key)
        sets = ", ".join(f"{self._q(c)} = :v{i}" for i, c in enumerate(cols))
        params.update({f"v{i}": self._bind(types[c], new.get(c)) for i, c in enumerate(cols)})
        await self._exec(table, f"UPDATE {self._q(table)} SET {sets} WHERE {where}", params)

    async def _insert_row(self, table: str, row: Mapping[str, Any]) -> None:
        types = self.cfg.tables[table].column_types
        cols = list(types)
        sql = (f"INSERT INTO {self._q(table)} ({', '.join(self._q(c) for c in cols)}) "
               f"VALUES ({', '.join(f':v{i}' for i in range(len(cols)))})")
        await self._exec(table, sql, {f"v{i}": self._bind(types[c], row.get(c)) for i, c in enumerate(cols)})

    async def _delete_row(self, table: str, key: Mapping[str, Any]) -> None:
        where, params = self._where(self.cfg.tables[table], key)
        await self._exec(table, f"DELETE FROM {self._q(table)} WHERE {where}", params)

    async def _exec(self, table: str, sql: str, params: dict[str, Any]) -> None:
        try:
            await self.conn.execute(text(sql), params)
        except IntegrityError:
            raise StepFailure("invalid_value", f"the change violates a constraint of {table}") from None
        except DBAPIError as exc:
            if getattr(exc, "connection_invalidated", False):
                raise
            raise StepFailure("invalid_value", f"{table} refused the values ({type(exc.orig).__name__})") from None

    # ───────────────────────────── helpers ─────────────────────────────

    def _iter(self, for_each: str | None, scope: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
        if for_each is None:
            yield dict(scope)
            return
        items = resolve(parse_ref(for_each), scope)
        if not isinstance(items, list):
            raise StepFailure("invalid_payload", f"{for_each} must be a list")
        for i, item in enumerate(items, start=1):
            yield {**scope, "item": item, "index": i}

    def _key(self, table: str, template: Mapping[str, Any], scope: Mapping[str, Any]) -> dict[str, Any]:
        t = self.cfg.tables[table]
        types = t.column_types
        key: dict[str, Any] = {}
        for col in t.key:
            value = coerce(types[col], render(template[col], scope), f"{table}.{col}")
            if value is None or value == "":
                raise StepFailure("invalid_payload", f"no value for the key column {table}.{col}")
            key[col] = value
        return key

    def _invariants(self, table: str, row: Mapping[str, Any]) -> None:
        for inv in self.cfg.invariants.get(table, ()):
            total = sum(coef * int(row.get(col) or 0) for col, coef in inv.expr.items())
            if total < inv.min:
                key = key_text(self.cfg.tables[table], row)
                raise StepFailure(inv.error, f"{table} {key}: {_expr_text(inv.expr)} would be {total} (minimum {inv.min})")

    async def allocate_code(self, spec: CodeSpec) -> str:
        name = f"code:{spec.table}.{spec.column}"
        if name not in self.codes:
            m = CODE_FORMAT_RE.match(spec.format)
            assert m is not None
            prefix = m.group(1)
            like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            res = await self.conn.execute(
                text(f"SELECT {self._q(spec.column)} FROM {self._q(spec.table)} "
                     f"WHERE {self._q(spec.column)} LIKE :p ESCAPE '\\'"),
                {"p": like},
            )
            pattern = spec.pattern()
            existing = [int(mm.group(1)) for (v,) in res.all() if isinstance(v, str) and (mm := pattern.match(v))]
            self.codes[name] = max([self.code_floors.get(name, 0), *existing])
        self.codes[name] += 1
        code = spec.render(self.codes[name])
        if self.mode == "validate":
            self._provisional.add(code)
        return code

    def _mask(self, value: Any) -> Any:
        """Validation previews show generated codes as ``null`` (assigned at apply)."""
        if not self._provisional:
            return value
        if isinstance(value, str) and value in self._provisional:
            return None
        if isinstance(value, dict):
            return {k: self._mask(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._mask(v) for v in value]
        return value

    # ───────────────────────────── items ─────────────────────────────

    async def run_item(self, item: ItemSpec) -> ItemResult:
        res = ItemResult(item.seq, item.op, item.target_ref, item.expected_version)
        spec = self.cfg.ops.get(item.op)
        if spec is None:
            return self._fail(res, StepFailure("unsupported_op", f"{item.op} is not supported by {self.cfg.system.name}"))
        touched: set[str] = set()
        sp = await self.conn.begin_nested()
        try:
            await self._run(spec, item, res, touched)
        except StepFailure as exc:
            await sp.rollback()
            return self._fail(res, exc)
        await sp.commit()
        self.txn.touched |= touched
        res.before = self._mask(res.before)
        res.after = self._mask(res.after)
        if self.mode == "validate":
            res.result_ref = None if res.result_ref in self._provisional else res.result_ref
        return res

    @staticmethod
    def _fail(res: ItemResult, exc: StepFailure) -> ItemResult:
        res.ok = False
        res.errors = [f"{exc.code}: {exc.detail}"]
        res.reason = exc.code
        res.actual_version = exc.actual_version
        res.effects = []
        return res

    async def _run(self, spec: OpSpec, item: ItemSpec, res: ItemResult, touched: set[str]) -> None:
        scope: dict[str, Any] = {
            "payload": item.payload, "target_ref": item.target_ref, "expected_version": item.expected_version,
            "op": item.op, "change_set": self.change_set,
        }
        target_key: dict[str, Any] | None = None
        if spec.target is not None:
            t = self.cfg.tables[spec.target.table]
            target_key = self._key(spec.target.table, spec.target.key, scope)
            row = await self.select(spec.target.table, target_key, lock=True)
            if row is None:
                raise StepFailure("not_found", f"{spec.target.table} {key_text(t, target_key)} does not exist")
            version = row.get(t.version_col) if t.version_col else None
            res.actual_version = version
            if spec.check_version:
                if item.expected_version is None:
                    raise StepFailure("invalid_payload", "expected_version is required", actual_version=version)
                if version != item.expected_version:
                    raise StepFailure("version_mismatch",
                                      f"expected {item.expected_version}, current {version}", actual_version=version)
            elif self.mode == "apply" and t.version_col is None and item.validated_before is not None \
                    and row != item.validated_before:
                raise StepFailure("row_changed", f"{spec.target.table} {key_text(t, target_key)} changed since validation")
            res.before = row
            scope["before"] = row
        if spec.code is not None:
            scope["code"] = await self.allocate_code(spec.code)
        steps = spec.steps
        for step in steps:
            if step.require is not None:
                await self._require(step.require, scope)
        for step in steps:
            body = step.body
            if isinstance(body, UpdateStep):
                await self._update(body, scope, item.seq, res.effects, touched)
            elif isinstance(body, InsertStep):
                await self._insert(body, scope, item.seq, res.effects, touched)
            elif isinstance(body, AdjustStep):
                await self._adjust(body, scope, item.seq, res.effects, touched)
        if spec.target is not None and target_key is not None:
            res.after = await self.select(spec.target.table, target_key, lock=False)
        else:
            inserted: dict[str, list[dict[str, Any]]] = {}
            for eff in res.effects:
                if eff.kind == "insert" and eff.after is not None:
                    inserted.setdefault(eff.table, []).append(eff.after)
            res.after = {k: (v[0] if len(v) == 1 else v) for k, v in inserted.items()} or None
        res.units, res.value_cents = await self._units(spec, scope, res.before, res.after)
        default_ref = "{{code}}" if spec.code is not None else "{{target_ref}}"
        ref = render(spec.result_ref if spec.result_ref is not None else default_ref, scope)
        res.result_ref = str(ref) if ref is not None else None

    async def _require(self, step: RequireStep, scope: Mapping[str, Any]) -> None:
        t = self.cfg.tables[step.table]
        for sc in self._iter(step.for_each, scope):
            key = self._key(step.table, step.key, sc)
            row = await self.select(step.table, key, lock=True)
            where = f"{step.table} {key_text(t, key)}"
            if row is None:
                raise StepFailure(step.missing_error, f"{where} does not exist")
            for col, allowed in step.in_.items():
                if row.get(col) not in allowed:
                    raise StepFailure(step.error, f"{where}: {col} is {row.get(col)!r} (allowed: {allowed})")
            if step.at_least is not None:
                need = _int(render(step.at_least.value, sc), f"{where} at_least")
                have = sum(coef * int(row.get(col) or 0) for col, coef in step.at_least.expr.items())
                if have < need:
                    raise StepFailure(step.error, f"{where}: need {need}, available {have}")

    async def _update(self, step: UpdateStep, scope: Mapping[str, Any], seq: int, effects: list[Effect],
                      touched: set[str]) -> None:
        t = self.cfg.tables[step.table]
        types = t.column_types
        for sc in self._iter(step.for_each, scope):
            key = self._key(step.table, step.key, sc)
            row = await self.select(step.table, key, lock=True)
            if row is None:
                raise StepFailure("not_found", f"{step.table} {key_text(t, key)} does not exist")
            new = dict(row)
            for col, tpl in step.set.items():
                new[col] = coerce(types[col], render(tpl, sc), f"{step.table}.{col}")
            for col, tpl in step.add.items():
                new[col] = int(row.get(col) or 0) + _int(render(tpl, sc), f"{step.table}.{col}")
            if step.bump_version and t.version_col:
                new[t.version_col] = int(row.get(t.version_col) or 0) + 1
            self._invariants(step.table, new)
            await self._update_row(step.table, key, new)
            touched.add(step.table)
            effects.append(Effect(seq, "update", step.table, key, row, new))

    async def _insert(self, step: InsertStep, scope: Mapping[str, Any], seq: int, effects: list[Effect],
                      touched: set[str]) -> None:
        t = self.cfg.tables[step.table]
        types = t.column_types
        for sc in self._iter(step.for_each, scope):
            row = {c: None for c in types}
            for col, tpl in step.values.items():
                row[col] = coerce(types[col], render(tpl, sc), f"{step.table}.{col}")
            if t.version_col and row.get(t.version_col) is None:
                row[t.version_col] = 1
            key = {k: row[k] for k in t.key}
            if any(v is None or v == "" for v in key.values()):
                raise StepFailure("invalid_payload", f"{step.table}: a key value is missing")
            if await self.select(step.table, key, lock=True) is not None:
                raise StepFailure("duplicate_key", f"{step.table} {key_text(t, key)} already exists")
            self._invariants(step.table, row)
            await self._insert_row(step.table, row)
            touched.add(step.table)
            effects.append(Effect(seq, "insert", step.table, key, None, row))

    async def _adjust(self, step: AdjustStep, scope: Mapping[str, Any], seq: int, effects: list[Effect],
                      touched: set[str]) -> None:
        t = self.cfg.tables[step.table]
        types = t.column_types
        for sc in self._iter(step.for_each, scope):
            key = self._key(step.table, step.key, sc)
            row = await self.select(step.table, key, lock=True)
            deltas = {col: _int(render(tpl, sc), f"{step.table}.{col}") for col, tpl in step.add.items()}
            if row is None:
                if step.upsert is None:
                    raise StepFailure("not_found", f"{step.table} {key_text(t, key)} does not exist")
                new = {c: None for c in types}
                for col, tpl in step.upsert.items():
                    new[col] = coerce(types[col], render(tpl, sc), f"{step.table}.{col}")
                new.update(key)
                for col, d in deltas.items():
                    new[col] = int(new.get(col) or 0) + d
                if t.version_col:
                    new[t.version_col] = 1
                self._invariants(step.table, new)
                await self._insert_row(step.table, new)
                effects.append(Effect(seq, "insert", step.table, key, None, new))
            else:
                new = dict(row)
                for col, d in deltas.items():
                    new[col] = int(row.get(col) or 0) + d
                if t.version_col:
                    new[t.version_col] = int(row.get(t.version_col) or 0) + 1
                self._invariants(step.table, new)
                await self._update_row(step.table, key, new)
                effects.append(Effect(seq, "update", step.table, key, row, new))
            touched.add(step.table)

    async def _units(self, spec: OpSpec, scope: Mapping[str, Any], before: Any, after: Any) -> tuple[int, int]:
        u = spec.units
        if u is None:
            return 0, 0
        terms: list[tuple[int, Mapping[str, Any]]] = []
        if u.delta is not None:
            b = int((before or {}).get(u.delta) or 0)
            a = int((after or {}).get(u.delta) or 0)
            terms.append((abs(a - b), scope))
        else:
            for sc in self._iter(u.for_each, scope):
                terms.append((_int(render(u.qty, sc), "units.qty"), sc))
        units = sum(q for q, _ in terms)
        value = 0
        if u.price is not None:
            for q, sc in terms:
                row = await self.select(u.price.table, self._key(u.price.table, u.price.key, sc), lock=False)
                price = row.get(u.price.column) if row else None
                value += q * int(price or 0)
        return units, value

    # ───────────────────────────── rollback ─────────────────────────────

    async def revert(self, effects: Sequence[Effect]) -> tuple[list[Effect], list[dict[str, Any]]]:
        """Undo ``effects`` newest first. Each row must still equal the effect's after-image (else a
        conflict and nothing is changed for it); an update restores the before-image's columns and
        bumps the version, an insert is deleted. Returns ``(new effects, conflicts)``."""
        done: list[Effect] = []
        conflicts: list[dict[str, Any]] = []
        for eff in reversed(effects):
            t = self.cfg.tables.get(eff.table)
            if t is None or eff.kind == "delete":
                raise StepFailure("not_reversible", f"{eff.table} effect {eff.kind} cannot be undone")
            current = await self.select(eff.table, eff.key, lock=True)
            vcol = t.version_col
            expected_v = (eff.after or {}).get(vcol) if vcol else None
            ref = f"{eff.table}:{key_text(t, eff.key)}"
            if current is None:
                conflicts.append({"seq": eff.item_seq, "target_ref": ref, "expected_version": expected_v,
                                  "actual_version": None, "reason": "not_found"})
                continue
            if current != eff.after:
                conflicts.append({"seq": eff.item_seq, "target_ref": ref, "expected_version": expected_v,
                                  "actual_version": current.get(vcol) if vcol else None, "reason": "row_changed"})
                continue
            if conflicts:
                continue  # nothing will be committed; keep checking the rest
            if eff.kind == "insert":
                await self._delete_row(eff.table, eff.key)
                done.append(Effect(eff.item_seq, "delete", eff.table, eff.key, current, None))
            else:
                restored = dict(eff.before or {})
                if vcol:
                    restored[vcol] = int(current.get(vcol) or 0) + 1
                self._invariants(eff.table, restored)
                await self._update_row(eff.table, eff.key, restored)
                done.append(Effect(eff.item_seq, "update", eff.table, eff.key, current, restored))
            self.txn.touched.add(eff.table)
        return done, conflicts


def _expr_text(expr: Mapping[str, int]) -> str:
    parts = []
    for i, (col, coef) in enumerate(expr.items()):
        sign = "-" if coef < 0 else ("+" if i else "")
        mag = "" if abs(coef) == 1 else f"{abs(coef)}×"
        parts.append(f"{sign} {mag}{col}".strip() if i else f"{sign}{mag}{col}")
    return " ".join(parts)


def effects_json(effects: Sequence[Effect]) -> str:
    return json.dumps([e.to_dict() for e in effects], separators=(",", ":"), sort_keys=True)
