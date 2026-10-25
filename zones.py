"""The 58 rink zones as SVG paths.

``HockeyRinkZones.geojson`` is the same file the challenge project and the
``boxid`` columns came from: ``O01``-``O26`` over the ``+x`` end, ``N01``-``N06``
across the middle, ``D01``-``D26`` over the ``-x`` end.  It is a clean partition
of the rink, so the polygons can be drawn side by side and tile the whole surface.

Coordinates go from tracking feet to the rink SVG the same way ``rink.to_svg``
does: ``x + 100`` across, ``42.5 - y`` down.

The polygons never change, so they are written once to
``static/rink-zones.json`` and fetched by the browser, which then caches them.
Generating them per request would put about 65 KB of path data in every page.

    python zones.py            # rewrite static/rink-zones.json
"""

from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ZONES_FILE = BASE_DIR.parent / "HockeyRinkZones.geojson"
OUTPUT_FILE = BASE_DIR / "static" / "rink-zones.json"

RINK_WIDTH = 200.0
RINK_HEIGHT = 85.0


def path_for(ring: list[list[float]]) -> str:
    """One polygon ring as SVG path data, in a 0 0 200 85 view box."""
    points = []
    for x, y in ring:
        svg_x = float(x) + RINK_WIDTH / 2
        svg_y = RINK_HEIGHT / 2 - float(y)
        points.append(f"{svg_x:.2f} {svg_y:.2f}")
    return "M" + "L".join(points) + "Z"


def zones() -> list[dict]:
    """Every zone, in file order, as ``{"id": ..., "d": ...}``."""
    raw = json.loads(ZONES_FILE.read_text(encoding="utf-8"))
    features = raw.get("features") or []
    if not features:
        raise SystemExit(f"no features in {ZONES_FILE}")

    out = []
    seen = set()
    for feature in features:
        zone_id = str((feature.get("properties") or {}).get("id") or "").strip()
        if not zone_id or zone_id in seen:
            raise SystemExit(f"missing or duplicate zone id in {ZONES_FILE}: {zone_id!r}")
        seen.add(zone_id)
        ring = feature["geometry"]["coordinates"][0]
        out.append({"id": zone_id, "d": path_for(ring)})
    return out


def attack_id(stored: str, mirrored: bool) -> str:
    """The same zone, labelled for a team attacking to the right.

    The stored frame has the home team defending ``+x``, and the geometry calls
    ``+x`` the ``O`` end.  For the home team that makes ``O`` their *defensive*
    end, so their view swaps the two ends: stored ``D05`` is shown as ``O05``.
    The away team already attacks ``+x`` and needs no swap.
    """
    if not mirrored or not stored:
        return stored
    if stored[0] == "O":
        return "D" + stored[1:]
    if stored[0] == "D":
        return "O" + stored[1:]
    return stored


def write_static() -> Path:
    """Write the path data the browser fetches once and caches."""
    document = {"viewBox": f"0 0 {RINK_WIDTH:.0f} {RINK_HEIGHT:.0f}", "zones": zones()}
    OUTPUT_FILE.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    return OUTPUT_FILE


if __name__ == "__main__":
    written = write_static()
    data = json.loads(written.read_text(encoding="utf-8"))
    print(f"{len(data['zones'])} zones -> {written} ({written.stat().st_size:,} bytes)")
