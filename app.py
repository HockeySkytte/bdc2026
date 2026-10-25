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

import gzip
import hmac
import logging
import threading
from functools import wraps
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
from jinja2 import StrictUndefined

import auth
import data_content
import data_source
import db
import rink
import simulation
import skaters
import zone_map
from config import Config, load_config

log = logging.getLogger("app")

#: Endpoints the gate lets through: the unlock form itself, the healthcheck the
#: platform polls, and the static files the gate page needs to render.
OPEN_ENDPOINTS = {"static", "unlock", "healthz"}

#: The Data page's tabs, in order.
DATA_TABS_ORDER = [
    ("method", "Data & method"),
    ("cleaning", "Cleaning"),
    ("supabase", "Supabase"),
]
DATA_TABS = {key for key, _label in DATA_TABS_ORDER}


def _safe_next(raw: str | None) -> str:
    """Only ever redirect to a path on this site.

    ``next`` arrives from the query string and the login form, so it is
    attacker-controlled.  Anything that is not a same-site absolute path is
    discarded, which stops the gate being used as an open redirect.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return url_for("theory")
    return raw


def create_app(config: Config | None = None) -> Flask:
    # A local .env, if there is one.  Deployments set real variables and have no
    # file, so this only ever fills in what the shell did not.
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass

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

    @app.after_request
    def compress(response):
        """Gzip the HTML and JSON.

        The coloured cleaning script is 367 kB of span-heavy markup and about a
        tenth of that compressed, so this is the difference between a slow page
        and a fast one. Written by hand rather than pulled in, because it is
        fifteen lines and one dependency less to deploy.
        """
        accept = request.headers.get("Accept-Encoding", "")
        if "gzip" not in accept or response.direct_passthrough:
            return response
        if response.status_code < 200 or response.status_code >= 300:
            return response
        if response.headers.get("Content-Encoding"):
            return response
        kind = response.headers.get("Content-Type", "")
        if not (kind.startswith("text/") or kind.startswith("application/json") or "javascript" in kind):
            return response

        body = response.get_data()
        if len(body) < 600:
            return response

        response.set_data(gzip.compress(body, 6))
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(response.get_data()))
        response.headers.add("Vary", "Accept-Encoding")
        return response

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

        # The data is static, so the default view of every game can be computed
        # while the visitor reads the first page they opened.  Started from the
        # hook rather than a route so it does not matter where they land.
        start_cache_warm()
        return None

    # ── Data ────────────────────────────────────────────────

    def page():
        """Load the summary, or render the page that says it is not there."""
        return data_source.load(config)

    # ── Simulation ──────────────────────────────────────────

    #: The slicer menus do not change while the process runs - the tables are
    #: loaded, not edited - so they are read once rather than on every page load.
    options_cache: dict = {}
    plan_cache: dict = {}
    PLAN_CACHE_SIZE = 24

    def sim_options() -> dict:
        if "value" not in options_cache:
            options_cache["value"] = simulation.filter_options()
        return options_cache["value"]

    def sim_filters(args) -> dict:
        return {
            "game": args.get("game", "").strip(),
            "strength": args.get("strength", "").strip(),
            "score": args.get("score", "").strip(),
            "team": args.get("team", "").strip(),
            "player_id": args.get("player_id", "").strip(),
            "jersey": args.get("jersey", "").strip(),
            "position": args.get("position", "").strip(),
            "side": args.get("side", "").strip(),
            "event": args.get("event", "").strip(),
            "before": args.get("before", "3").strip(),
            "after": args.get("after", "3").strip(),
        }

    # The theory behind the project, in the author's words, is the front page.
    @app.get("/", endpoint="theory")
    def theory_page():
        return render_template("theory.html", active_tab="theory")

    # The endpoint is named explicitly so it stays "simulation" even though the
    # function cannot be: a function called ``simulation`` would shadow the
    # module of the same name for the whole of create_app.
    @app.get("/simulation", endpoint="simulation")
    def simulation_page():
        try:
            options = sim_options()
        except db.DatabaseMissing as exc:
            return render_template("error.html", message=str(exc), active_tab="simulation"), 503

        return render_template(
            "simulation.html",
            active_tab="simulation",
            options=options,
            chunk_seconds=simulation.CHUNK_SECONDS,
            fps=simulation.FPS,
        )

    @app.get("/api/sim/plan")
    def sim_plan():
        filters = sim_filters(request.args)
        if not filters["game"]:
            return jsonify({"error": "Choose a game first."}), 400

        # A plan is pure: the same slicers always give the same windows.  Filtering
        # on strength or score state has to read the heap - about eight seconds on
        # a cold cache - so remembering a handful of plans makes going back to a
        # filter combination instant.
        key = tuple(sorted(filters.items()))
        if key in plan_cache:
            return jsonify(plan_cache[key])

        try:
            result = simulation.plan(filters)
        except db.DatabaseMissing as exc:
            return jsonify({"error": str(exc)}), 503

        if len(plan_cache) >= PLAN_CACHE_SIZE:
            plan_cache.pop(next(iter(plan_cache)))
        plan_cache[key] = result
        return jsonify(result)

    @app.get("/api/sim/frames")
    def sim_frames():
        filters = sim_filters(request.args)
        t0 = request.args.get("t0", type=int)
        t1 = request.args.get("t1", type=int)
        if not filters["game"] or t0 is None or t1 is None:
            return jsonify({"error": "game, t0 and t1 are required."}), 400
        # Never hand out more than one chunk, whatever the browser asks for.
        t1 = min(t1, t0 + simulation.CHUNK_SECONDS)
        try:
            return jsonify({"frames": simulation.frames(filters, t0, t1)})
        except db.DatabaseMissing as exc:
            return jsonify({"error": str(exc)}), 503

    @app.get("/puck-player", endpoint="puck_player")
    def puck_player_page():
        try:
            options = sim_options()
        except db.DatabaseMissing as exc:
            return render_template("error.html", message=str(exc), active_tab="puck_player"), 503

        return render_template(
            "puck_player.html",
            active_tab="puck_player",
            options=options,
            accent=config.accent,
        )

    #: The tables are static, so the default view of every game can be computed
    #: before anyone asks for it.  Twenty queries in a background thread at the
    #: first page load, and then the common case is a dictionary lookup instead of
    #: a few hundred megabytes of cold reads.
    warm = {"started": False}

    def start_cache_warm() -> None:
        if warm["started"]:
            return
        warm["started"] = True

        def run() -> None:
            try:
                # Only the game the page opens on.  Warming all twenty pairs took
                # minutes of continuous IO and starved real requests while it ran -
                # the maps timed out waiting behind it.  The rest warm as they are
                # asked for.
                targets = zone_map.warm_targets()[:1]
                for game_id, home, away in targets:
                    for team in (home, away):
                        zone_map.load(
                            zone_map.default_filters(game_id, team), config.accent
                        )
                log.info("zone map cache warmed for %s", targets[0][0] if targets else "nothing")
            except Exception as exc:                  # noqa: BLE001 - never fatal
                log.info("zone map warm-up stopped after %s", exc)

        threading.Thread(target=run, name="zone-warm", daemon=True).start()

    @app.get("/api/zone-map")
    def zone_map_api():
        # Its own parser: this page has slicers the simulation does not, and a
        # shared one silently dropped them.
        filters = {
            "team": request.args.get("team", "").strip(),
            "game": request.args.get("game", "").strip(),
            "jersey": request.args.get("jersey", "").strip(),
            "position": request.args.get("position", "").strip(),
            "side": request.args.get("side", "").strip(),
            "strength": request.args.get("strength", "").strip(),
            "score": request.args.get("score", "").strip(),
            "possession": request.args.get("possession", "").strip(),
            "player_possession": request.args.get("player_possession", "").strip(),
            "zones": [
                zone.strip()
                for zone in (request.args.get("zones") or "").split(",")
                if zone.strip()
            ],
        }
        if not filters["team"]:
            return jsonify({"error": "Choose a team first."}), 400
        try:
            return jsonify(zone_map.load(filters, config.accent))
        except db.DatabaseMissing as exc:
            return jsonify({"error": str(exc)}), 503
        except Exception as exc:                      # noqa: BLE001 - reported on the page
            log.exception("zone map failed")
            return jsonify({"error": f"The zone maps could not be built: {exc}"}), 500

    @app.get("/skaters", endpoint="skaters")
    def skaters_page():
        player_id = request.args.get("player", type=int)
        mode = request.args.get("mode", "totals")
        if mode not in ("totals", "rates"):
            mode = "totals"
        try:
            data = skaters.load(player_id, mode)
        except db.DatabaseMissing as exc:
            return render_template("error.html", message=str(exc), active_tab="skaters"), 503
        except Exception as exc:                      # noqa: BLE001 - reported on the page
            log.exception("skaters page failed")
            return render_template("error.html", message=str(exc), active_tab="skaters"), 500

        return render_template("skaters.html", active_tab="skaters", data=data)

    # ── Pages ───────────────────────────────────────────────

    @app.get("/data")
    def data_page():
        try:
            summary = page()
        except data_source.SummaryMissing as exc:
            return render_template("error.html", message=str(exc), active_tab=""), 500

        tab = request.args.get("tab", "method")
        if tab not in DATA_TABS:
            tab = "method"

        return render_template(
            "data.html",
            active_tab="data",
            tab=tab,
            tabs=DATA_TABS_ORDER,
            summary=summary,
            meta=summary.meta,
            cleaning_sql=data_content.cleaning_sql() if tab == "cleaning" else "",
            cleaning_html=data_content.cleaning_html() if tab == "cleaning" else "",
            cleaning_lines=data_content.line_count(data_content.cleaning_sql()) if tab == "cleaning" else 0,
            loader_script=data_content.loader_script() if tab == "supabase" else "",
            loader_html=data_content.loader_html() if tab == "supabase" else "",
            loader_lines=data_content.line_count(data_content.loader_script()) if tab == "supabase" else 0,
            code_css=data_content.style_defs() if tab in ("cleaning", "supabase") else "",
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
        return auth.clear_unlock(redirect(url_for("theory")))

    @app.get("/healthz")
    def healthz():
        """The platform polls this.

        It reports the sample summary and the database together, because the
        analysis pages cannot serve a number without the second one. A deploy
        whose DATABASE_URL is missing or wrong should fail its healthcheck rather
        than come up serving errors to a visitor.
        """
        try:
            data_source.load(config)
        except data_source.SummaryMissing as exc:
            return jsonify({"status": "degraded", "detail": str(exc)}), 503

        try:
            with db.cursor() as cur:
                cur.execute("select 1")
                cur.fetchone()
        except db.DatabaseMissing as exc:
            return jsonify({"status": "degraded", "database": str(exc)}), 503
        except Exception as exc:                      # noqa: BLE001 - reported, not raised
            return jsonify({"status": "degraded", "database": str(exc)}), 503

        return jsonify({"status": "ok", "database": "ok"}), 200

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
