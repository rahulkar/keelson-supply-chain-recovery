"""Declarative config: the shipped configs load; every static rule refuses what it should; the
database-dependent checks (entity SQL columns) run at start-up; the ``check`` CLI."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import FERNHILL, SERVICE, sidecar_env

from adapter_sidecar.config import load_config, parse_config
from adapter_sidecar.errors import ConfigError

EXAMPLES = SERVICE / "examples"


def fernhill_raw() -> dict[str, Any]:
    return yaml.safe_load((FERNHILL / "sidecar.yaml").read_text(encoding="utf-8"))


def refuse(raw: dict[str, Any], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        parse_config(raw)


def test_shipped_configs_load() -> None:
    fern = load_config(FERNHILL / "sidecar.yaml")
    assert fern.backend.kind == "csv" and fern.writable and fern.consistency == "consistent"
    assert sorted(fern.ops) == ["allocation.set_qty", "transfer_order.create"]
    assert len(fern.entities) == 8
    sql = load_config(EXAMPLES / "sql-postgres.yaml")
    assert sql.backend.kind == "sql" and sql.writable
    rest = load_config(EXAMPLES / "rest-readonly.yaml")
    assert rest.backend.kind == "rest" and not rest.writable and rest.consistency == "crawled"


def test_unknown_entity_and_unknown_keys() -> None:
    raw = fernhill_raw()
    raw["entities"]["shipment"] = {"sql": "SELECT 1"}
    refuse(raw, "entities.shipment")
    raw = fernhill_raw()
    raw["tables"]["stock"]["colour"] = "red"
    refuse(raw, "Extra inputs are not permitted")


@pytest.mark.parametrize("name", ["bad-name", "1stock", "x" * 70, "a;drop"])
def test_invalid_identifiers(name: str) -> None:
    raw = fernhill_raw()
    raw["tables"]["stock"]["columns"][name] = "int"
    refuse(raw, "columns")


def test_unknown_template_root_and_fields() -> None:
    raw = fernhill_raw()
    raw["ops"]["allocation.set_qty"]["steps"][1]["update"]["set"]["qty"] = "{{secret.qty}}"
    refuse(raw, "unknown placeholder root 'secret'")
    raw = fernhill_raw()
    # typed op: the payload model has no such field
    raw["ops"]["allocation.set_qty"]["steps"][1]["update"]["set"]["qty"] = "{{payload.quantity}}"
    refuse(raw, "payload has no field 'quantity'")
    raw = fernhill_raw()
    raw["ops"]["transfer_order.create"]["steps"][4]["insert"]["values"]["qty"] = "{{item.amount}}"
    refuse(raw, "item has no field 'amount'")
    raw = fernhill_raw()
    raw["ops"]["transfer_order.create"]["steps"][3]["insert"]["values"]["status"] = "{{item.sku}}"
    refuse(raw, "unknown placeholder root 'item'")  # no for_each on that step
    raw = fernhill_raw()
    raw["ops"]["allocation.set_qty"]["steps"][1]["update"]["set"]["qty"] = "{{payload.__class__}}"
    refuse(raw, "may not start with '__'")


def test_float_literal_and_yaml_date() -> None:
    raw = fernhill_raw()
    raw["snapshot"]["ttl_s"] = 7200.5
    refuse(raw, "float literal")
    text = (FERNHILL / "sidecar.yaml").read_text(encoding="utf-8").replace(
        "upsert: {site_kind: dc,", "upsert: {eta_date: 2026-10-01, site_kind: dc,")
    with pytest.raises(ConfigError, match="unquoted YAML date"):
        parse_config(yaml.safe_load(text))


def test_inline_secret_refused() -> None:
    raw = fernhill_raw()
    raw["auth"]["static_header"]["keys"][0]["api_key"] = "fh_0123456789abcdef0123456789abcdef"
    refuse(raw, "secrets are never inline")
    raw = load_raw(EXAMPLES / "rest-readonly.yaml")
    raw["backend"]["rest"]["auth"]["token"] = "abc"
    refuse(raw, "secrets are never inline")
    raw = load_raw(EXAMPLES / "rest-readonly.yaml")
    raw["backend"]["rest"]["base_url"] = "http://user:pw@erp.example.test"
    refuse(raw, "userinfo")


def load_raw(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_op_rules() -> None:
    raw = fernhill_raw()
    raw["ops"]["Transfer.Create"] = copy.deepcopy(raw["ops"]["transfer_order.create"])
    refuse(raw, "String should match pattern")
    raw = fernhill_raw()
    raw["ops"]["revert.update"] = copy.deepcopy(raw["ops"]["allocation.set_qty"])
    refuse(raw, "revert.* ops are internal")
    raw = fernhill_raw()
    del raw["views"]
    refuse(raw, "views.transfer_orders is required")
    raw = fernhill_raw()
    raw["ops"]["allocation.set_qty"]["steps"][1]["update"]["key"] = {"sku": "{{target_ref}}"}
    refuse(raw, "key must name exactly the table key columns")
    raw = fernhill_raw()
    raw["ops"]["allocation.set_qty"]["steps"][1]["update"]["set"]["rev"] = 3
    refuse(raw, "key / version columns")
    raw = fernhill_raw()
    raw["ops"]["transfer_order.create"]["steps"][5]["adjust"]["add"] = {"status": 1}
    refuse(raw, "not int columns")
    raw = fernhill_raw()
    del raw["tables"]["stock"]["affects"]
    refuse(raw, "tables.stock: ops change this table, so it needs affects")
    raw = fernhill_raw()
    raw["receipts"]["autocommit"]["ops"].append("po_line.cancel")
    refuse(raw, "not configured ops")
    raw = fernhill_raw()
    raw["auth"]["static_header"]["keys"][0]["scopes"] = ["catalog:read"]
    refuse(raw, "no key holds changes:apply")


def test_backend_specific_rules() -> None:
    raw = fernhill_raw()
    raw["tables"]["journal"] = copy.deepcopy(raw["tables"]["size_mix"])
    raw["tables"]["journal"]["file"] = "journal.csv"
    refuse(raw, "reserved for the sidecar's state tables")
    raw = fernhill_raw()
    raw["tables"]["size_mix"]["fingerprint_sql"] = "SELECT 1"
    refuse(raw, "fingerprint_sql is only for sql backends")
    raw = load_raw(EXAMPLES / "rest-readonly.yaml")
    raw["ops"] = {"allocation.set_qty": fernhill_raw()["ops"]["allocation.set_qty"]}
    refuse(raw, "read-only in M8")
    raw = fernhill_raw()
    del raw["tables"]["stock"]["file"]
    refuse(raw, "csv tables need file")


async def test_entity_sql_must_return_scn_columns(tmp_path: Path, data_dir: Path) -> None:
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    raw = fernhill_raw()
    raw["entities"]["allocation"]["sql"] = "SELECT code AS source_id, code, sku AS sku_code FROM store_allocations"
    raw["entities"]["size_curve"]["sql"] = "SELECT nope FROM size_mix"
    cfg_path = tmp_path / "bad.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    sidecar = Sidecar(Settings.from_env(sidecar_env(tmp_path, data_dir, cfg_path)))
    try:
        with pytest.raises(ConfigError) as err:
            await sidecar.start()
        assert "entities.allocation: the SQL must return exactly the scn/1.0 columns" in str(err.value)
        assert "entities.size_curve: the SQL fails" in str(err.value)
        assert not sidecar.ready
    finally:
        await sidecar.close()


def test_settings_validation(tmp_path: Path) -> None:
    from adapter_sidecar.settings import Settings

    env = sidecar_env(tmp_path, tmp_path)
    for bad in ({"PUBLIC_URL": "fernhill.localhost"}, {"PUBLIC_URL": "http://fernhill.localhost/x"},
                {"MCP_RESOURCE": "http://other.localhost/mcp"}, {"MCP_ALLOWED_HOSTS": "other.localhost"},
                {"SIDECAR_SNAPSHOT_SECRET": "short"}, {"SIDECAR_ADMIN_KEY": "short"}):
        with pytest.raises(ConfigError):
            Settings.from_env({**env, **bad})
    s = Settings.from_env(env)
    assert s.mcp_resource == "http://fernhill.localhost/mcp" and s.mcp_allowed_hosts == ["fernhill.localhost"]


def test_check_cli_on_fernhill(data_dir: Path, monkeypatch: pytest.MonkeyPatch,
                               capsys: pytest.CaptureFixture[str]) -> None:
    from adapter_sidecar.__main__ import main

    monkeypatch.setenv("SIDECAR_CONFIG", str(FERNHILL / "sidecar.yaml"))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "config ok: Fernhill ERP (csv)" in out and "FK checks ok" in out
    assert "promotion" in out and "[empty]" in out
    # the real data directory was not touched (checked on a copy, no lock left behind)
    assert not (data_dir / ".sidecar.lock").exists()


def test_check_cli_reports_fk_problems(data_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    from adapter_sidecar.__main__ import main

    lines = (data_dir / "purchase_lines.csv").read_text(encoding="utf-8").replace(
        "FH-PARKA-NVY-XS", "FH-GHOST-XS", 1)
    (data_dir / "purchase_lines.csv").write_text(lines, encoding="utf-8")
    monkeypatch.setenv("SIDECAR_CONFIG", str(FERNHILL / "sidecar.yaml"))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    assert main(["check"]) == 1
    assert "FK check failed: po_line.sku_code → item(sku): FH-GHOST-XS" in capsys.readouterr().out


def test_snapshot_cli_writes_parts(data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from adapter_sidecar.__main__ import main

    monkeypatch.setenv("SIDECAR_CONFIG", str(FERNHILL / "sidecar.yaml"))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    out = tmp_path / "export"
    assert main(["snapshot", "--out", str(out)]) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["entities"]) == 15
    assert (out / "allocation" / "part-0001.parquet").is_file()
