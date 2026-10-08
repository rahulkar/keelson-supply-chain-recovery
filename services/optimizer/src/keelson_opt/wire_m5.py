"""M5 re-planning wire extensions (``docs/contracts/95-m5-remediation.md`` §4).

``keelson_contracts.optimizer`` does not carry these yet, so the optimizer serves them as
*additive* subclasses of the shared models (owned here until the contracts package takes
them over; the shapes below are the proposal):

* ``ProblemData.capacity_bookings`` - lane capacity already booked (Merxa
  ``capacity_booking``, status held/confirmed): ``{code, lane_code, iso_week, qty_units,
  version|null, lines:[{po_line_ref, qty, revert_eta|null}]}``. ``lines`` are the PO lines
  the booking carries (a ``po_line.split`` books the new line; a sea-air PO's booking
  carries all its lines); ``revert_eta`` is the line's ``eta_dc`` once the booking is
  cancelled (its sea arrival; ``null`` = derive it from the lane's ``alt_lane_code``).
  Omitted from dumps while empty, so every pre-M5 ``inputs_hash`` is unchanged.
* :class:`ActionM5` adds the op ``capacity_booking.cancel`` (``target_ref`` = the booking
  code, ``expected_version`` = its version, payload ``{booking_code}``). It compiles into
  a change set as a :class:`keelson_contracts.changeset.GenericOpItem` (not a typed op yet).
* :class:`OptimizeResponseM5` = ``OptimizeResponse`` whose actions may carry that op.
  Without bookings over capacity the response is byte-identical to the shared model's.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

from keelson_contracts import optimizer as _o
from keelson_contracts.changeset import CHANGE_SET_ITEM_ADAPTER
from keelson_contracts.common import ContractModel
from keelson_contracts.common import IsoDate as _IsoDate
from keelson_contracts.optimizer import (
    Action,
    ActionOp,
    ActionPayload,
    ImpactRequest,
    OptimizeRequest,
    OptimizeResponse,
    ProblemData,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

CANCEL_OP: Final = "capacity_booking.cancel"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _is_empty(value: Any) -> bool:
    return not value


class BookedLine(_Model):
    """A PO line (part) carried by a booking."""

    po_line_ref: _o.PoLineRef
    qty: _o.PosInt
    """Units of the line on the booked lane."""
    revert_eta: _IsoDate | None = None
    """``eta_dc`` of the line once the booking is cancelled (the sea arrival); ``None`` =
    derived from the booking lane's ``alt_lane_code``."""


class CapacityBooking(_Model):
    """Capacity held on an inbound lane in its departure week (Merxa ``capacity_booking``)."""

    code: _o.Code
    lane_code: _o.Code
    """An ``InboundLane`` code."""
    iso_week: _o.IsoWeek
    """Departure (booking) week."""
    qty_units: _o.PosInt
    """Capacity drawn from the lane in ``iso_week``."""
    version: _o.PosInt | None = None
    lines: list[BookedLine] = Field(default_factory=list)
    """PO lines the booking carries (``[]`` = a stand-alone hold)."""

    @model_validator(mode="after")
    def _lines_fit(self) -> CapacityBooking:
        refs = [ln.po_line_ref for ln in self.lines]
        if len(set(refs)) != len(refs):
            raise ValueError(f"capacity_booking {self.code}: duplicate po_line_ref")
        if sum(ln.qty for ln in self.lines) > self.qty_units:
            raise ValueError(f"capacity_booking {self.code}: lines carry more than qty_units")
        return self


class ProblemDataM5(ProblemData):
    """``ProblemData`` + ``capacity_bookings`` (see the module docstring)."""

    capacity_bookings: list[CapacityBooking] = Field(default_factory=list, exclude_if=_is_empty)

    @model_validator(mode="after")
    def _check_bookings(self) -> ProblemDataM5:
        if not self.capacity_bookings:
            return self
        codes = [b.code for b in self.capacity_bookings]
        if len(set(codes)) != len(codes):
            raise ValueError("duplicate capacity_booking codes")
        lanes = {il.code for il in self.inbound_lanes}
        po_lines = {pl.ref for pl in self.po_lines}
        owner: dict[str, str] = {}
        errors: list[str] = []
        for b in self.capacity_bookings:
            if b.lane_code not in lanes:
                errors.append(f"capacity_booking {b.code}: unknown inbound lane {b.lane_code}")
            for ln in b.lines:
                if ln.po_line_ref not in po_lines:
                    errors.append(f"capacity_booking {b.code}: unknown po_line {ln.po_line_ref}")
                elif ln.po_line_ref in owner:
                    errors.append(
                        f"capacity_booking {b.code}: po_line {ln.po_line_ref} is already carried by "
                        f"{owner[ln.po_line_ref]}"
                    )
                else:
                    owner[ln.po_line_ref] = b.code
        if errors:
            raise ValueError("; ".join(errors[:20]) + (f" (+{len(errors) - 20} more)" if len(errors) > 20 else ""))
        return self


class OptimizeRequestM5(OptimizeRequest):
    data: ProblemDataM5


class ImpactRequestM5(ImpactRequest):
    data: ProblemDataM5


class CapacityBookingCancelPayload(ContractModel):
    """``capacity_booking.cancel``: release a booking (its PO lines go back to their sea
    arrival through the ``po_line.reschedule`` items of the same atomic group)."""

    booking_code: _o.Code


_PAYLOADS: Final[Mapping[str, type[BaseModel]]] = {
    **_o._ACTION_PAYLOADS,  # noqa: SLF001 - the shared op -> payload table
    CANCEL_OP: CapacityBookingCancelPayload,
}


class ActionM5(Action):
    """:class:`keelson_contracts.optimizer.Action` + ``capacity_booking.cancel``."""

    op: ActionOp | Literal["capacity_booking.cancel"]  # type: ignore[assignment]
    payload: ActionPayload | CapacityBookingCancelPayload  # type: ignore[assignment]

    @field_validator("payload", mode="before")
    @classmethod
    def _payload_for_op(cls, value: Any, info: ValidationInfo) -> Any:
        op = info.data.get("op")
        model = _PAYLOADS.get(op) if isinstance(op, str) else None
        if model is not None and isinstance(value, Mapping):
            return model.model_validate(value)
        return value

    @model_validator(mode="after")
    def _check_op(self) -> ActionM5:
        model = _PAYLOADS[self.op]
        if not isinstance(self.payload, model):
            raise ValueError(f"payload of {self.op} must be {model.__name__}, got {type(self.payload).__name__}")
        if isinstance(self.payload, CapacityBookingCancelPayload) and self.target_ref != self.payload.booking_code:
            raise ValueError(f"{CANCEL_OP}: target_ref must equal payload.booking_code")
        CHANGE_SET_ITEM_ADAPTER.validate_python(self.change_set_item())
        return self


class OptimizeResponseM5(OptimizeResponse):
    actions: list[ActionM5]  # type: ignore[assignment]
    """Ordered deterministically with ``seq`` = 1..n; atomic groups are contiguous."""


__all__ = [
    "CANCEL_OP",
    "ActionM5",
    "BookedLine",
    "CapacityBooking",
    "CapacityBookingCancelPayload",
    "ImpactRequestM5",
    "OptimizeRequestM5",
    "OptimizeResponseM5",
    "ProblemDataM5",
]
