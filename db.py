"""Open connections to Supabase, and say so clearly when there are none.

The simulation page reads frames on demand instead of shipping a summary, so it
is the first thing in the app that needs a database.  Two rules:

* **One door.**  This module is the only place that knows a connection string.
  ``simulation.py`` asks it for a cursor, and the rest of the app never sees psycopg.
* **A missing database is a message, not a crash.**  ``DatabaseMissing`` is raised
  with the variable that was not set, and the page shows it.

A small pool sits in front of ``psycopg.connect``.  A simulation asks for a new
chunk every few seconds and a connect is a TLS handshake plus authentication, so
reusing a handful of sessions is worth the twenty lines.  Every worker process
keeps its own pool, which is why the size is small.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import psycopg


class DatabaseMissing(RuntimeError):
    """Raised when no connection string is configured."""


#: A handful is enough: a page load asks for a plan and then one chunk at a time.
MAX_CONNECTIONS = 4

#: A session left idle is eventually killed by the pooler, and psycopg only finds
#: out when a query fails on it.  Anything idle for longer than this is checked
#: with a cheap statement before it is handed out again - one round trip against
#: a failed page.
STALE_AFTER_SECONDS = 60

_idle: list[tuple[psycopg.Connection, float]] = []
_lock = threading.Lock()


def _new_connection() -> psycopg.Connection:
    connection = psycopg.connect(
        connection_string(),
        autocommit=True,
        # No server-side prepared statements: harmless here, and it keeps the app
        # working unchanged if the connection string is ever pointed at the
        # transaction pooler.
        prepare_threshold=None,
        connect_timeout=15,
    )
    # The default 4 MB is not enough for the hash joins the maps make: a scope of
    # 80,000 frame ids spilled to temp files, which was 110 MB of disk writes per
    # query.  32 MB keeps it in memory, and four pooled connections bound it.
    with connection.cursor() as cur:
        cur.execute("set work_mem = '32MB'")
    return connection


def _checkout() -> psycopg.Connection:
    connection = None
    since = 0.0
    with _lock:
        while _idle:
            candidate, used_at = _idle.pop()
            if not candidate.closed:
                connection, since = candidate, used_at
                break

    if connection is None:
        return _new_connection()

    if time.monotonic() - since > STALE_AFTER_SECONDS:
        try:
            with connection.cursor() as cur:
                cur.execute("select 1")
        except psycopg.Error:
            connection.close()
            return _new_connection()

    return connection


def _checkin(connection: psycopg.Connection) -> None:
    with _lock:
        if not connection.closed and len(_idle) < MAX_CONNECTIONS:
            _idle.append((connection, time.monotonic()))
            return
    connection.close()


def connection_string() -> str:
    """The DSN to use, with the pooler port corrected.

    Supabase serves the same pooler host on 6543 for transaction pooling and 5432
    for session pooling.  Transaction mode will not hold a session across
    statements, which a multi-statement read needs, so the port is rewritten
    rather than left to fail with a confusing pool timeout.
    """
    raw = (
        os.environ.get("DATABASE_URL", "")
        or os.environ.get("RENDER_DATABASE_URL", "")
        or ""
    ).strip()
    if not raw:
        raise DatabaseMissing(
            "DATABASE_URL is not set, so there is no database to read frames from."
        )
    if ".pooler.supabase.com" in raw and ":6543" in raw:
        return raw.replace(":6543", ":5432")
    return raw


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    """A cursor from a pooled connection, returned even when the query raises."""
    connection = _checkout()
    try:
        with connection.cursor() as cur:
            yield cur
    except psycopg.OperationalError:
        # The session died under us: do not put it back in the pool.
        connection.close()
        raise
    else:
        _checkin(connection)
