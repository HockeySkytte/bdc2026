"""Flask app for exploring the Big Data Cup 2026 tracking data.

The pages read one aggregate summary and draw it, mostly on a rink.  Two rules
shape the file:

* **One gate.**  When ``APP_PASSWORD`` is set, every page except the unlock form
  and the healthcheck sits behind a signed cookie.  With it unset the site is
  public - see ``auth.py`` for why that differs from the event app.
* **One data door.**  ``data_source`` is the only module that reads anything, so
  moving from the sample JSON to Supabase is a change in one file.

Run locally::

    python app.py                       # http://127.0.0.1:5003
    APP_PASSWORD=secret python app.py   # ...with the gate on
"""

from __future__ import annotations

import hmac
import logging
from functools import wraps

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
from jinja2 import StrictUndefined

import auth
import data_source
import rink
from config import Config, load_config

log = logging.getLogger("app")

#: Endpoints the gate lets through: the unlock form itself, the healthcheck the
#: platform polls, and the static files the gate page needs to render.
OPEN_ENDPOINTS = {"static", "unlock", "healthz"}


def _safe_next(raw: str | None) -> str:
    """Only ever redirect to a path on this site.

    ``next`` arrives from the query string and the login form, so it is
    attacker-controlled.  Anything that is not a same-site absolute path is
    discarded, which stops the gate being used as an open redirect.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return url_for("overview")
    return raw


def create_app(config: Config | None = None) -> Flask:
    config = config or load_config()
    app = Flask(__name__)
    app.config["SETTINGS"] = config
    app.secret_key = config.secret_key
    # A template variable that was never passed should be an error, not a blank
    # space.  Jinja renders nothing by default, which hides mistakes like an
    # include that forgot its argument.
    app.jinja_env.undefined = StrictUndefined

    if not config.password_configured:
        log.info("APP_PASSWORD is not set - every page is public.")
    if config.secret_is_ephemeral:
        log.warning(
            "SECRET_KEY is not set - a random one was generated for this process, "
            "so unlock cookies will not survive a restart."
        )

    @app.context_processor
    def inject_branding():
        return {
            "config": config,
            "page_title": f"{config.app_title_top} {config.app_title_bottom}",
            "rink_size": {"width": rink.RINK_WIDTH, "height": rink.RINK_HEIGHT},
            "gated": config.password_configured,
        }

    def render_gate(error: str = "", next_url: str | None = None):
        return render_template(
            "gate.html",
            error=error,
            next_url=_safe_next(next_url),
            locked_out=False,
        )

    @app.before_request
    def require_unlock():
        """One gate for every page, rather than a check copied into each route."""
        if request.endpoint in OPEN_ENDPOINTS:
            return None
        if not auth.is_unlocked():
            return render_gate(next_url=request.full_path)
        return None

    # ── Data ────────────────────────────────────────────────

    def page():
        """Load the summary, or render the page that says it is not there."""
        return data_source.load(config)

    # ── Pages ───────────────────────────────────────────────

    @app.get("/")
    def overview():
        try:
            summary = page()
        except data_source.SummaryMissing as exc:
            return render_template("error.html", message=str(exc), active_tab=""), 500

        return render_template(
            "overview.html",
            active_tab="overview",
            summary=summary,
            meta=summary.meta,
            periods=summary.periods,
        )

    @app.get("/possession")
    def possession():
        try:
            summary = page()
        except data_source.SummaryMissing as exc:
            return render_template("error.html", message=str(exc), active_tab=""), 500

        held = summary.possession
        return render_template(
            "possession.html",
            active_tab="possession",
            summary=summary,
            meta=summary.meta,
            by_team=data_source.share_bars(held["by_team"], "share"),
            players=data_source.bars(held["top_players"], "frames"),
            by_strength=data_source.bars(held["by_strength"], "possessed_frames"),
        )

    @app.get("/pressure")
    def pressure():
        try:
            summary = page()
        except data_source.SummaryMissing as exc:
            return render_template("error.html", message=str(exc), active_tab=""), 500

        return render_template(
            "pressure.html",
            active_tab="pressure",
            summary=summary,
            meta=summary.meta,
            buckets=data_source.share_bars(summary.pressure["buckets"], "share"),
            roles=data_source.share_bars(data_source.roles(summary), "share"),
            by_team=data_source.bars(summary.pressure["by_team"], "mean_distance"),
        )

    @app.get("/space")
    def space():
        try:
            summary = page()
        except data_source.SummaryMissing as exc:
            return render_template("error.html", message=str(exc), active_tab=""), 500

        return render_template(
            "space.html",
            active_tab="space",
            summary=summary,
            meta=summary.meta,
            cells=data_source.heat(summary, config.accent),
            peak=summary.space["peak"],
            zones=data_source.bars(summary.space["zones"], "frames"),
        )

    @app.get("/situations")
    def situations():
        try:
            summary = page()
        except data_source.SummaryMissing as exc:
            return render_template("error.html", message=str(exc), active_tab=""), 500

        return render_template(
            "situations.html",
            active_tab="situations",
            summary=summary,
            meta=summary.meta,
            rows=data_source.bars(summary.possession["by_strength"], "possessed_frames"),
        )

    @app.get("/data")
    def data_page():
        try:
            summary = page()
        except data_source.SummaryMissing as exc:
            return render_template("error.html", message=str(exc), active_tab=""), 500

        return render_template(
            "data.html",
            active_tab="data",
            summary=summary,
            meta=summary.meta,
        )

    # ── Files ───────────────────────────────────────────────

    @app.get("/download/summary")
    def download_summary():
        """Hand over the aggregates the pages are drawn from."""
        path = summary_path()
        return send_file(
            path, as_attachment=True, download_name=path.name, mimetype="application/json"
        )

    def summary_path():
        path = data_source.load(config).path
        if not path.exists():
            abort(404)
        return path

    # ── Gate ────────────────────────────────────────────────

    @app.post("/unlock")
    def unlock():
        next_url = _safe_next(request.form.get("next"))
        expected = config.password.strip()

        # Nothing to match when no password is configured; the site is open.
        if not expected:
            return redirect(next_url)

        supplied = request.form.get("password", "")
        # Constant-time compare so the gate does not leak the password by timing.
        if not hmac.compare_digest(supplied, expected):
            log.warning("rejected an unlock attempt from %s", request.remote_addr)
            return render_gate(error="Incorrect password.", next_url=next_url)

        return auth.issue_unlock(redirect(next_url))

    @app.post("/lock")
    def lock():
        return auth.clear_unlock(redirect(url_for("overview")))

    @app.get("/healthz")
    def healthz():
        """The platform polls this.  It reports the app and the data together."""
        try:
            data_source.load(config)
        except data_source.SummaryMissing as exc:
            return jsonify({"status": "degraded", "detail": str(exc)}), 503
        return jsonify({"status": "ok"}), 200

    @app.errorhandler(404)
    def not_found(_error):
        # Does not confirm what does or does not exist in the app.
        return render_gate(next_url=request.path), 404

    return app


app = create_app()


if __name__ == "__main__":
    import os

    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", 5003)),
        debug=os.environ.get("FLASK_DEBUG", "") == "1",
    )
