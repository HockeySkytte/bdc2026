"""The Skaters page, built the way the main Moncton app builds its player card.

Three pieces of that design are what make the bars mean something:

*   the length of a bar is the player's *percentile* within the comparison group,
    not the raw number, so a bar is comparable across metrics;
*   the colour is a red-white-blue gradient on that percentile, so the shape and
    the colour say the same thing;
*   metrics where less is better - shots against, goals against, the share of
    defensive time spent as the primary defender - have the percentile flipped
    before either is drawn, so a long blue bar is always good.

The comparison group is the player's own position.  That is the only fair pool:
a defenceman's distance to his own net means nothing next to a forward's, and
averaging the two together hides both.
"""

from __future__ import annotations

import logging

import db

log = logging.getLogger("skaters")

#: Below this the numbers rest on a handful of shifts and say more about ice time
#: than about the player.
MIN_TRACKED_SECONDS = 100

#: Group, then rows of (key, label, unit, better).  ``better`` is "down" where a
#: smaller number is the good one; everything else is "up".
GROUPS = [
    ("Ice time", [
        ("tracked_time", "Tracked time", "seconds", "up"),
        ("time_dz", "Defensive zone time", "seconds", "up"),
        ("time_oz", "Offensive zone time", "seconds", "up"),
        ("oz_share", "Share in the offensive zone", "percent", "up"),
    ]),
    ("Defending", [
        ("time_dz_def", "Defending in own zone", "seconds", "up"),
        ("time_dz_primary", "Time as primary defender", "seconds", "up"),
        ("percentage_primary", "Share as primary defender", "percent", "down"),
    ]),
    ("Positioning", [
        ("dist_net_dz_def", "Distance to own net, defending", "feet", "up"),
        ("dist_net_primary", "Distance to own net, primary", "feet", "up"),
        ("puck_dist_net_primary", "Puck distance to own net, primary", "feet", "up"),
        ("dist_puck_primary", "Distance to the puck, primary", "feet", "up"),
        ("dist_puck_secondary", "Distance to the puck, secondary", "feet", "up"),
    ]),
    ("Shots while primary", [
        ("blocked_primary", "Blocked", "count", "down"),
        ("missed_primary", "Missed", "count", "down"),
        ("on_net_primary", "On net", "count", "down"),
        ("goal_primary", "Goals", "count", "down"),
    ]),
    ("Shots while on the ice", [
        ("blocked_against", "Blocked", "count", "down"),
        ("missed_against", "Missed", "count", "down"),
        ("on_net_against", "On net", "count", "down"),
        ("goal_against", "Goals", "count", "down"),
    ]),
    ("Position", [
        ("x_avg", "Average x, attacking positive", "feet", "up"),
        ("x_avg_dz_def", "Average x while defending", "feet", "up"),
        ("x_avg_oz_off", "Average x while attacking", "feet", "up"),
    ]),
]

POSITION_NAMES = {
    "F": "forwards",
    "D": "defence",
    "G": "goalies",
    "Undetermined": "players without a recorded position",
}

COLUMNS = (
    "player_id, player_jersey, team, player_position, side, tracked_time, "
    "time_dz, time_oz, time_dz_def, time_dz_primary, x_avg, x_avg_dz_def, "
    "x_avg_oz_off, dist_puck_primary, dist_puck_secondary, puck_dist_net_primary, "
    "dist_net_dz_def, dist_net_primary, blocked_against, blocked_primary, "
    "missed_against, missed_primary, on_net_against, on_net_primary, "
    "goal_against, goal_primary"
)

TEXT_COLUMNS = {"player_id", "player_jersey", "team", "player_position", "side"}


def percentile_rank(values, target):
    """Where ``target`` sits in ``values``, 0 to 100, ties at the midpoint.

    The same convention the main app uses, so a bar here and a bar there agree.
    """
    if not values:
        return 50.0
    below = sum(1 for value in values if value < target)
    equal = sum(1 for value in values if value == target)
    return (below + equal * 0.5) / len(values) * 100.0


def _lerp(a, b, t):
    return int(round(a + (b - a) * t))


def percentile_color(pct):
    """Red at 0, white at 50, blue at 100 - the main app's gradient."""
    p = max(0.0, min(100.0, pct))
    if p <= 50.0:
        t = p / 50.0
        r, g, b = _lerp(220, 255, t), _lerp(38, 255, t), _lerp(38, 255, t)
    else:
        t = (p - 50.0) / 50.0
        r, g, b = _lerp(255, 37, t), _lerp(255, 99, t), _lerp(255, 235, t)
    return "#%02x%02x%02x" % (r, g, b)


