"""Request-local Web audit identity; background and host actors stay explicit."""

from contextlib import contextmanager
from contextvars import ContextVar


_actor = ContextVar("researchops_audit_actor", default="")


def current_audit_actor():
    return _actor.get()


@contextmanager
def audit_actor(actor):
    if not isinstance(actor, str) or not actor or len(actor) > 128:
        raise ValueError("Invalid audit actor")
    token = _actor.set(actor)
    try:
        yield
    finally:
        _actor.reset(token)
