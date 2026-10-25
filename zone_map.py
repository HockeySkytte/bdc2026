"""The two maps on the Puck vs. Player page.

One question, asked twice: where is the puck, and where are the players, in the
frames the slicers keep?

**Frame filters and row filters are different things, and the difference matters.**
The puck's zone, the strength state, the score state and who has possession are
facts about a *frame*.  Selecting a zone therefore means "keep the frames where
the puck was in this zone", and then show every player in those frames - not
"keep the player rows whose own zone is this zone", which would answer a
different question and put dots only inside the selection.

That is why every frame-level slicer is answered by ``bdc2026_frames``: one row
per frame, 780,000 of them, small enough to scan for a whole game in a moment.
The player maps then read the tracking table for those frames only.

**The view is attack-normalised.**  Stored coordinates have the home team
defending ``+x``, so a row is mirrored when the selected team was the home team in
that game - ``x`` and ``y`` both negated, the zone id swapped ``O`` to ``D`` -
which puts the selected team's offensive zone on the right.  The mirror keys on a
list of game ids rather than a join; see the note on ``_SWAP``.
"""

from __future__ import annotations

import logging

import db
import rink

log = logging.getLogger("zone_map")

#: ``+x`` is the ``O`` end in the geometry, so a team that defends ``+x`` sees its
#: stored ``D`` zones as ``O``.  Keyed on an array of game ids: joining the events
#: table instead put that subquery inside a nested loop, re-running it for every
#: tracking row, and took 36 seconds.
_SWAP = """
    case when {game} = any(%(mirror_games)s)
         then case left({column}, 1)
                  when 'O' then 'D'
                  when 'D' then 'O'
                  else left({column}, 1)
              end || substr({column}, 2)
         else {column}
    end
"""

#: How the team in possession slicer reads.  ``possession_team`` is NULL when
#: nobody was within reach of the puck, which is the third state.
POSSESSION = {"": "", "team": "team", "opponent": "opponent", "none": "none"}


def _swap(column: str, game: str = "f.game_id") -> str:
    return _SWAP.format(column=column, game=game)


def _mirror_games(filters: dict) -> list[str]:
    """The games in scope in which the selected team was the home team."""
    params: dict = {"team": filters["team"]}
    clause = ""
    if filters.get("game"):
        clause = "and game_id = %(game)s"
        params["game"] = filters["game"]
    with db.cursor() as cur:
        cur.execute(
            f"select distinct game_id from bdc2026_events_cleaned "
            f"where home_team = %(team)s {clause}",
            params,
        )
        return [row[0] for row in cur.fetchall()]


def _jerseys(filters: dict) -> list[str] | None:
    """Which jerseys on the selected team the player slicers keep."""
    wanted = []
    params: dict = {"team": filters["team"]}

    if filters.get("jersey"):
        wanted.append("player_jersey = %(jersey)s")
        params["jersey"] = filters["jersey"]
    if filters.get("position"):
        wanted.append("player_position = %(position)s")
        params["position"] = filters["position"]
    if filters.get("side"):
        wanted.append("side = %(side)s")
        params["side"] = filters["side"]

    if not wanted:
        return None

    with db.cursor() as cur:
        cur.execute(
            f"select player_jersey from bdc2026_players where team = %(team)s "
            f"and {' and '.join(wanted)}",
            params,
        )
        return [row[0] for row in cur.fetchall()]


#: Team names per game, and how many games in scope.  Both are fixed while the
#: process runs, and a Supabase round trip is about 95 ms.
_meta_cache: dict[tuple, dict] = {}


def _scope_meta(filters: dict) -> dict:
    key = (filters["team"], filters.get("game") or "")
    if key in _meta_cache:
        return _meta_cache[key]

    params = {"team": filters["team"]}
    clause = ""
    if filters.get("game"):
        clause = "and game_id = %(game)s"
        params["game"] = filters["game"]

    with db.cursor() as cur:
        cur.execute(
            f"""
            select count(distinct game_id),
                   count(distinct game_id) filter (where home_team = %(team)s),
                   min(home_team), min(away_team)
            from bdc2026_events_cleaned
            where %(team)s in (home_team, away_team) {clause}
            """,
            params,
        )
        games, home_games, home_team, away_team = cur.fetchone()

    meta = {
        "games": int(games or 0),
        "home_games": int(home_games or 0),
        "home_team": home_team,
        "away_team": away_team,
    }
    _meta_cache[key] = meta
    return meta


