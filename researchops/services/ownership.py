"""Stable resource ownership for Web commands and background delivery.

Account activation controls browser access only. Scheduled work and delivery
derive their scope from persisted resource identity, including disabled owners.
"""

from contextlib import closing, contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib

from researchops.errors import NotFoundError, ValidationError

_creator = ContextVar("researchops_creation_owner", default=None)
KINDS = frozenset({"task", "sender", "recipient_group"})


@contextmanager
def _connection(database):
    if hasattr(database, "get_connection"):
        with closing(database.get_connection()) as conn:
            yield conn
    else:
        yield database


@contextmanager
def creation_owner(user_id):
    """Set a trusted server-selected creator; None selects host authority."""
    if user_id is not None and (not isinstance(user_id, str) or not user_id):
        raise ValueError("Invalid creation owner")
    token = _creator.set(user_id)
    try:
        yield
    finally:
        _creator.reset(token)


def installation_owner(database):
    with _connection(database) as conn:
        row = conn.execute("SELECT owner_user_id FROM auth_installation WHERE singleton=1").fetchone()
        return row[0] if row else None


def resolve_creation_owner(conn):
    owner = _creator.get()
    if owner is None:
        return installation_owner(conn)
    # The authenticated actor is authorized separately. An administrator can
    # add a sender/group for a Task whose owner cannot currently log in.
    row = conn.execute("SELECT 1 FROM auth_users WHERE user_id=?", (owner,)).fetchone()
    if not row:
        raise ValidationError("자료의 소유 계정을 찾을 수 없습니다.")
    return owner


def adopt_unowned(conn, user_id):
    """Called in the first-admin/migration transaction; never moves ownership."""
    conn.execute("INSERT OR IGNORE INTO auth_installation(singleton,owner_user_id) VALUES(1,?)", (user_id,))
    owner = installation_owner(conn)
    conn.execute("UPDATE entity_catalog SET owner_user_id=? WHERE owner_user_id IS NULL", (owner,))
    return owner


def entity_owner(database, kind, key):
    if kind not in KINDS:
        raise ValidationError("Unknown resource kind")
    with _connection(database) as conn:
        row = conn.execute("SELECT owner_user_id FROM entity_catalog WHERE kind=? AND legacy_key=?", (kind, key)).fetchone()
        if row is None:
            raise NotFoundError("자료를 찾을 수 없습니다.")
        return row[0]


def scoped_request_key(owner_user_id, request_key):
    if request_key is None or owner_user_id is None:
        return request_key
    return "owner:" + hashlib.sha256((owner_user_id + "\0" + request_key).encode()).hexdigest()


def scoped_delivery_config(database, config, owner_user_id):
    """Copy the protected config with a namespace-local recipient mapping.

Selected sender checks remain mandatory. This is an internal delivery helper,
not a redacted presentation object: other senders are deliberately preserved.
Uncataloged host configuration belongs to the installation owner until bootstrap.
"""
    with _connection(database) as conn:
        owners = dict(conn.execute("SELECT legacy_key,owner_user_id FROM entity_catalog WHERE kind='recipient_group'"))
        host_owner = installation_owner(conn)
    result = deepcopy(config)
    result.recipient_groups = {key: list(value) for key, value in config.recipient_groups.items()
        if owners.get(key, host_owner) == owner_user_id}
    return result


def require_task_delivery_ownership(database, definition):
    """Validate immutable Task references, including in a publication transaction."""
    if hasattr(definition, "to_dict"):
        definition = definition.to_dict()
    with _connection(database) as conn:
        try:
            owner = entity_owner(conn, "task", definition["id"])
        except NotFoundError:
            owner = resolve_creation_owner(conn)
        delivery = definition.get("delivery") or {}
        references = [("sender", delivery.get("sender_profile_id", "default"))]
        references += [("recipient_group", key) for key in delivery.get("allowed_recipient_group_ids", [])]
        alerts = definition.get("alerting") or {}
        if alerts.get("events"):
            references.append(("recipient_group", alerts.get("recipient_group_id", "researchops-admins")))
        for kind, key in references:
            try:
                reference_owner = entity_owner(conn, kind, key)
            except NotFoundError:
                # Core/CLI fixtures can run before account setup. Once accounts
                # exist, missing identities must never become implicit grants.
                if owner is None and installation_owner(conn) is None:
                    continue
                raise ValidationError("Task 소유자의 발신 계정과 수신자 그룹을 선택하세요.") from None
            if reference_owner != owner:
                raise ValidationError("Task 소유자의 발신 계정과 수신자 그룹을 선택하세요.")
        return owner
