"""Typed runtime errors.

Nodes never catch broad exceptions (that would break ADK HITL/retries); they let these
propagate, or catch exactly the ones they can handle (e.g. :class:`McpToolError` with a known
code).
"""

from __future__ import annotations

__all__ = [
    "BudgetExceeded",
    "ConnectRequiredError",
    "HashMismatchError",
    "McpToolError",
    "McpTransportError",
    "OptimizerError",
    "RuntimeConfigError",
    "StaleSessionStateError",
]


class RuntimeConfigError(RuntimeError):
    """The runtime is mis-configured (missing model, unknown tool binding, ...)."""


class McpToolError(Exception):
    """A connector tool returned an error result (``"<code>: <detail>"``)."""

    def __init__(self, tool: str, code: str, detail: str) -> None:
        super().__init__(f"{tool}: {code}: {detail}")
        self.tool = tool
        self.code = code
        self.detail = detail

    @property
    def transient(self) -> bool:
        """Errors the caller may retry with the same idempotency key."""
        return self.code in {"receipt_jwks_unavailable", "idempotency_in_flight"}


class McpTransportError(Exception):
    """The connector could not be reached (proxy, DNS, timeout, protocol error)."""

    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"{tool}: {message}")
        self.tool = tool


class OptimizerError(Exception):
    """The optimizer service rejected a request or is unavailable."""

    def __init__(self, path: str, status: int | None, message: str) -> None:
        super().__init__(f"optimizer {path}: {status}: {message}")
        self.path = path
        self.status = status


class ConnectRequiredError(RuntimeError):
    """The approver has no active delegated link to the connector (``changes:apply``)."""

    def __init__(self, connect_url: str | None = None) -> None:
        super().__init__("connect_required")
        self.connect_url = connect_url


class HashMismatchError(RuntimeError):
    """A change-set hash changed between compile, dry-run and execute."""


class BudgetExceeded(RuntimeError):
    """A per-run LLM budget (calls or tokens) was exhausted."""


class StaleSessionStateError(RuntimeError):
    """Session/DB state does not match what the node expects (e.g. plan version moved)."""
