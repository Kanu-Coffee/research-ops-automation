"""Request-local presentation context; never shared between HTTP threads."""

from contextlib import contextmanager
from contextvars import ContextVar

_principal = ContextVar("researchops_web_principal", default=None)
_query = ContextVar("researchops_web_query", default={})


def get_principal():
    return _principal.get()


def get_query():
    return dict(_query.get())


@contextmanager
def request_context(principal, query=None):
    user_token = _principal.set(principal)
    query_token = _query.set(dict(query or {}))
    try:
        yield
    finally:
        _query.reset(query_token)
        _principal.reset(user_token)
