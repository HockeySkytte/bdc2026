"""Rink geometry, and the cells the position map is drawn from.

The tracking feed gives positions in feet from the middle of the rink: x from
-100 (own goal line) to +100 (target goal line), y from -42.5 to +42.5.  The SVG
that draws them uses the same frame the event app does - the top left corner is
(0, 0) and y points down - so ``to_svg`` flips the vertical axis.

The position map is a plain density grid rather than the 58 boxes the event app
uses, because tracking data is a cloud of points and the interesting thing is
where the cloud is thick, not which named box a point falls in.
"""

from __future__ import annotations

RINK_WIDTH = 200.0
RINK_HEIGHT = 85.0

#: Half of each, so the middle of the rink lands in the middle of the SVG.
X_CENTRE = RINK_WIDTH / 2
Y_CENTRE = RINK_HEIGHT / 2

#: The grid the position map counts into.  Twenty columns across the length and
#: ten rows across the width gives 200 cells of 10 x 8.5 feet.
CELL_WIDTH = 10.0
CELL_HEIGHT = 8.5
COLUMNS = int(RINK_WIDTH / CELL_WIDTH)
ROWS = int(RINK_HEIGHT / CELL_HEIGHT)


def to_svg(x: float, y: float) -> tuple[float, float]:
    """Tracking coordinates -> SVG coordinates."""
    return round(float(x) + X_CENTRE, 2), round(Y_CENTRE - float(y), 2)


def cells() -> list[dict]:
    """Every cell of the grid, in SVG coordinates, left to right and top down."""
    out = []
    for row in range(ROWS):
        for column in range(COLUMNS):
            x0 = column * CELL_WIDTH
            y0 = row * CELL_HEIGHT
            out.append(
                {
                    "x": round(x0, 2),
                    "y": round(y0, 2),
                    "w": CELL_WIDTH,
                    "h": CELL_HEIGHT,
                    "cx": round(x0 + CELL_WIDTH / 2, 2),
                    "cy": round(y0 + CELL_HEIGHT / 2, 2),
                    # The rink coordinate of the cell's middle, so the template
                    # can say where a cell is without knowing about the flip.
                    "rink_x": round(x0 + CELL_WIDTH / 2 - X_CENTRE, 2),
                    "rink_y": round(Y_CENTRE - (y0 + CELL_HEIGHT / 2), 2),
                }
            )
    return out


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    value = hex_colour.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def heat_colour(count: int, peak: int, accent: str) -> str:
    """A fill for one cell, given the busiest cell on the map.

    The square root keeps the quiet half of the map visible: without it a single
    busy crease washes everything else out to nothing.
    """
    if count <= 0:
        return "rgba(255,255,255,0.03)"
    if peak <= 0:
        return "rgba(255,255,255,0.03)"
    share = (count / peak) ** 0.5
    red, green, blue = _rgb(accent)
    # Capped well short of opaque: the rink drawing sits underneath, and the
    # brightest cell should still let the boards and creases show through.
    return f"rgba({red},{green},{blue},{0.06 + 0.56 * share:.3f})"


def heat_map(counts: dict[str, int], accent: str) -> list[dict]:
    """Every grid cell, with its count and the fill to draw it in.

    ``counts`` is keyed by ``"column,row"`` so the summary can stay small: only
    the cells that were ever occupied are written out.
    """
    peak = max(counts.values(), default=0)
    out = []
    for index, cell in enumerate(cells()):
        column = index % COLUMNS
        row = index // COLUMNS
        count = int(counts.get(f"{column},{row}", 0))
        out.append(
            {
                **cell,
                "count": count,
                "fill": heat_colour(count, peak, accent),
            }
        )
    return out


def peak(counts: dict[str, int]) -> int:
    """The busiest cell, for the legend."""
    return max(counts.values(), default=0)
