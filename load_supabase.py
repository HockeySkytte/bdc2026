#!/usr/bin/env python
"""Reload the three BDC2026 tables in Supabase from MySQL.

The copy itself is not implemented here.  It is delegated to
``scripts/reload_supabase_table_from_mysql.py``, the loader this project already
uses for every other table: it streams MySQL rows into ``COPY`` with
``write_row``, runs with ``statement_timeout`` and ``lock_timeout`` off,
``prepare_threshold=None`` for the pgbouncer pooler, and can rebuild a table
from the MySQL schema.  Duplicating that here produced a second, worse version
of the same thing.

What this file adds on top:

1.  the three tables, loaded one after another in the order that matters -
    players first, because it is tiny and everything else refers to it;
2.  a rename of ``position`` to ``player_position``, because ``POSITION`` is a
    reserved word in PostgreSQL and would need quoting in every query for ever;
3.  the indexes, in the same shape as ``scripts/add_all_indexes.py``;
4.  ``ANALYZE`` and ``VACUUM ANALYZE``, which a fresh COPY badly needs.

Usage::

    python load_supabase.py                 # reload all three, then index
    python load_supabase.py --indexes-only  # data is already there, just index
    python load_supabase.py --dry-run       # print what would happen

Environment, from ``.env`` at the repo root: ``MYSQL_*`` for the source and
``DATABASE_URL`` (or ``RENDER_DATABASE_URL``) for Supabase.

Two things worth knowing before a full reload:

*   ``bdc2026_tracking_cleaned`` is 7.4 million rows and about 2.5 GB with its
    indexes.  Supabase counts that against the project's storage limit, and
    over the limit it writes ``default_transaction_read_only = on`` into
    ``postgresql.auto.conf``, which makes every write fail with
    ``ReadOnlySqlTransaction``.  Free space first.
*   Building an index over 7.4 million rows is what costs disk IO on a Supabase
    project.  Create them once, after the load, and not again; every extra
    index here is about 200 MB written and a minute of the IO budget.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import psycopg
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parent.parent
RELOADER = REPO_ROOT / "scripts" / "reload_supabase_table_from_mysql.py"

#: Load order.  Players first: it is 270 rows and the other two join to it.
TABLES = [
    "bdc2026_players",
    "bdc2026_events_cleaned",
    "bdc2026_tracking_cleaned",
]

#: Indexes to build, in the shape scripts/add_all_indexes.py uses.
#: Keep this list short - each one on the tracking table is ~200 MB of writes.
INDEXES = {
    "bdc2026_players": [
        "(team, player_jersey)",
    ],
    "bdc2026_events_cleaned": [
        "(game_id, time_elapsed)",
        "(player_id)",
        "(event)",
    ],
    "bdc2026_tracking_cleaned": [
        "(image_id, player_id)",
        "(game_id, time_elapsed)",
        "(player_id, game_id)",
        # Only ~3% of frames carry an event, so the partial index is a small
        # fraction of a full one and still exact for "frames that have an event".
        "(event_index) WHERE event_index IS NOT NULL",
        # The simulation can filter on either state, and neither is a prefix of
        # the (game_id, time_elapsed) index, so those plans fell back to reading
        # the heap: eight seconds for one filter change.  time_elapsed trails the
        # state so the plan query - distinct seconds for one state - is an
        # index-only scan.
        "(game_id, strength_state, time_elapsed)",
        "(game_id, score_state, time_elapsed)",
        # The Puck vs. Player maps count rows into rink zones.  Rows are not
        # stored in game order (correlation -0.20), so a game-scoped query that
        # needs boxid or the coordinates had to read the whole 1.7 GB table: 30
        # seconds a query, three queries a page.  These cover the columns those
        # queries touch, so the scan stays inside the index - 50 MB instead of
        # 1.7 GB.
        "(game_id, team, boxid, player_jersey, x_feet, y_feet)",
        "(game_id, boxid_puck, image_id)",
    ],
}

#: MySQL column -> PostgreSQL column, applied after the load.
RENAMES = [
    ("position", "player_position"),
]

#: An earlier loader created this index under a shorter name.  Renaming it is a
#: metadata change, so the definition below then skips instead of spending
#: another minute of disk IO rebuilding 7.4 million rows.
LEGACY_INDEX_RENAMES = {
    "ix_tracking_image": "ix_tracking_cleaned_image_id_player_id",
}

#: Index builds sort the whole table; keep the sort in memory where possible so
#: it does not spill to temp files and double the disk IO.
MAINTENANCE_WORK_MEM = "256MB"


def database_url() -> str:
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass
    import os

    url = (os.environ.get("RENDER_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("missing DATABASE_URL (or RENDER_DATABASE_URL)")
    # Supabase's transaction pooler (6543) will not hold a connection long
    # enough for a COPY or an index build; the session pooler on 5432 will.
    if ".pooler.supabase.com" in url and ":6543" in url:
        print("using the session pooler on 5432 instead of the transaction pooler on 6543")
        url = url.replace(":6543", ":5432")
    return url


def connect(url: str) -> psycopg.Connection:
    connection = psycopg.connect(url, autocommit=True, prepare_threshold=None, connect_timeout=20)
    with connection.cursor() as cur:
        cur.execute("set statement_timeout = 0")
        cur.execute("set lock_timeout = 0")
        # Over its storage limit, Supabase sets this in postgresql.auto.conf.
        # Clearing it for the session is a bypass, not a fix: free space.
        try:
            cur.execute("set default_transaction_read_only = off")
        except psycopg.Error:
            pass
    return connection


def reload_table(table: str) -> None:
    """Hand one table to the shared loader and wait for it."""
    command = [
        sys.executable,
        str(RELOADER),
        "--mysql-table", table,
        "--pg-table", table,
        "--rebuild-target",
        "--write-mode", "insert",
    ]
    print(f"\n> {' '.join(command[1:])}", flush=True)
    started = time.time()
    subprocess.run(command, check=True, cwd=REPO_ROOT)
    print(f"  {table} reloaded in {time.time() - started:.0f}s", flush=True)


def rename_columns(pg, tables=None) -> None:
    tables = tables or TABLES
    for old, new in RENAMES:
        with pg.cursor() as cur:
            cur.execute(
                """
                select 1 from information_schema.columns
                where table_schema = 'public' and column_name = %s
                """,
                (old,),
            )
            if cur.fetchone() is None:
                continue
            for table in tables:
                cur.execute(
                    """
                    select 1 from information_schema.columns
                    where table_schema = 'public' and table_name = %s and column_name = %s
                    """,
                    (table, old),
                )
                if cur.fetchone() is None:
                    continue
                cur.execute(
                    sql.SQL("alter table {} rename column {} to {}").format(
                        sql.Identifier(table), sql.Identifier(old), sql.Identifier(new)
                    )
                )
                print(f"  {table}: {old} -> {new}")


def index_name(table: str, definition: str) -> str:
    columns = definition.split(")")[0].lstrip("(")
    cleaned = "".join(c if c.isalnum() else "_" for c in columns.lower())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return f"ix_{table.replace('bdc2026_', '')}_{cleaned}"[:63]


def rename_legacy_indexes(pg) -> None:
    with pg.cursor() as cur:
        for old, new in LEGACY_INDEX_RENAMES.items():
            cur.execute(
                """
                select 1 from pg_indexes where schemaname = 'public' and indexname = %s
                """,
                (old,),
            )
            if cur.fetchone() is None:
                continue
            cur.execute(
                sql.SQL("alter index {} rename to {}").format(
                    sql.Identifier(old), sql.Identifier(new)
                )
            )
            print(f"  renamed {old} -> {new}")


def build_indexes(pg, tables=None) -> None:
    tables = tables or TABLES
    with pg.cursor() as cur:
        cur.execute(f"set maintenance_work_mem = '{MAINTENANCE_WORK_MEM}'")
        for table, definitions in INDEXES.items():
            if table not in tables:
                continue
            for definition in definitions:
                name = index_name(table, definition)
                started = time.time()
                cur.execute(
                    sql.SQL("create index if not exists {} on {} {}").format(
                        sql.Identifier(name), sql.Identifier(table), sql.SQL(definition)
                    )
                )
                print(f"  {name}  ({time.time() - started:.0f}s)", flush=True)


def analyze(pg, tables=None) -> None:
    tables = tables or TABLES
    """Refresh planner statistics.

    A COPY leaves none at all, so this is not optional.  There is no VACUUM step
    because the tables were just loaded and have no dead tuples to reclaim;
    running one would read 7.4 million rows to find nothing.
    """
    with pg.cursor() as cur:
        for table in tables:
            started = time.time()
            cur.execute(sql.SQL("analyze {}").format(sql.Identifier(table)))
            print(f"  analyzed {table}  ({time.time() - started:.0f}s)", flush=True)


def report(pg, tables=None) -> None:
    tables = tables or TABLES
    with pg.cursor() as cur:
        for table in tables:
            cur.execute(f"select count(*) from {table}")
            rows = cur.fetchone()[0]
            cur.execute(f"select pg_size_pretty(pg_total_relation_size('public.{table}'))")
            size = cur.fetchone()[0]
            cur.execute(
                "select count(*) from pg_indexes where schemaname = 'public' and tablename = %s",
                (table,),
            )
            indexes = cur.fetchone()[0]
            print(f"  {table:30} {rows:>10,} rows  {size:>8}  {indexes} indexes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--indexes-only", action="store_true", help="skip the reload")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    parser.add_argument(
        "--table",
        action="append",
        metavar="NAME",
        help="reload only this table; repeat the flag for more than one",
    )
    args = parser.parse_args(argv)

    selected = args.table or TABLES
    unknown = [name for name in selected if name not in TABLES]
    if unknown:
        raise SystemExit(
            "unknown table(s): " + ", ".join(unknown) + "; choose from " + ", ".join(TABLES)
        )

    if args.dry_run:
        print("would reload, in this order:")
        for table in selected:
            print(f"  {table}  via {RELOADER.name}")
        print("\nwould then build:")
        for table, definitions in INDEXES.items():
            if table not in selected:
                continue
            for definition in definitions:
                print(f"  {index_name(table, definition):45} {table}{definition}")
        return 0

    started = time.time()
    if not args.indexes_only:
        for table in selected:
            reload_table(table)

    pg = connect(database_url())
    with pg:
        print("\nrenaming reserved-word columns")
        rename_columns(pg, selected)

        print("\nreusing any index an earlier loader already built")
        rename_legacy_indexes(pg)

        print("\nindexes")
        build_indexes(pg, selected)

        print("\nstatistics")
        analyze(pg, selected)

        print("\nresult")
        report(pg, selected)

    print(f"\ndone in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
