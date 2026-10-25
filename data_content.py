"""The long documents the Data page shows: the cleaning SQL and the loader.

Both are read from the files themselves rather than pasted into a template, so
the page can never drift from what actually ran.  Reading is cached on the file's
modification time, which means an edit shows up on the next request without a
restart.

The colouring is done here rather than in the browser: Pygments runs once per
file and its output is cached with the text, so the page ships coloured markup
and no JavaScript or CDN is involved.  The palette is one-dark, which is close to
the dark theme the pages are already in.
"""

from __future__ import annotations

from pathlib import Path

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name

BASE_DIR = Path(__file__).resolve().parent

CLEANING_SQL = BASE_DIR / "data_cleaning.sql"
LOADER = BASE_DIR / "load_supabase.py"

#: The class Pygments writes around the block, and the scope its CSS is written
#: against.  Both are "code" so the stylesheet cannot leak into the rest of the
#: page.
CSS_CLASS = "code"

#: Close to VS Code's dark theme.
STYLE = "one-dark"

_cache: dict[str, tuple[float, str]] = {}
_coloured: dict[tuple[str, float], str] = {}


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    stamp = path.stat().st_mtime
    cached = _cache.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    text = path.read_text(encoding="utf-8")
    _cache[str(path)] = (stamp, text)
    return text


def _formatter() -> HtmlFormatter:
    return HtmlFormatter(style=STYLE, cssclass=CSS_CLASS)


def _coloured_html(path: Path, language: str) -> str:
    text = _read(path)
    if not text:
        return ""
    stamp = path.stat().st_mtime
    key = (str(path), stamp)
    if key not in _coloured:
        lexer = get_lexer_by_name(language, stripnl=False)
        _coloured[key] = highlight(text, lexer, _formatter())
    return _coloured[key]


def cleaning_sql() -> str:
    return _read(CLEANING_SQL)


def loader_script() -> str:
    return _read(LOADER)


def cleaning_html() -> str:
    """The cleaning script as coloured HTML."""
    return _coloured_html(CLEANING_SQL, "sql")


def loader_html() -> str:
    """The loader as coloured HTML."""
    return _coloured_html(LOADER, "python")


def style_defs() -> str:
    """The stylesheet for the palette, scoped to the code block.

    Pygments writes one unscoped rule (``pre { line-height: 125% }``) alongside
    the scoped ones, which would restyle every pre element on the page.  It is
    prefixed here so the whole sheet is contained.
    """
    lines = []
    for line in _formatter().get_style_defs("." + CSS_CLASS).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith((".", "@", "}", "/*")):
            line = "." + CSS_CLASS + " " + line
        lines.append(line)
    return "\n".join(lines)


def line_count(text: str) -> int:
    return len(text.splitlines())