def _scope_sql(filters: dict, jerseys: list[str] | None) -> tuple[str, dict]:
    """The CTE of image ids every frame-level slicer keeps.

    Everything here is a fact about the frame: the game, the strength and score
    state, which zone the puck was in, who had possession, and - when a player is
    selected - whether that player was on the ice at all.
    """
    clauses = []
    params: dict = {"team": filters["team"]}

    clauses.append("1 = 1")
    if filters.get("game"):
        clauses.append("f.game_id = %(game)s")
        params["game"] = filters["game"]
    if filters.get("strength"):
        clauses.append("f.strength_state = %(strength)s")
        params["strength"] = filters["strength"]
    if filters.get("score"):
        clauses.append("f.score_state = %(score)s::smallint")
        params["score"] = filters["score"]

    # The zone slicer is the puck's zone, which is also what clicking the top map
    # does.  Both end up here, so the two ways of choosing a zone agree.
    if filters.get("zones"):
        clauses.append(
            f"{_swap('f.boxid_puck')} = any(%(zones)s)"
        )
        params["zones"] = filters["zones"]

    possession = POSSESSION.get(filters.get("possession") or "", "")
    if possession == "team":
        clauses.append("f.possession_team = %(team)s")
    elif possession == "opponent":
        clauses.append(
            "f.possession_team is not null and f.possession_team <> %(team)s"
        )
    elif possession == "none":
        clauses.append("f.possession_team is null")

    if filters.get("player_possession") == "yes":
        # The puck carrier is the selected player, or any player when no jersey
        # is chosen.
        if filters.get("jersey"):
            clauses.append("f.possession_jersey = %(jersey)s")
            params["jersey"] = filters["jersey"]
        else:
            clauses.append("f.possession_jersey is not null")
    elif filters.get("player_possession") == "no":
        clauses.append("f.possession_jersey is null")

    if jerseys is not None:
        if not jerseys:
            clauses.append("false")
        else:
            params["jerseys"] = jerseys
            clauses.append(
                "f.image_id in (select m.image_id from bdc2026_tracking_cleaned m "
                "where m.game_id = f.game_id and m.team = %(team)s "
                "and m.player_jersey = any(%(jerseys)s))"
            )

    return " and ".join(clauses), params


#: One statement, three answers.  Every branch reads the same scope CTE, so the
#: page costs a single round trip instead of five - and each of those round trips
#: is about 95 ms of pure latency.
_QUERY = """
-- MATERIALIZED on purpose: left to inline, the planner drove the join from the
-- tracking side and probed it once per scope row, which is random IO over half a
-- gigabyte.  Built once as a hash table it is a single sequential scan.
with scope as materialized (
    select f.image_id
    from bdc2026_frames f
    where {scope}
)
select
    'puck' as kind,
    {puck_zone} as zone,
    count(*) as frames,
    null::double precision as sum_x,
    null::double precision as sum_y
from bdc2026_frames f
where f.image_id in (select image_id from scope)
  and f.boxid_puck is not null
  {puck_game}
group by 2

union all

select
    'player_zone' as kind,
    {player_zone} as zone,
    count(*) as frames,
    null::double precision,
    null::double precision
from bdc2026_tracking_cleaned t
join scope s on s.image_id = t.image_id
where t.team = %(team)s
  and t.boxid is not null
  {tracking_game}
  {jersey_filter}
group by 2

union all

select
    'position' as kind,
    t.player_jersey as zone,
    count(*) as frames,
    sum(case when {mirror} then -t.x_feet else t.x_feet end) as sum_x,
    sum(case when {mirror} then -t.y_feet else t.y_feet end) as sum_y
from bdc2026_tracking_cleaned t
join scope s on s.image_id = t.image_id
where t.team = %(team)s
  and t.x_feet is not null
  and t.y_feet is not null
  and coalesce(t.player_jersey, '') <> ''
  {tracking_game}
  {jersey_filter}
group by 2
"""


#: The tables are loaded, not edited, so a result never goes stale.  A page that
#: has been looked at once comes back without touching the database, which is
#: what makes clicking around feel instant.
_CACHE: dict[tuple, dict] = {}
_CACHE_SIZE = 64


