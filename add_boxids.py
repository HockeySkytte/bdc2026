#!/usr/bin/env python
"""Add ``boxid`` and ``boxid_puck`` to ``bdc2026_tracking_cleaned`` in Supabase.

The geometry is ``HockeyRinkZones.geojson`` from the repo root: 58 polygons,
``O01``-``O26``, ``N01``-``N06`` and ``D01``-``D26``, the same file the challenge
project used.  It is a clean partition of the rink - no gaps, no overlaps - so
every on-rink point belongs to exactly one box.

The rules match ``scripts/tracking_load_bdc2026.py``:

*   a point on a shared edge is covered by both boxes and the **lowest id wins**,
    the same tie-break the QMJHL loader uses with ``order by zone_id limit 1``;
*   a point outside the rink - past the rounded corners - gets NULL;
*   a missing coordinate gets NULL.

**One thing to know before using these labels.**  This table's coordinates were
rotated so the home team always defends ``+x``.  The zone file labels ``O`` as
the ``+x`` end.  So in this table ``O`` is the **home team's defensive end**,
not the attacking end.  In the challenge project the coordinates were
attack-normalised, so ``O`` there meant the attacking end.

If you want attack-relative boxes, swap the ends for one side:

```sql
case when venue = 'Home' then <O->D, D->O> else boxid end
```

Steps: enable PostGIS, build the 58-row zone table, add the two columns, then
fill one game at a time so a failure is resumable.

Usage::

    python add_boxids.py --dry-run
    python add_boxids.py
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import psycopg
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parent.parent
ZONES_FILE = REPO_ROOT / "HockeyRinkZones.geojson"
TRACKING_TABLE = "bdc2026_tracking_cleaned"
ZONES_TABLE = "bdc2026_rink_zones"


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
    if ".pooler.supabase.com" in url and ":6543" in url:
        print("using the session pooler on 5432 instead of the transaction pooler on 6543")
        url = url.replace(":6543", ":5432")
    return url


def connect(url: str) -> psycopg.Connection:
    connection = psycopg.connect(url, autocommit=True, prepare_threshold=None, connect_timeout=20)
    with connection.cursor() as cur:
        cur.execute("set statement_timeout = 0")
        cur.execute("set lock_timeout = 0")
        cur.execute("set default_transaction_read_only = off")
    return connection


def zones_from_geojson() -> list[tuple[str, str]]:
    """``[(zone_id, geometry_json), ...]`` in file order."""
    raw = json.loads(ZONES_FILE.read_text(encoding="utf-8"))
    features = raw.get("features") or []
    if not features:
        raise SystemExit(f"no features in {ZONES_FILE}")

    out = []
    seen = set()
    for feature in features:
        zone_id = str((feature.get("properties") or {}).get("id") or "").strip()
        if not zone_id:
            raise SystemExit(f"zone without an id in {ZONES_FILE}")
        if zone_id in seen:
            raise SystemExit(f"duplicate zone id {zone_id!r} in {ZONES_FILE}")
        seen.add(zone_id)
        out.append((zone_id, json.dumps(feature["geometry"])))
    return out


def ensure_zones(pg, zones: list[tuple[str, str]]) -> None:
    with pg.cursor() as cur:
        cur.execute("create extension if not exists postgis")
        cur.execute(
            sql.SQL("drop table if exists {}").format(sql.Identifier(ZONES_TABLE))
        )
        cur.execute(
            sql.SQL("create table {} (zone_id text primary key, geom geometry)").format(
                sql.Identifier(ZONES_TABLE)
            )
        )
        for zone_id, geometry in zones:
            cur.execute(
                sql.SQL(
                    "insert into {} (zone_id, geom) "
                    "values (%s, st_setsrid(st_geomfromgeojson(%s), 0))"
                ).format(sql.Identifier(ZONES_TABLE)),
                (zone_id, geometry),
            )
        cur.execute(
            sql.SQL("create index on {} using gist (geom)").format(
                sql.Identifier(ZONES_TABLE)
            )
        )
        cur.execute(
            sql.SQL("select count(*), min(zone_id), max(zone_id) from {}").format(
                sql.Identifier(ZONES_TABLE)
            )
        )
        count, lowest, highest = cur.fetchone()
        print(f"  {count} zones loaded ({lowest} .. {highest})")


def add_columns(pg) -> None:
    with pg.cursor() as cur:
        cur.execute(
            sql.SQL(
                "alter table {} "
                "add column if not exists boxid text, "
                "add column if not exists boxid_puck text"
            ).format(sql.Identifier(TRACKING_TABLE))
        )


def games(pg) -> list[str]:
    with pg.cursor() as cur:
        cur.execute(
            sql.SQL("select distinct game_id from {} order by game_id").format(
                sql.Identifier(TRACKING_TABLE)
            )
        )
        return [row[0] for row in cur.fetchall()]


def already_done(pg, game_id: str) -> bool:
    """A game counts as done once any row carries a box id.

    Every game has millions of on-rink player points, so a game with no box ids
    at all has simply not been processed yet.
    """
    with pg.cursor() as cur:
        cur.execute(
            sql.SQL(
                "select exists (select 1 from {} where game_id = %s and boxid is not null)"
            ).format(sql.Identifier(TRACKING_TABLE)),
            (game_id,),
        )
        return bool(cur.fetchone()[0])


def fill_game(pg, game_id: str) -> tuple[int, float]:
    """Set both columns for one game in a single pass.

    One UPDATE rather than two: the row is rewritten once instead of twice, which
    halves both the WAL and the heap churn on a 7.4 million row table.

    The correlated subqueries are the point-in-polygon test.  ``st_covers`` means
    a point on a shared edge matches both boxes, and ``min(zone_id)`` picks the
    lowest, which is the documented tie-break.  With no covering polygon the
    subquery yields NULL, so off-rink points get NULL rather than a wrong label.
    """
    started = time.time()
    with pg.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                update {table} t
                   set boxid = (
                         select min(z.zone_id) from {zones} z
                          where st_covers(z.geom, st_setsrid(st_makepoint(t.x_feet, t.y_feet), 0))
                       ),
                       boxid_puck = (
                         select min(z.zone_id) from {zones} z
                          where st_covers(z.geom, st_setsrid(st_makepoint(t.x_puck_feet, t.y_puck_feet), 0))
                       )
                 where t.game_id = %s
                """
            ).format(
                table=sql.Identifier(TRACKING_TABLE),
                zones=sql.Identifier(ZONES_TABLE),
            ),
            (game_id,),
        )
        return cur.rowcount, time.time() - started


