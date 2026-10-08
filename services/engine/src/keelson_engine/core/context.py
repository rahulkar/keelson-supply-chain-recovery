"""Ambient tenant for repo helpers called from the runtime.

``core.repo`` functions take an explicit ``tenant=`` keyword; when omitted they use the
tenant bound with :func:`use_tenant` (the worker binds it per run). Missing both is an
error, never a silent cross-tenant query.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = ["NoTenantBound", "current_tenant", "resolve_tenant", "use_tenant"]

_tenant: ContextVar[str | None] = ContextVar("keelson_tenant", default=None)


class NoTenantBound(RuntimeError):
    pass


def current_tenant() -> str | None:
    return _tenant.get()


@contextmanager
def use_tenant(tenant: str) -> Iterator[str]:
    token = _tenant.set(tenant)
    try:
        yield tenant
    finally:
        _tenant.reset(token)


def resolve_tenant(tenant: str | None) -> str:
    value = tenant or _tenant.get()
    if not value:
        raise NoTenantBound("no tenant given and none bound with use_tenant()")
    return value
