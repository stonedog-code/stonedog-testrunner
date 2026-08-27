"""The queue and the registry — one contract, two backends, chosen by a DSN.

    open_store("data/edge.db")                    -> SQLite, the default
    open_store("sqlite:///var/lib/edge/edge.db")  -> SQLite, spelled explicitly
    open_store("postgresql://user@host/edge")     -> Postgres

**SQLite is the default and Postgres is never reached by accident.** A
standalone runner should need nothing but this repo — no DSN, no container, no
database to stand up first — so the only way to select Postgres is to say so.

The reverse matters too: `EDGE_DB_PATH` still works exactly as it did, because
an upgrade that hard-crashes on the environment somebody already has is not an
upgrade. `EDGE_DB_DSN` wins when both are set, and the edge says at startup
which one it opened.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from .base import (
    ABANDONED, ACTIVE_STATES, BUSY_STATES, CLAIMED, DONE, FAILED, MAX_FIELD,
    MAX_SUMMARY, NOT_DISPATCHED, NO_CAPS, QUEUED, RUNNING, ActionKind, Caps,
    DEFAULT_WORKFLOW, Language, default_action_target,
    EnqueueResult, Job,
    JobDef, JobStore, SaveResult, StoreBusy, StoreError, StoreUnavailable,
    near_misses, validate_job_def,
)

#: The schemes that mean Postgres. `postgres://` is the one everybody types and
#: the one psycopg still accepts; `postgresql://` is the spelling the docs use.
POSTGRES_SCHEMES = ("postgres://", "postgresql://")
SQLITE_SCHEME = "sqlite://"


def backend_for(dsn: str) -> str:
    """Which backend a DSN selects — without opening anything.

    Split out so configuration can be *reported* before it is *used*: the edge
    prints the backend at startup, and printing it must not be the thing that
    fails when the DSN is wrong.
    """
    text = (dsn or "").strip()
    if text.lower().startswith(POSTGRES_SCHEMES):
        return "postgres"
    return "sqlite"


def sqlite_path(dsn: str) -> Path:
    """The file a SQLite DSN names. A bare path is a path — that is the default form.

    THE PATH IS TAKEN LITERALLY, which is deliberately NOT SQLAlchemy's rule.
    There, `sqlite:///x.db` is *relative* and an absolute path needs a fourth
    slash — a distinction that is invisible in a config file and puts the
    database somewhere nobody looked. Here:

        sqlite:///var/lib/edge.db   ->  /var/lib/edge.db   (absolute, as written)
        sqlite://./edge.db          ->  edge.db            (relative)
        data/edge.db                ->  data/edge.db       (the documented default)

    A relative path is normally written as a bare path anyway, because that is
    what `EDGE_DB_PATH` has always been.
    """
    text = (dsn or "").strip()
    if not text.lower().startswith(SQLITE_SCHEME):
        return Path(text)
    parsed = urlparse(text)
    # sqlite:///abs/path.db  ->  netloc "",  path "/abs/path.db"
    # sqlite://./rel.db      ->  netloc ".", path "/rel.db"
    raw = unquote(parsed.path)
    if parsed.netloc in ("", "localhost"):
        return Path(raw)
    return Path(parsed.netloc + raw)


def open_store(dsn: str, *, busy_timeout: float = 5.0,
               pool_size: int | None = None) -> JobStore:
    """Open the store a DSN names. Raises StoreUnavailable with a fixable message.

    `pool_size` is how many operations may genuinely proceed at once against
    Postgres, and it is ignored by SQLite, which has one writer by definition.
    It is a parameter rather than a constant because a caller that wants N
    concurrent claims must be able to ask for N connections — a pool smaller
    than the concurrency does not merely slow the caller down, it serialises it,
    and serialised concurrency is concurrency that cannot be tested.
    """
    text = (dsn or "").strip()
    if not text:
        raise StoreUnavailable("no store configured: set EDGE_DB_PATH or EDGE_DB_DSN")

    if backend_for(text) == "postgres":
        from .postgres_backend import PostgresStore  # noqa: PLC0415 - optional dependency

        kwargs = {} if pool_size is None else {"pool_size": pool_size}
        return PostgresStore(text, busy_timeout=busy_timeout, **kwargs)

    from .sqlite_backend import SqliteStore  # noqa: PLC0415

    return SqliteStore(sqlite_path(text), busy_timeout=busy_timeout)


__all__ = [
    "ABANDONED", "ACTIVE_STATES", "NOT_DISPATCHED", "ActionKind", "BUSY_STATES", "CLAIMED", "Caps",
    "DEFAULT_WORKFLOW", "Language", "default_action_target",
    "DONE", "EnqueueResult", "FAILED", "Job", "JobDef", "JobStore", "MAX_FIELD",
    "MAX_SUMMARY", "NO_CAPS", "POSTGRES_SCHEMES", "QUEUED", "RUNNING",
    "SaveResult", "StoreBusy", "StoreError", "StoreUnavailable", "backend_for",
    "near_misses", "open_store", "sqlite_path", "validate_job_def",
]
