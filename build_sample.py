"""Build the aggregate summary the pages read, from one real tracking game.

This is a development utility, not part of the app.  It exists for two reasons:

1. The pages need *something* to render before the database exists.  Rather than
   invent numbers, this reads one complete game out of the raw tracking files
   and computes the same aggregates the Supabase queries will return later.
2. It pins down that shape.  ``data/sample_summary.json`` is the contract: the
   keys below are what ``data_source.py`` reads and what the templates expect,
   so the database work has a target to hit.

    python build_sample.py               # writes data/sample_summary.json

The thresholds match the cleaned table, so the numbers here should agree with
what the SQL produces for the same game: a player is in possession when they are
the nearest player to the puck and within 6 feet of it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
TRACKING_DIR = BASE_DIR.parent / "data" / "bigdata_cup_2026"
OUT = BASE_DIR / "data" / "sample_summary.json"

#: The one game this sample is built from.  It is the only one in the drop with
#: four periods, so the overtime handling gets exercised.
GAME = "2025-11-28.Team.J.@.Team.K"

#: Period labels, and the seconds each one contributes, exactly as the cleaning
#: script maps them: regulation periods are 1200 seconds, overtime is 300.
PERIOD_ORDER = ["P1", "P2", "P3", "POT"]
PERIOD_LABEL = {"P1": "1", "P2": "2", "P3": "3", "POT": "OT"}
PERIOD_SECONDS = {"P1": 1200, "P2": 1200, "P3": 1200, "POT": 300}
PERIOD_OFFSET = {"P1": 0, "P2": 1200, "P3": 2400, "POT": 3600}

#: The camera table says which team had its goalie on the right in period 1, and
#: teams change ends each period, so the mapping is flipped in the other half.
GOALIE_ON_RIGHT_IN_P1 = "Away"

COLUMNS = [
    "Image Id",
    "Period",
    "Game Clock",
    "Player or Puck",
    "Team",
    "Player Id",
    "Player Jersey Number",
    "Rink Location X (Feet)",
    "Rink Location Y (Feet)",
]

#: How far a player may be from the puck and still be holding it.
POSSESSION_FEET = 6.0

#: Distance buckets for the pressure page.
DISTANCE_BUCKETS = [
    ("0-2 ft", 0, 2),
    ("2-4 ft", 2, 4),
    ("4-6 ft", 4, 6),
    ("6-10 ft", 6, 10),
    ("10-20 ft", 10, 20),
    ("20-40 ft", 20, 40),
    ("40+ ft", 40, 10_000),
]

CELL_WIDTH = 10.0
CELL_HEIGHT = 8.5
COLUMNS_IN_GRID = 20
ROWS_IN_GRID = 10


def _read_period(period: str) -> tuple[pd.DataFrame, int]:
    path = TRACKING_DIR / f"{GAME}.Tracking_{period}.csv"
    frame = pd.read_csv(path, usecols=COLUMNS, low_memory=False)
    frame = frame.rename(
        columns={
            "Image Id": "image_id",
            "Game Clock": "clock",
            "Player or Puck": "kind",
            "Team": "team",
            "Player Id": "player_id",
            "Player Jersey Number": "jersey",
            "Rink Location X (Feet)": "x",
            "Rink Location Y (Feet)": "y",
        }
    )
    for column in ("x", "y", "player_id"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["jersey"] = frame["jersey"].astype(str).str.strip()
    frame["team"] = frame["team"].astype(str).str.strip()
    frame["kind"] = frame["kind"].astype(str).str.strip()

    # Clock counts down inside a period, so elapsed time is the period length
    # minus the clock, plus the offset of the period itself.  A handful of rows
    # in the raw files have no clock at all (9,075 of 8.4 million across the
    # drop), and they cannot be placed in time, so they are dropped and counted.
    minutes = frame["clock"].astype(str).str.split(":", n=1, expand=True)
    seconds = pd.to_numeric(minutes[0], errors="coerce") * 60 + pd.to_numeric(
        minutes[1], errors="coerce"
    )
    frame["time_elapsed"] = PERIOD_SECONDS[period] - seconds + PERIOD_OFFSET[period]
    dropped = int(frame["time_elapsed"].isna().sum())
    frame = frame[frame["time_elapsed"].notna()].copy()

    # Mirror the periods where the home team is not already defending the
    # positive-x end, so the map faces one way all game.
    home_on_right = GOALIE_ON_RIGHT_IN_P1 == "Home"
    flipped = {"2", "OT"} if home_on_right else {"1", "3"}
    if PERIOD_LABEL[period] in flipped:
        frame["x"] = -frame["x"]
        frame["y"] = -frame["y"]

    frame["period"] = PERIOD_LABEL[period]
    return frame, dropped


def _strength_runs() -> pd.DataFrame:
    """The strength state of the game as a list of spells with a start and end."""
    events = pd.read_csv(TRACKING_DIR / f"{GAME}.Events.csv", low_memory=False)
    minutes = events["Clock"].str.split(":", n=1, expand=True)
    seconds = minutes[0].astype(int) * 60 + minutes[1].astype(int)
    length = events["Period"].map({1: 1200, 2: 1200, 3: 1200, 4: 300})
    offset = events["Period"].map({1: 0, 2: 1200, 3: 2400, 4: 3600})
    events["time_elapsed"] = length - seconds + offset
    events["strength"] = (
        events["Home_Team_Skaters"].astype(str) + "v" + events["Away_Team_Skaters"].astype(str)
    )
    events = events.sort_values("time_elapsed", kind="stable")
    changed = events["strength"].ne(events["strength"].shift())
    starts = events.loc[changed, ["time_elapsed", "strength"]].reset_index(drop=True)
    starts["end_time"] = starts["time_elapsed"].shift(-1).fillna(10_000)
    return starts


def build() -> dict:
    read = [_read_period(p) for p in PERIOD_ORDER]
    frames = [row[0] for row in read]
    dropped = sum(row[1] for row in read)
    frame = pd.concat(frames, ignore_index=True)

    game_id = str(frame["image_id"].iloc[0])[:26]
    away_team = game_id.split(" @ ")[0][11:]
    home_team = game_id.split(" @ ")[1]

    players = frame[frame["kind"] == "Player"].copy()
    puck = frame[frame["kind"] == "Puck"][["image_id", "x", "y"]].rename(
        columns={"x": "puck_x", "y": "puck_y"}
    )

    players = players.merge(puck, on="image_id", how="left")
    players["distance"] = np.hypot(players["x"] - players["puck_x"], players["y"] - players["puck_y"])
    players["rank_all"] = players.groupby("image_id")["distance"].rank(method="first")
    players["rank_team"] = players.groupby(["image_id", "team"])["distance"].rank(method="first")

    # The player holding the puck: nearest overall, and close enough to be
    # holding it rather than merely nearest.
    possession = players[(players["rank_all"] == 1) & (players["distance"] < POSSESSION_FEET)]
    possession_team = possession.set_index("image_id")["team"]

    total_frames = int(frame["image_id"].nunique())
    possession_frames = int(len(possession))

    # ── possession ──
    team_counts = possession["team"].value_counts()
    names = {"Home": home_team, "Away": away_team}
    by_team = [
        {
            "team": names.get(team, team),
            "frames": int(count),
            "share": round(float(count) / possession_frames, 4) if possession_frames else 0.0,
        }
        for team, count in team_counts.items()
    ]

    # Note: Player Id is a *track* id, not a person.  It is re-issued when the
    # tracker loses and re-acquires a player - one game has 819 of them for 41
    # players, 132 of them on the goalie alone.  So anything per player groups on
    # (team, jersey), which is the identity a human would recognise.  Within a
    # single frame the id is unique, which is why the possession join is safe.
    grouped = (
        possession.groupby(["team", "jersey"])
        .agg(frames=("image_id", "size"), mean_distance=("distance", "mean"))
        .reset_index()
        .sort_values("frames", ascending=False)
        .head(12)
    )
    top_players = [
        {
            "team": names.get(row.team, row.team),
            "jersey": row.jersey,
            "goalie": row.jersey == "Go",
            "frames": int(row.frames),
            "share": round(float(row.frames) / possession_frames, 4) if possession_frames else 0.0,
            "mean_distance": round(float(row.mean_distance), 2),
        }
        for row in grouped.itertuples()
    ]

    # ── pressure ──
    distances = players["distance"].dropna()
    buckets = []
    for label, low, high in DISTANCE_BUCKETS:
        count = int(((distances >= low) & (distances < high)).sum())
        buckets.append(
            {
                "label": label,
                "frames": count,
                "share": round(count / len(distances), 4) if len(distances) else 0.0,
            }
        )

    defenders = players[players["image_id"].map(possession_team).notna()].copy()
    defenders["poss_team"] = defenders["image_id"].map(possession_team)
    defenders = defenders[defenders["team"] != defenders["poss_team"]]
    primary = (defenders["rank_team"] == 1) | (defenders["distance"] < POSSESSION_FEET)
    roles = {"primary": int(primary.sum()), "secondary": int((~primary).sum())}

    by_team_pressure = []
    for team, group in players.groupby("team"):
        values = group["distance"].dropna()
        by_team_pressure.append(
            {
                "team": names.get(team, team),
                "mean_distance": round(float(values.mean()), 2) if len(values) else 0.0,
                "median_distance": round(float(values.median()), 2) if len(values) else 0.0,
                "within_six": int((values < POSSESSION_FEET).sum()),
            }
        )

    # ── space ──
    positioned = players.dropna(subset=["x", "y"])
    column = np.clip(((positioned["x"] + 100.0) // CELL_WIDTH).astype(int), 0, COLUMNS_IN_GRID - 1)
    row = np.clip(((42.5 - positioned["y"]) // CELL_HEIGHT).astype(int), 0, ROWS_IN_GRID - 1)
    cell_counts = (
        pd.DataFrame({"column": column, "row": row}).value_counts().sort_index()
    )
    cells = {f"{c},{r}": int(n) for (c, r), n in cell_counts.items()}

    # The mirroring above puts the home team's defensive end on the positive-x
    # side in every period, so the halves can be named for the teams rather than
    # for the picture.  Both teams' players appear in both halves; this is where
    # the play is, not who it belongs to.
    zones = [
        (f"{home_team} end", 25.0, 101.0),
        ("Neutral zone", -25.0, 25.0),
        (f"{away_team} end", -101.0, -25.0),
    ]
    zone_rows = [
        {
            "zone": label,
            "frames": int(((positioned["x"] >= low) & (positioned["x"] < high)).sum()),
        }
        for label, low, high in zones
    ]

    # ── thanks to strength ──
    runs = _strength_runs()
    possession_times = possession[["image_id", "time_elapsed", "team"]].copy()
    index = np.searchsorted(runs["time_elapsed"].to_numpy(), possession_times["time_elapsed"].to_numpy(), side="right") - 1
    possession_times["strength"] = runs["strength"].to_numpy()[index]
    frame_times = pd.DataFrame({"time_elapsed": frame.groupby("image_id")["time_elapsed"].min()})
    frame_index = np.searchsorted(runs["time_elapsed"].to_numpy(), frame_times["time_elapsed"].to_numpy(), side="right") - 1
    frame_times["strength"] = runs["strength"].to_numpy()[frame_index]

    by_strength = []
    for strength, group in possession_times.groupby("strength"):
        counts = group["team"].value_counts()
        home_frames = int(counts.get("Home", 0))
        away_frames = int(counts.get("Away", 0))
        total = home_frames + away_frames
        by_strength.append(
            {
                "strength": str(strength),
                "frames": int((frame_times["strength"] == strength).sum()),
                "possessed_frames": int(total),
                "home_share": round(home_frames / total, 4) if total else 0.0,
            }
        )
    by_strength.sort(key=lambda item: -item["frames"])

    period_rows = [
        {
            "period": PERIOD_LABEL[p],
            "frames": int(frames[i]["image_id"].nunique()),
            "player_frames": int((frames[i]["kind"] == "Player").sum()),
        }
        for i, p in enumerate(PERIOD_ORDER)
    ]

    return {
        "meta": {
            "source": "sample",
            "label": "One game, every period",
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "game_id": game_id,
            "date": game_id[:10],
            "home_team": home_team,
            "away_team": away_team,
            "periods": [PERIOD_LABEL[p] for p in PERIOD_ORDER],
            "frames": total_frames,
            "player_frames": int(len(players)),
            "puck_frames": int(len(puck)),
            "goalie_frames": int((players["jersey"] == "Go").sum()),
            "skater_frames": int((players["jersey"] != "Go").sum()),
            "players": int(players.groupby(["team", "jersey"]).ngroups),
            "track_ids": int(players["player_id"].nunique()),
            "possession_frames": possession_frames,
            "possession_rate": round(possession_frames / total_frames, 4) if total_frames else 0.0,
            "possession_feet": POSSESSION_FEET,
            "dropped_rows": dropped,
        },
        "periods": period_rows,
        "possession": {
            "by_team": by_team,
            "top_players": top_players,
            "by_strength": by_strength,
        },
        "pressure": {
            "buckets": buckets,
            "roles": roles,
            "by_team": by_team_pressure,
        },
        "space": {
            "cells": cells,
            "zones": zone_rows,
            "peak": int(max(cells.values(), default=0)),
        },
    }


def main() -> None:
    summary = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    meta = summary["meta"]
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")
    print(f"  game       {meta['game_id']}")
    print(f"  frames     {meta['frames']:,}")
    print(f"  player     {meta['player_frames']:,} rows, {meta['players']} players")
    print(f"  possession {meta['possession_frames']:,} frames "
          f"({meta['possession_rate']:.1%} of them)")


if __name__ == "__main__":
    main()
