"""Read the frames the simulation plays.

The page does not ship a game to the browser.  A game is about 743,000 tracking
rows - 78,000 frames - so the browser asks for one short window at a time and the
work happens here.

Three shapes of query, each shaped by the indexes the tables have:

``filter_options``  the menus for the slicer panel, read from the small tables.
``plan``            which frames match the slicers, answered as runs of seconds
                    rather than a list of 78,000 frame numbers.
``frames``          the coordinates for one short window.

The player slicers are resolved first, against ``bdc2026_players`` - 270 rows -
into a list of ``player_id``.  That keeps the join out of the tracking query: a
filter that means "the players from Team K who shoot left" becomes an array of
ids, and the frame query only has to ask whether one of them was on the ice.
"""

from __future__ import annotations

import logging

import db

log = logging.getLogger("simulation")

#: The tracker samples about thirty times a second, and the release's frame
#: counter follows it.  The simulation plays at this rate at 1x speed.
FPS = 30

#: Longest window handed to the browser in one request.  Ten seconds is about
#: three hundred frames, which is a comfortable payload and enough that the
#: prefetch stays well ahead of playback.
CHUNK_SECONDS = 10

#: Every slicer value that means "any".  Empty string from a form select.
ANY = ""


def _frame_filters(filters: dict) -> tuple[list[str], dict]:
    """Conditions on the frame itself: these are the same on every row of it.

    Note what is *not* here: the event type.  An event slicer decides which
    windows to play (see ``_event_windows``), not which rows to return - putting
    ``event = 'Goal'`` in this list reduced a six second lead-in to the single
    frame the goal was logged on, and the simulation jumped from that frame
    straight to the next goal.
    """
    clauses: list[str] = []
    params: dict = {}

    if filters.get("strength"):
        clauses.append("strength_state = %(strength)s")
        params["strength"] = filters["strength"]
    if filters.get("score"):
        clauses.append("score_state = %(score)s::smallint")
        params["score"] = filters["score"]

    return clauses, params


def resolve_players(filters: dict) -> list[int] | None:
    """Which ``player_id`` the player slicers select.

    Returns ``None`` when no player slicer is set, which means "no restriction"
    rather than "nothing matched" - the two must not be confused, or an untouched
    panel would simulate nothing.

    ``side`` is the estimated side of the ice the player plays on (L, R or C) as
    the players table records it.  It is not handedness, and it is not the
    Home/Away venue the tracking rows carry.
    """
    wanted = {
        "team": filters.get("team"),
        "player_jersey": filters.get("jersey"),
        "player_position": filters.get("position"),
        "side": filters.get("side"),
        "player_id": filters.get("player_id"),
    }
    active = {key: value for key, value in wanted.items() if value}
    if not active:
        return None

    where = " and ".join(f"{column} = %(value_{column})s" for column in active)
    params = {f"value_{column}": value for column, value in active.items()}
    with db.cursor() as cur:
        cur.execute(f"select player_id from bdc2026_players where {where} order by player_id", params)
        return [row[0] for row in cur.fetchall()]


def filter_options() -> dict:
    """The menus for the slicer panel.  Read from the small tables only."""
    with db.cursor() as cur:
        cur.execute("select distinct game_id from bdc2026_events_cleaned order by game_id")
        games = [row[0] for row in cur.fetchall()]

        cur.execute(
            "select distinct strength_state from bdc2026_events_cleaned "
            "where strength_state is not null and strength_state <> '' order by 1"
        )
        strengths = [row[0] for row in cur.fetchall()]

        cur.execute("select distinct score_state from bdc2026_events_cleaned order by 1")
        scores = [row[0] for row in cur.fetchall()]

        cur.execute("select distinct event from bdc2026_events_cleaned order by 1")
        events = [row[0] for row in cur.fetchall()]

        cur.execute(
            "select player_id, player_jersey, team, player_position, side "
            "from bdc2026_players order by team, "
            "nullif(regexp_replace(player_jersey, '\\D', '', 'g'), '')::int nulls last, player_jersey"
        )
        players = [
            {
                "player_id": row[0],
                "jersey": row[1],
                "team": row[2],
                "position": row[3],
                "side": row[4],
            }
            for row in cur.fetchall()
        ]

    games_by_team: dict[str, list[str]] = {}
    with db.cursor() as cur:
        cur.execute(
            "select distinct game_id, home_team, away_team from bdc2026_events_cleaned "
            "order by game_id"
        )
        for game_id, home, away in cur.fetchall():
            games_by_team.setdefault(home, []).append(game_id)
            games_by_team.setdefault(away, []).append(game_id)

    # Which jerseys actually appear in each game.  From the small derived table,
    # because the events table misses players who never touched the puck and the
    # tracking table costs a scan of 7.4 million rows to ask.
    players_by_game: dict[str, list[list[str]]] = {}
    with db.cursor() as cur:
        cur.execute(
            "select game_id, team, player_jersey from bdc2026_game_players "
            "order by game_id, team, player_jersey"
        )
        for game_id, team, jersey in cur.fetchall():
            players_by_game.setdefault(game_id, []).append([team, jersey])

    return {
        "games": games,
        "strengths": strengths,
        "scores": scores,
        "events": events,
        "players": players,
        "teams": sorted({player["team"] for player in players}),
        "jerseys": sorted({player["jersey"] for player in players}, key=_jersey_key),
        "positions": sorted({player["position"] for player in players if player["position"]}),
        "sides": sorted({player["side"] for player in players if player["side"]}),
        "games_by_team": games_by_team,
        "players_by_game": players_by_game,
    }