def _format(value, unit, scaled=False):
    if value is None:
        return "-"
    if scaled:
        return "%.1f /60" % value
    if unit == "seconds":
        return "%.1f min" % (value / 60.0)
    if unit == "feet":
        return "%.1f ft" % value
    if unit == "percent":
        return "%.1f%%" % value
    return "%.0f" % value


def _scaled(row, key, unit, rates):
    """The value the bar should be built from.

    Per 60 divides by the player's tracked time, which is what makes two players
    with different shifts comparable: a defenceman who plays twice as much will
    always accumulate twice the defensive zone time, and that is ice time
    talking, not defending.

    Four things are left alone.  Tracked time is the denominator.  Percentages
    have already cancelled the minutes out.  An average position in feet per hour
    is not a quantity.  Everything else - counts per 60 minutes, and durations in
    minutes per 60 minutes - is scaled.
    """
    raw = row.get(key)
    if raw is None:
        return None
    if not rates or key == "tracked_time":
        return float(raw)

    tracked = row.get("tracked_time") or 0.0
    if tracked <= 0:
        return None

    if unit == "count":
        return float(raw) * 3600.0 / float(tracked)
    if unit == "seconds":
        # Minutes of this per 60 minutes of ice: seconds * 60 / seconds tracked.
        return float(raw) * 60.0 / float(tracked)
    return float(raw)


def _roster():
    """Every player with enough tracked time to be worth comparing."""
    with db.cursor() as cur:
        cur.execute(
            "select " + COLUMNS + " from bdc2026_players "
            "where tracked_time is not null and tracked_time >= %d "
            "order by tracked_time desc" % MIN_TRACKED_SECONDS
        )
        names = [description[0] for description in cur.description]
        rows = [dict(zip(names, row)) for row in cur.fetchall()]

    for row in rows:
        # A ratio of two columns that are already there: keeping it in the table
        # invites the three drifting apart.
        defensive = row["time_dz_def"]
        primary = row["time_dz_primary"]
        row["percentage_primary"] = (
            round(100.0 * float(primary) / float(defensive), 2)
            if defensive and primary is not None and float(defensive) > 0
            else None
        )
        # Share of the player's own zone time spent in the offensive zone.
        own_zone = (row["time_dz"] or 0) + (row["time_oz"] or 0)
        row["oz_share"] = (
            round(100.0 * float(row["time_oz"]) / float(own_zone), 2)
            if own_zone else None
        )
        for name in names:
            if name not in TEXT_COLUMNS:
                row[name] = None if row[name] is None else float(row[name])
    return rows


def load(player_id=None, mode="totals"):
    rates = mode == "rates"
    roster = _roster()
    if not roster:
        return {
            "players": [], "choices": [], "selected": None, "sections": [],
            "minimum": MIN_TRACKED_SECONDS,
        }

    selected = None
    if player_id is not None:
        selected = next((row for row in roster if row["player_id"] == player_id), None)
    selected = selected or roster[0]

    peers = [row for row in roster if row["player_position"] == selected["player_position"]]
    group_name = POSITION_NAMES.get(
        selected["player_position"], "%s players" % selected["player_position"]
    )

    sections = []
    for title, metrics in GROUPS:
        rows = []
        for key, label, unit, better in metrics:
            raw = _scaled(selected, key, unit, rates)
            pool = [
                value
                for value in (_scaled(peer, key, unit, rates) for peer in peers)
                if value is not None
            ]
            if raw is None or not pool:
                continue
            pct = percentile_rank(pool, raw)
            if better == "down":
                pct = 100.0 - pct
            rows.append({
                "metric": key,
                "name": label,
                "display": _format(
                    raw, unit, rates and unit in ("count", "seconds") and key != "tracked_time"
                ),
                "pct": round(pct, 1),
                "color": percentile_color(pct),
            })
        if rows:
            sections.append({"category": title, "metrics": rows})

    return {
        "choices": [
            {
                "player_id": row["player_id"],
                "label": "#%s - %s - %s" % (
                    row["player_jersey"], row["team"], row["player_position"] or "?"
                ),
            }
            for row in roster
        ],
        "selected": selected,
        "peers": len(peers),
        "group_name": group_name,
        "sections": sections,
        "minimum": MIN_TRACKED_SECONDS,
        "mode": "rates" if rates else "totals",
    }
