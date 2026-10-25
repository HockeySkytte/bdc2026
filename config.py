"""Runtime configuration for the tracking app.

Same shape as the event app in ``challenge/``: branding lives here rather than in
the templates, so the whole UI follows ``ACCENT`` and the two logo files without
touching markup.

One deliberate difference: an unset ``APP_PASSWORD`` leaves this site **open**
rather than locked.  The event app fails closed because it was a private
submission; this one is a public analysis site, and a fresh deploy should not
come up showing a password box nobody can get past.  Set ``APP_PASSWORD`` and
the gate switches on.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

#: Calgary Flames red.  Drives every accent in the UI.
ACCENT = "#c8102e"


@dataclass(frozen=True)
class Config:
    # ── Branding ──
    app_title_top: str = "Calgary"
    app_title_bottom: str = "Flames"
    app_title_note: str = "Tracking"
    accent: str = ACCENT
    #: The white-outlined mark is the one built for dark backgrounds, which is
    #: what the sidebar gradient is.
    logo_url: str = "/static/flames-dark.svg"
    favicon_url: str = "/static/flames-light.svg"

    # ── Access ──
    password: str = ""
    secret_key: str = ""
    cookie_name: str = "tracking_access"
    cookie_max_age: int = 60 * 60 * 24 * 7

    # ── Data ──
    data_dir: Path = field(default_factory=lambda: BASE_DIR / "data")
    #: The aggregates the pages read.  Written by ``build_sample.py`` today and
    #: by the Supabase loader later; the shape is the contract between them.
    summary_json: Path = field(
        default_factory=lambda: BASE_DIR / "data" / "sample_summary.json"
    )
    #: Where the raw tracking files live, for ``build_sample.py`` only.
    tracking_dir: Path = field(
        default_factory=lambda: BASE_DIR.parent / "data" / "bigdata_cup_2026"
    )

    # ── Supabase ──
    #: The connection string the simulation reads frames from.  Empty locally is
    #: not an error until a page asks for frames: the aggregate pages still work
    #: from the sample JSON without a database.
    database_url: str = ""
    supabase_url: str = ""
    supabase_key: str = ""

    @property
    def password_configured(self) -> bool:
        return bool(self.password.strip())

    @property
    def secret_is_ephemeral(self) -> bool:
        return not os.environ.get("SECRET_KEY", "").strip()


def load_config() -> Config:
    """Read configuration from the environment, falling back to repo defaults."""
    data_dir = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
    return Config(
        password=os.environ.get("APP_PASSWORD", ""),
        secret_key=os.environ.get("SECRET_KEY", "").strip() or secrets.token_hex(32),
        data_dir=data_dir,
        summary_json=Path(
            os.environ.get("SUMMARY_JSON", data_dir / "sample_summary.json")
        ),
        tracking_dir=Path(
            os.environ.get("TRACKING_DIR", BASE_DIR.parent / "data" / "bigdata_cup_2026")
        ),
        supabase_url=os.environ.get("SUPABASE_URL", ""),
        supabase_key=os.environ.get("SUPABASE_KEY", ""),
        database_url=(
            os.environ.get("DATABASE_URL", "")
            or os.environ.get("RENDER_DATABASE_URL", "")
        ).strip(),
    )