def _jersey_key(jersey: str) -> tuple:
    """Sort jerseys as numbers, with 'Go' and blanks last."""
    return (0, int(jersey)) if jersey.isdigit() else (1, jersey)


#: Team names per game.  A round trip to Supabase costs about 95 ms, and these
#: never change while the process runs, so they are remembered rather than asked
#: for on every plan.
_team_cache: dict[str, dict] = {}


def game_teams(game_id: str) -> dict:
    """Home and away names for the heading, from the events table."""
    if game_id in _team_cache:
        return _team_cache[game_id]

    with db.cursor() as cur:
        cur.execute(
            "select home_team, away_team, min(game_date) from bdc2026_events_cleaned "
            "where game_id = %s group by home_team, away_team",
            (game_id,),
        )
        row = cur.fetchone()
    if not row:
        return {"home_team": "", "away_team": "", "game_date": None}

    found = {"home_team": row[0], "away_team": row[1], "game_date": row[2]}
    _team_cache[game_id] = found
    return found


def plan(filters: dict) -> dict:
    """Which seconds the simulation should play, as merged windows.

    Two modes.  With no event type chosen the windows are the runs of frames that
    match the slicers, so a player filter plays that player's shifts and skips
    the bench.  With an event type chosen the windows are that event's time plus
    the seconds asked for either side, and overlapping windows are merged so a
    burst of events does not replay the same second twice.
    """
    players = resolve_players(filters)
    game_id = filters["game"]
    teams = game_teams(game_id)

    if filters.get("event"):
        windows, events = _event_windows(filters, players)
        mode = "event"
        frame_count = sum(max(0, (end - start + 1)) for start, end in windows) * FPS
    else:
        runs = _full_windows(filters, players)
        windows = [[start, end] for start, end, _seconds in runs]
        events = []
        mode = "full"
        frame_count = sum(seconds for _start, _end, seconds in runs) * FPS

    return {
        "game_id": game_id,
        "mode": mode,
        "fps": FPS,
        "chunk_seconds": CHUNK_SECONDS,
        "windows": windows,
        "events": events,
        "frames": frame_count,
        "players_matched": players,
        **teams,
    }


def _full_windows(filters: dict, players: list[int] | None) -> list[tuple[int, int, int]]:
    """Runs of matching seconds, as ``(first, last, seconds)``.

    Planned per second rather than per frame on purpose.  A frame is a triple of
    (image_id, frame number, second) and the client only ever asks for a second
    range, so the second is the finest thing the plan needs - and ``distinct
    time_elapsed`` can be answered from the ``(game_id, time_elapsed)`` index
    without touching the heap.  Grouping by frame instead reads every row of the
    game: 843,000 of them, which took 26 seconds on this database.
    """
    clauses, params = _frame_filters(filters)
    params["game"] = filters["game"]

    where = " and ".join(["game_id = %(game)s", *clauses])
    if players is not None:
        if not players:
            return []
        where += " and player_id = any(%(players)s)"
        params["players"] = players

    sql = f"""
        with matching as (
            select distinct time_elapsed
            from bdc2026_tracking_cleaned
            where {where}
        ),
        runs as (
            select time_elapsed,
                   time_elapsed - row_number() over (order by time_elapsed) as run_id
            from matching
        )
        select min(time_elapsed) as first_second,
               max(time_elapsed) as last_second,
               count(*) as seconds
        from runs
        group by run_id
        order by first_second
    """
    with db.cursor() as cur:
        cur.execute(sql, params)
        return [(int(row[0]), int(row[1]), int(row[2])) for row in cur.fetchall()]


