"""SQLite connection and transaction management."""

import os
from pathlib import Path
import sqlite3
import stat
from typing import Generator, Optional
from contextlib import contextmanager
from datetime import datetime, timezone
from researchops.errors import ConfigError


class GuardedConnection(sqlite3.Connection):
    """Keep offline restore excluded for the full lifetime of a connection."""
    _runtime_guard = None

    def close(self):
        try:
            super().close()
        finally:
            guard,self._runtime_guard=self._runtime_guard,None
            if guard is not None:
                guard.__exit__(None,None,None)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class Database:
    def __init__(self, db_path: str or Path):
        self.db_path = str(db_path)
        # Ensure parent directory exists
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def get_connection(self) -> sqlite3.Connection:
        from researchops.operations import runtime_guard
        guard=runtime_guard(Path(self.db_path))
        guard.__enter__()
        conn=None
        try:
            if not Path(self.db_path).exists():
                try:
                    fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                    os.close(fd)
                except FileExistsError:
                    pass
            info=Path(self.db_path).lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                raise ConfigError("Database must be a singly linked regular file")
            conn = sqlite3.connect(self.db_path, timeout=30.0, factory=GuardedConnection)
            conn._runtime_guard=guard
            os.chmod(self.db_path, 0o600)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA synchronous=FULL;")
            # Connection-local triggers attribute legacy default actor writes
            # without persisting a custom SQL function into the business schema.
            # Old worker releases never see this trigger. Explicit worker/CLI
            # actors are preserved, and ContextVar values cannot cross requests.
            from researchops.services.audit_context import current_audit_actor
            conn.create_function("researchops_request_actor", 0, current_audit_actor)
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_events'").fetchone():
                conn.execute("""CREATE TEMP TRIGGER request_audit_actor AFTER INSERT ON main.audit_events
                    WHEN NEW.actor='single-operator' AND researchops_request_actor()!=''
                    BEGIN
                        UPDATE audit_events SET actor=researchops_request_actor() WHERE event_id=NEW.event_id;
                    END""")
            return conn
        except Exception:
            if conn is not None:
                conn.close()
            else:
                guard.__exit__(None,None,None)
            raise

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        """Apply versioned migrations without mutating preserved legacy evidence."""
        from researchops.storage.migrations import migrate
        conn = self.get_connection()
        try:
            migrate(conn, Path(self.db_path))
        finally:
            conn.close()
