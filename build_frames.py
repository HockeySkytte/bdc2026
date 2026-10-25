#!/usr/bin/env python
"""Build ``bdc2026_frames``: one row per tracked frame.

The tracking table holds about eight rows per frame - one per player - and the
things that are true of the *frame* (the puck's zone, the strength state, who has
possession) are repeated on every one of them.  Asking those questions of 7.4
million rows when the answer lives in 780,000 is most of why the Puck vs. Player
page took seconds.

This table is that 780,000: the frame's own facts, once.  It is derived, so it can
be rebuilt at any time, and it is small enough that a query over a whole game is
a scan of a few megabytes.

    python build_frames.py

The page's frame-level slicers - game, strength, score, team in possession, player
in possession, zone - are all answered from this table.  The player maps still
read the tracking table, but only for the frames it returns.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import psycopg
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = "bdc2026_tracking_cleaned"
TARGET = "bdc2026_frames"


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
        url = url.replace(":6543", ":5432")
    return url


def build(pg) -> int:
    """Fill the table from the tracking rows.  Idempotent: it truncates first."""
    with pg.cursor() as cur:
        cur.execute(sql.SQL("truncate table {}").format(sql.Identifier(TARGET)))
        started = time.time()
        cur.execute(
            sql.SQL(
                """
                insert into {target}
                    (game_id, image_id, frame, time_elapsed, period,
                     boxid_puck, x_puck, y_puck,
                     strength_state, score_state,
                     possession_team, possession_jersey, players)
                select
                    game_id,
                    image_id,
                    min(frame),
                    min(time_elapsed),
                    min(period),
                    min(boxid_puck),
                    min(x_puck_feet),
                    min(y_puck_feet),
                    min(strength_state),
                    min(score_state),
                    -- The possessing team is the one whose rows carry the flag,
                    -- and NULL means nobody was within reach of the puck.
                    min(team) filter (where team_in_possession = 1),
                    min(player_jersey) filter (where player_in_possession = 1),
                    count(*) filter (where player_id is not null)
                from {source}
                group by game_id, image_id
                """
            ).format(target=sql.Identifier(TARGET), source=sql.Identifier(SOURCE))
        )
        inserted = cur.rowcount
        print(f"  {inserted:,} frames in {time.time() - started:.0f}s", flush=True)
    return inserted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.dry_run:
        print(f"would rebuild {TARGET} from {SOURCE}")
        return 0

    with psycopg.connect(
        database_url(), autocommit=True, prepare_threshold=None, connect_timeout=20
    ) as pg:
        with pg.cursor() as cur:
            cur.execute("set statement_timeout = 0")
            cur.execute("set maintenance_work_mem = '256MB'")
            cur.execute(
                sql.SQL(
                    """
                    create table if not exists {} (
                        game_id           text        not null,
                        image_id          varchar(35) not null primary key,
                        frame             integer     not null,
                        time_elapsed      smallint    not null,
                        period            varchar(3)  not null,
                        boxid_puck        varchar(4),
                        x_puck            double precision,
                        y_puck            double precision,
                        strength_state    varchar(10),
                        score_state       smallint,
                        possession_team   varchar(20),
                        possession_jersey varchar(3),
                        players           smallint    not null default 0
                    )
                    """
                ).format(sql.Identifier(TARGET))
            )
            print(f"{TARGET} ready")

        inserted = build(pg)

        # A tiny derived table: which jerseys actually appear in which game.  The
        # events table is not enough - three players in one game have no event at
        # all, including one with 23,719 tracked frames - and asking the tracking
        # table costs a scan of 7.4 million rows every time a game is chosen.
        with pg.cursor() as cur:
            cur.execute(
                """
                create table if not exists bdc2026_game_players (
                    game_id       varchar(30) not null,
                    team          varchar(20) not null,
                    player_jersey varchar(3)  not null,
                    primary key (game_id, team, player_jersey)
                )
                """
            )
            cur.execute("truncate table bdc2026_game_players")
            started = time.time()
            cur.execute(
                """
                insert into bdc2026_game_players (game_id, team, player_jersey)
                select distinct game_id, team, player_jersey
                from bdc2026_tracking_cleaned
                where player_jersey <> ''
                """
            )
            print(f"  {cur.rowcount:,} game/player rows in {time.time() - started:.0f}s", flush=True)
            cur.execute("analyze bdc2026_game_players")

        with pg.cursor() as cur:
            for statement in (
                f"create index if not exists ix_frames_game_zone on {TARGET} (game_id, boxid_puck)",
                f"create index if not exists ix_frames_game_state on {TARGET} (game_id, strength_state, score_state)",
                f"create index if not exists ix_frames_game_time on {TARGET} (game_id, time_elapsed)",
            ):
                started = time.time()
                cur.execute(statement)
                print(f"  index in {time.time() - started:.0f}s", flush=True)
            cur.execute(f"vacuum (analyze) {TARGET}")
            cur.execute(f"select count(*) from {TARGET}")
            print(f"  {cur.fetchone()[0]:,} rows after rebuild ({inserted:,} inserted)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
