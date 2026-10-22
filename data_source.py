"""Read the aggregates the pages ask for.

This is the only module that touches data.  Today it reads one JSON file written
by ``build_sample.py``; when the tables move to Supabase this file changes and
nothing else does, which is the point of keeping it in one place.

The shape it reads is the contract:

    meta       what the sample is, and how big it is
    periods    one row per period
    possession who holds the puck, by team, by player, by strength state
    pressure   how far players are from the puck, and who is defending
    space      a density grid of player positions over the rink

Nothing here invents a value.  A missing file is an error the page shows, not a
zero it quietly reports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import rink


class SummaryMissing(RuntimeError):
    """Raised when the summary has not been built yet."""


@dataclass(frozen=True)
class Summary:
    document: dict
    path: Path
    modified: str

    @property
    def meta(self) -> dict:
        return self.document["meta"]

    @property
    def periods(self) -> list[dict]:
        return self.document["periods"]

    @property
    def possession(self) -> dict:
        return self.document["possession"]

    @property
    def pressure(self) -> dict:
        return self.document["pressure"]

    @property
    def space(self) -> dict:
        return self.document["space"]

    @property
    def is_sample(self) -> bool:
        return self.meta.get("source") == "sample"

    def team_name(self, side: str) -> str:
        """Turn ``Home``/``Away`` into the team it means in this dataset."""
        return self.meta["home_team"] if side == "Home" else self.meta["away_team"]


@lru_cache(maxsize=2)
def _read(path_str: str, mtime: float, size: int) -> dict:
    """Read the JSON, cached until the file changes."""
    del mtime, size  # only there to make the cache key change with the file
    return json.loads(Path(path_str).read_text(encoding="utf-8"))


def load(config) -> Summary:
    """Load the summary, or say clearly that it has not been built."""
    path = Path(config.summary_json)
    if not path.exists():
        raise SummaryMissing(
            f"{path.name} not found in {path.parent}. Run `python build_sample.py`, "
            "or point SUMMARY_JSON at the file the database writes."
        )
    stat = path.stat()
    from datetime import datetime

    return Summary(
        document=_read(str(path), stat.st_mtime, stat.st_size),
        path=path,
        modified=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
    )


def bars(rows: list[dict], key: str) -> list[dict]:
    """Add a percentage of the largest value, for drawing bars."""
    peak = max((row.get(key, 0) for row in rows), default=0) or 1
    return [{**row, "pct": round(100 * row.get(key, 0) / peak, 1)} for row in rows]


def share_bars(rows: list[dict], key: str) -> list[dict]:
    """The same, for rows that already carry a share between 0 and 1."""
    return [{**row, "pct": round(100 * row.get(key, 0), 1)} for row in rows]


def heat(summary: Summary, accent: str) -> list[dict]:
    """The position grid, ready to draw."""
    return rink.heat_map(
        {str(k): int(v) for k, v in summary.space["cells"].items()}, accent
    )


def roles(summary: Summary) -> list[dict]:
    """Primary and secondary defenders as two rows, with a share each."""
    counted = summary.pressure["roles"]
    total = (counted.get("primary", 0) + counted.get("secondary", 0)) or 1
    return [
        {
            "role": "Primary",
            "frames": int(counted.get("primary", 0)),
            "share": round(counted.get("primary", 0) / total, 4),
        },
        {
            "role": "Secondary",
            "frames": int(counted.get("secondary", 0)),
            "share": round(counted.get("secondary", 0) / total, 4),
        },
    ]