def report(pg) -> None:
    with pg.cursor() as cur:
        cur.execute(
            sql.SQL(
                "select count(*), "
                "count(boxid), count(boxid_puck), "
                "count(*) filter (where x_feet is not null), "
                "count(*) filter (where x_puck_feet is not null), "
                "count(distinct boxid) "
                "from {}"
            ).format(sql.Identifier(TRACKING_TABLE))
        )
        rows, boxed, boxed_puck, with_xy, with_puck_xy, distinct_boxes = cur.fetchone()
        print(f"\n  rows                 {rows:>12,}")
        print(f"  x/y present          {with_xy:>12,}")
        print(f"  boxid set            {boxed:>12,}   ({100 * boxed / max(with_xy, 1):.1f}% of points with coordinates)")
        print(f"  puck coordinates     {with_puck_xy:>12,}")
        print(f"  boxid_puck set       {boxed_puck:>12,}   ({100 * boxed_puck / max(with_puck_xy, 1):.1f}%)")
        print(f"  distinct zones used  {distinct_boxes:>12,}")

        cur.execute(
            sql.SQL("select boxid, count(*) from {} group by boxid order by count(*) desc limit 5").format(
                sql.Identifier(TRACKING_TABLE)
            )
        )
        print("  busiest player boxes:")
        for zone, count in cur.fetchall():
            print(f"    {zone or 'NULL':6} {count:>12,}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    args = parser.parse_args(argv)

    zones = zones_from_geojson()
    print(f"{len(zones)} zones in {ZONES_FILE.name}")
    if args.dry_run:
        print("would: enable postgis, rebuild bdc2026_rink_zones, add the two columns,")
        print("       then run one UPDATE per game")
        return 0

    started = time.time()
    pg = connect(database_url())
    with pg:
        print("\nzones")
        ensure_zones(pg, zones)

        print("\ncolumns")
        add_columns(pg)
        print(f"  {TRACKING_TABLE}.boxid, boxid_puck")

        print("\ngames")
        for game_id in games(pg):
            if already_done(pg, game_id):
                print(f"  {game_id}  already done")
                continue
            rows, seconds = fill_game(pg, game_id)
            print(f"  {game_id}  {rows:>9,} rows  {seconds:>6.0f}s", flush=True)

        print("\nstatistics")
        with pg.cursor() as cur:
            cur.execute(
                sql.SQL("analyze {} (boxid, boxid_puck)").format(sql.Identifier(TRACKING_TABLE))
            )
            print("  analyzed boxid, boxid_puck")

        report(pg)

    print(f"\ndone in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