def _event_windows(
    filters: dict, players: list[int] | None
) -> tuple[list[list[int]], list[dict]]:
    """The event's second, widened by the before/after slicers, merged."""
    before = max(0, int(filters.get("before") or 0))
    after = max(0, int(filters.get("after") or 0))
    params: dict = {"game": filters["game"], "event": filters["event"]}

    clauses = ["e.game_id = %(game)s", "e.event = %(event)s"]
    if filters.get("strength"):
        clauses.append("e.strength_state = %(strength)s")
        params["strength"] = filters["strength"]
    if filters.get("score"):
        clauses.append("e.score_state = %(score)s::smallint")
        params["score"] = filters["score"]
    if filters.get("team"):
        clauses.append("e.team = %(team)s")
        params["team"] = filters["team"]
    if players is not None:
        if not players:
            return [], []
        # events.player_id is the resolved id, the same one the tracking rows and
        # the players table carry, so this compares like with like.
        clauses.append("e.player_id = any(%(players)s)")
        params["players"] = players

    sql = f"""
        select e.event_index, e.time_elapsed, e.team, e.event
        from bdc2026_events_cleaned e
        where {" and ".join(clauses)}
        order by e.time_elapsed, e.event_index
    """
    with db.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    events = [
        {"event_index": row[0], "second": int(row[1]), "team": row[2], "event": row[3]}
        for row in rows
    ]

    spans = sorted((max(0, event["second"] - before), event["second"] + after) for event in events)
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged, events


def frames(filters: dict, t_from: int, t_to: int) -> list[dict]:
    """Every player and the puck between two whole seconds of the same game.

    The player slicers pick the *frames*; every player in a chosen frame is then
    returned, because a rink with one dot on it answers nothing.
    """
    players = resolve_players(filters)
    if players == []:
        return []

    clauses, params = _frame_filters(filters)
    params.update({"game": filters["game"], "t0": t_from, "t1": t_to})

    where = " and ".join(
        ["game_id = %(game)s", "time_elapsed between %(t0)s and %(t1)s", *clauses]
    )
    columns = (
        "image_id, frame, time_elapsed, period, player_jersey, player_id, venue, "
        "x_feet, y_feet, x_puck_feet, y_puck_feet, player_in_possession"
    )
    order = "order by frame, player_id nulls last"

    with db.cursor() as cur:
        if players is None:
            # Without a player filter every row in the window qualifies, so the
            # frame list would be the window itself: one query instead of two.
            cur.execute(f"select {columns} from bdc2026_tracking_cleaned where {where} {order}", params)
            return _group_frames(cur.fetchall())

        params["players"] = players
        cur.execute(
            f"select distinct image_id from bdc2026_tracking_cleaned where {where} "
            "and player_id = any(%(players)s)",
            params,
        )
        image_ids = [row[0] for row in cur.fetchall()]
        if not image_ids:
            return []

        # Then every player in those frames, because a rink with one dot on it
        # answers nothing.
        cur.execute(
            f"select {columns} from bdc2026_tracking_cleaned "
            f"where image_id = any(%s) {order}",
            (image_ids,),
        )
        return _group_frames(cur.fetchall())


def _group_frames(rows) -> list[dict]:
    """Collapse one row per player into one row per frame."""
    out: list[dict] = []
    current: dict | None = None

    for (
        image_id, frame, time_elapsed, period, jersey, player_id,
        venue, x, y, puck_x, puck_y, in_possession,
    ) in rows:
        if current is None or current["image_id"] != image_id:
            current = {
                "image_id": image_id,
                "frame": int(frame),
                "t": int(time_elapsed),
                "period": str(period),
                "puck": None,
                "players": [],
            }
            out.append(current)

        if current["puck"] is None and puck_x is not None and puck_y is not None:
            current["puck"] = [round(float(puck_x), 2), round(float(puck_y), 2)]

        if x is None or y is None:
            continue
        current["players"].append(
            {
                "j": jersey or "",
                "s": "H" if venue == "Home" else "A",
                "x": round(float(x), 2),
                "y": round(float(y), 2),
                "d": int(player_id) if player_id is not None else None,
                "p": int(in_possession or 0),
            }
        )
    return out
