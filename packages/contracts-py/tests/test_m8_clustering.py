"""M8 signal clustering contracts (``docs/contracts/123-m8-signal-clustering.md``): event memberships
(method evidence), ``AlertDetail.memberships`` (optional, omitted when empty), the platform clustering
settings (bounds, versioning) and the status shape."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from keelson_contracts.signals import (
    DEFAULT_CLUSTERING_CONFIG,
    LOCAL_SIMILARITY_PROFILE,
    MEMBERSHIP_METHODS,
    AlertDetail,
    ClusteringConfig,
    ClusteringConfigUpdate,
    ClusteringStatus,
    EventMembership,
    VectorThresholds,
    event_id,
    signal_id,
)

from test_m6 import EVT_ID, SIG_ID, _alert, _event, _exposure, _signal

NEWS_ID = signal_id("rss.demo", "https://news.example/a", 1)
COPY_ID = signal_id("rss.wire", "https://wire.example/b", 1)
OTHER_EVENT = event_id("signal", "rss.demo", "https://news.example/old")


def _membership(method: str = "founded", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "signal_id": SIG_ID,
        "event_id": EVT_ID,
        "method": method,
        "prior_method": None,
        "from_event_id": None,
        "distance_km": None,
        "shared_refs": [],
        "similarity_bp": None,
        "similarity_profile": None,
        "hamming_bits": None,
        "duplicate_of_signal_id": None,
        "config_version": 1,
        "explanation": "First report of this event.",
        "decided_at": "2026-10-07T06:01:00Z",
    }
    base.update(over)
    return base


GOOD: list[dict[str, Any]] = [
    _membership("anchor", explanation="Official GDACS report that founded this event."),
    _membership("founded"),
    _membership("same_hazard", distance_km=38),
    _membership("attach_geo", distance_km=142),
    _membership("attach_ref", shared_refs=["UNLOCODE:VNSGN", "ISO3166:VN"], distance_km=12),
    _membership("attach_ref", shared_refs=["ISO3166:VN"]),
    _membership("attach_vector", similarity_bp=8200, similarity_profile=LOCAL_SIMILARITY_PROFILE, distance_km=410),
    _membership("near_duplicate", signal_id=COPY_ID, hamming_bits=2, duplicate_of_signal_id=NEWS_ID),
    _membership("absorbed", prior_method="attach_ref", from_event_id=OTHER_EVENT, shared_refs=["ISO3166:VN"]),
    _membership("merged_vector", prior_method="founded", from_event_id=OTHER_EVENT, similarity_bp=9300,
                similarity_profile="embed-default@1536"),
]


@pytest.mark.parametrize("data", GOOD, ids=[f"{d['method']}-{i}" for i, d in enumerate(GOOD)])
def test_membership_evidence_ok(data: dict[str, Any]) -> None:
    m = EventMembership.model_validate(data)
    assert EventMembership.model_validate(m.model_dump(mode="json")) == m


BAD: list[tuple[str, dict[str, Any]]] = [
    ("founded with evidence", _membership("founded", distance_km=3)),
    ("anchor with refs", _membership("anchor", shared_refs=["ISO3166:VN"])),
    ("vector without similarity", _membership("attach_vector")),
    ("vector without profile", _membership("attach_vector", similarity_bp=8000)),
    ("profile without similarity", _membership("attach_geo", distance_km=5, similarity_profile="local-terms-v1")),
    ("near-dup without bits", _membership("near_duplicate", signal_id=COPY_ID, duplicate_of_signal_id=NEWS_ID)),
    ("near-dup without original", _membership("near_duplicate", signal_id=COPY_ID, hamming_bits=1)),
    ("near-dup of itself", _membership("near_duplicate", hamming_bits=1, duplicate_of_signal_id=SIG_ID)),
    ("attach_ref without refs", _membership("attach_ref")),
    ("attach_geo without distance", _membership("attach_geo")),
    ("same_hazard without distance", _membership("same_hazard")),
    ("absorbed without origin", _membership("absorbed", prior_method="founded")),
    ("absorbed without prior", _membership("absorbed", from_event_id=OTHER_EVENT)),
    ("absorbed from itself", _membership("absorbed", prior_method="founded", from_event_id=EVT_ID)),
    ("prior on a direct join", _membership("attach_geo", distance_km=5, prior_method="founded")),
    ("prior is a move", _membership("merged_vector", prior_method="absorbed", from_event_id=OTHER_EVENT,
                                    similarity_bp=9000, similarity_profile="local-terms-v1")),
    ("bits > 64", _membership("near_duplicate", signal_id=COPY_ID, hamming_bits=65, duplicate_of_signal_id=NEWS_ID)),
    ("similarity > 10000", _membership("attach_vector", similarity_bp=10_001, similarity_profile="local-terms-v1")),
    ("bad profile", _membership("attach_vector", similarity_bp=9000, similarity_profile="Embed Default")),
    ("duplicate refs", _membership("attach_ref", shared_refs=["ISO3166:VN", "ISO3166:VN"])),
    ("bad ref", _membership("attach_ref", shared_refs=["PORT:SGN"])),
    ("empty explanation", _membership("founded", explanation="")),
    ("long explanation", _membership("founded", explanation="x" * 401)),
    ("naive time", _membership("founded", decided_at="2026-10-07T06:01:00")),
    ("unknown method", _membership("guessed")),
    ("extra field", {**_membership("founded"), "score": 1}),
]


@pytest.mark.parametrize(("why", "data"), BAD, ids=[b[0] for b in BAD])
def test_membership_evidence_refused(why: str, data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EventMembership.model_validate(data)


def test_membership_methods_vocabulary() -> None:
    assert MEMBERSHIP_METHODS == ("anchor", "same_hazard", "founded", "attach_geo", "attach_ref", "attach_vector",
                                  "near_duplicate", "absorbed", "merged_vector")


# ─────────────────────────────── AlertDetail ───────────────────────────────


def _detail(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"alert": _alert(), "event": _event(), "signals": [_signal()], "exposures": [_exposure()]}
    base.update(over)
    return base


def test_alert_detail_without_memberships_is_unchanged() -> None:
    m6 = _detail()
    detail = AlertDetail.model_validate(m6)
    assert detail.memberships == []
    dumped = detail.model_dump(mode="json")
    assert "memberships" not in dumped  # M6/M7 payloads serialize exactly as before
    assert json.dumps(dumped, sort_keys=True) == json.dumps(AlertDetail.model_validate(dumped).model_dump(mode="json"),
                                                             sort_keys=True)


def test_alert_detail_with_memberships_round_trip() -> None:
    detail = AlertDetail.model_validate(_detail(memberships=[_membership("anchor", explanation="Official GDACS report.")]))
    dumped = detail.model_dump(mode="json")
    assert dumped["memberships"][0]["method"] == "anchor"
    assert AlertDetail.model_validate(dumped) == detail


@pytest.mark.parametrize("bad", [
    [_membership("founded", event_id=OTHER_EVENT)],  # another event
    [_membership("founded", signal_id=NEWS_ID)],  # not a member signal
    [_membership("founded"), _membership("anchor")],  # two for one signal
])
def test_alert_detail_refuses_foreign_memberships(bad: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        AlertDetail.model_validate(_detail(memberships=bad))


# ─────────────────────────────── settings ───────────────────────────────


def test_default_config_is_valid_and_frozen() -> None:
    cfg = DEFAULT_CLUSTERING_CONFIG
    assert cfg.version == 1 and cfg.updated_at is None and cfg.updated_by is None
    assert cfg.simhash.max_hamming_bits <= 7  # the 8 x 8-bit band index is exact up to 7 bits
    assert ClusteringConfig.model_validate(cfg.model_dump(mode="json")) == cfg
    with pytest.raises(ValidationError):
        cfg.simhash.enabled = False  # type: ignore[misc]


@pytest.mark.parametrize(("thresholds", "ok"), [
    ({"attach_bp": 7000, "merge_bp": 7000, "strict_bp": 7000}, True),
    ({"attach_bp": 5000, "merge_bp": 9900, "strict_bp": 10_000}, True),
    ({"attach_bp": 4999, "merge_bp": 8000, "strict_bp": 8000}, False),
    ({"attach_bp": 9901, "merge_bp": 9950, "strict_bp": 9950}, False),
    ({"attach_bp": 8000, "merge_bp": 7999, "strict_bp": 8000}, False),
    ({"attach_bp": 8000, "merge_bp": 8000, "strict_bp": 7999}, False),
    ({"attach_bp": 8000, "merge_bp": 8000, "strict_bp": 10_001}, False),
    ({"attach_bp": 8000.0, "merge_bp": 8000, "strict_bp": 8000}, False),
])
def test_vector_threshold_bounds(thresholds: dict[str, Any], ok: bool) -> None:
    if ok:
        VectorThresholds.model_validate(thresholds)
    else:
        with pytest.raises(ValidationError):
            VectorThresholds.model_validate(thresholds)


def _update(**over: Any) -> dict[str, Any]:
    cfg = DEFAULT_CLUSTERING_CONFIG.model_dump(mode="json")
    base: dict[str, Any] = {"base_version": 1, "simhash": cfg["simhash"], "vector": cfg["vector"], "updated_by": "ava"}
    base.update(over)
    return base


def test_config_update_bounds() -> None:
    ClusteringConfigUpdate.model_validate(_update())
    ClusteringConfigUpdate.model_validate({k: v for k, v in _update().items() if k != "updated_by"})
    simhash = DEFAULT_CLUSTERING_CONFIG.simhash.model_dump(mode="json")
    vector = DEFAULT_CLUSTERING_CONFIG.vector.model_dump(mode="json")
    for bad in (
        _update(base_version=0),
        _update(simhash={**simhash, "max_hamming_bits": 8}),  # the band index is exact only up to 7 bits
        _update(simhash={**simhash, "window_hours": 0}),
        _update(simhash={**simhash, "window_hours": 169}),
        _update(simhash={**simhash, "enabled": "yes"}),
        _update(vector={**vector, "max_distance_km": 49}),
        _update(vector={**vector, "max_distance_km": 3001}),
        _update(vector={**vector, "local": {**vector["local"], "merge_bp": 100}}),
        _update(updated_by=""),
        {**_update(), "version": 2},
    ):
        with pytest.raises(ValidationError):
            ClusteringConfigUpdate.model_validate(bad)


def test_status_shape() -> None:
    counts = dict.fromkeys(("simhash_near_duplicates", "same_source_duplicates", "extractions_reused", "anchor",
                            "same_hazard", "founded", "attach_geo", "attach_ref", "attach_vector", "near_duplicate",
                            "absorbed", "merged_vector", "embeddings_pending", "embeddings_failed"), 0)
    status = {
        "config": DEFAULT_CLUSTERING_CONFIG.model_dump(mode="json"),
        "vector_backend": {"pgvector": True, "local_profile": "local-terms-v1",
                           "remote": {"alias": "embed-default", "dims": 1536, "storage": "vector(1536)",
                                      "status": "backfilling", "pending": 4},
                           "remote_reason": None},
        "window_days": 7,
        "counts": counts,
    }
    parsed = ClusteringStatus.model_validate(status)
    assert parsed.vector_backend.remote is not None and parsed.vector_backend.remote.storage == "vector(1536)"
    local = copy.deepcopy(status)
    local["vector_backend"].update(remote=None, remote_reason="provider_local")
    ClusteringStatus.model_validate(local)
    for bad_backend in ({"remote_reason": "nope"}, {"local_profile": "local-hash-v1"}, {"pgvector": 1}):
        bad = copy.deepcopy(status)
        bad["vector_backend"].update(bad_backend)
        with pytest.raises(ValidationError):
            ClusteringStatus.model_validate(bad)
    bad = copy.deepcopy(status)
    bad["vector_backend"]["remote"]["storage"] = "vector(0)"
    with pytest.raises(ValidationError):
        ClusteringStatus.model_validate(bad)
    bad = copy.deepcopy(status)
    bad["counts"]["attach_vector"] = -1
    with pytest.raises(ValidationError):
        ClusteringStatus.model_validate(bad)