def _cache_key(filters: dict, accent: str) -> tuple:
    return (
        filters["team"], filters.get("game") or "", filters.get("jersey") or "",
        filters.get("position") or "", filters.get("side") or "",
        filters.get("strength") or "", filters.get("score") or "",
        filters.get("possession") or "", filters.get("player_possession") or "",
        tuple(sorted(filters.get("zones") or [])), accent,
    )


def default_filters(game_id: str, team: str) -> dict:
    """The slicers as the page opens on them: one team, one game, 5v5."""
    return {
        "team": team, "game": game_id, "jersey": "", "position": "", "side": "",
        "strength": "5v5", "score": "", "possession": "",
        "player_possession": "", "zones": [],
    }


def warm_targets() -> list[tuple[str, str, str]]:
    """Every game with its two teams, so the first look at any of them is free."""
    with db.cursor() as cur:
        cur.execute(
            "select distinct game_id, home_team, away_team from bdc2026_events_cleaned "
            "order by game_id"
        )
        return [(row[0], row[1], row[2]) for row in cur.fetchall()]


def load(filters: dict, accent: str) -> dict:
    """Both maps, for one set of slicers."""
    key = _cache_key(filters, accent)
    if key in _CACHE:
        return _CACHE[key]

    jerseys = _jerseys(filters)
    scope, params = _scope_sql(filters, jerseys)
    params["mirror_games"] = _mirror_games(filters)

    # Naming the game again lets the covering index narrow to one game before the
    # join to the scope.  Without it the planner reads the whole tracking table.
    tracking_game = "and t.game_id = %(game)s" if filters.get("game") else ""
    puck_game = "and f.game_id = %(game)s" if filters.get("game") else ""
    # A player, position or side slicer narrows the frames *and* whose positions
    # are drawn: picking a position and still being shown the whole team reads as
    # the filter not working.
    jersey_filter = "and t.player_jersey = any(%(jerseys)s)" if jerseys else ""

    sql = _QUERY.format(
        tracking_game=tracking_game,
        puck_game=puck_game,
        jersey_filter=jersey_filter,
        scope=scope,
        puck_zone=_swap("f.boxid_puck", "f.game_id"),
        player_zone=_swap("t.boxid", "t.game_id"),
        mirror="t.game_id = any(%(mirror_games)s)",
    )

    puck: dict[str, int] = {}
    player: dict[str, int] = {}
    positions: dict[str, list[float]] = {}

    with db.cursor() as cur:
        cur.execute(sql, params)
        for kind, zone, frames, sum_x, sum_y in cur.fetchall():
            if not zone:
                continue
            if kind == "puck":
                puck[zone] = int(frames)
            elif kind == "player_zone":
                player[zone] = int(frames)
            else:
                positions[zone] = [int(frames), float(sum_x or 0), float(sum_y or 0)]

    meta = _scope_meta(filters)
    result = {
        "team": filters["team"],
        "games": meta["games"],
        "home_games": meta["home_games"],
        "home_team": meta["home_team"],
        "away_team": meta["away_team"],
        "jerseys": jerseys,
        "zones": filters.get("zones") or [],
        "puck": _scale(puck, accent),
        "player": _scale(player, accent),
        "positions": _positions(positions),
    }
    if len(_CACHE) >= _CACHE_SIZE:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = result
    return result


def _positions(raw: dict[str, list[float]]) -> list[dict]:
    """Average position per jersey, in SVG coordinates, busiest first."""
    out = []
    for jersey, (frames, sum_x, sum_y) in raw.items():
        out.append(
            {
                "jersey": jersey,
                "x": round(sum_x / frames + rink.X_CENTRE, 2),
                "y": round(rink.Y_CENTRE - sum_y / frames, 2),
                "frames": frames,
            }
        )
    out.sort(key=lambda player: -player["frames"])
    return out


def _scale(counts: dict[str, int], accent: str) -> dict:
    """Attach a fill to every zone, on a scale shared by the whole map."""
    peak = max(counts.values(), default=0)
    return {
        "frames": sum(counts.values()),
        "peak": peak,
        "zones": {
            zone: {"count": value, "fill": rink.heat_colour(value, peak, accent)}
            for zone, value in counts.items()
        },
    }
