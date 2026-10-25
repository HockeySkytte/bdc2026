/* The simulation page.
 *
 * A game is 78,000 frames, so the browser never holds one.  The server answers
 * two questions: which seconds match the slicers (the "plan"), and what the
 * coordinates are for a short window of them.  This file just walks the plan and
 * draws whatever comes back.
 *
 * Playback is frame-accurate rather than wall-clock: the animation advance is
 * counted in frames, so a slow request pauses the clock instead of skipping
 * frames.  That is why the loop keeps a fractional playhead.
 */
(function () {
  "use strict";

  var form = document.getElementById("simSlicers");
  if (!form) return;

  var rink = document.getElementById("simRink");
  var playerLayer = document.getElementById("simPlayerLayer");
  var puckLayer = document.getElementById("simPuckLayer");
  var playButton = document.getElementById("simPlay");
  var restartButton = document.getElementById("simRestart");
  var speedSelect = document.getElementById("simSpeed");
  var clock = document.getElementById("simClock");
  var status = document.getElementById("simStatus");
  var bar = document.getElementById("simBar");
  var sub = document.getElementById("simSub");
  var title = document.getElementById("simTitle");
  var legendNote = document.getElementById("simLegendNote");
  var windowRow = document.getElementById("simWindow");
  var eventSelect = document.getElementById("fEvent");
  var gameSelect = document.getElementById("fGame");
  var teamSelect = document.getElementById("fTeam");
  var playerSelect = document.getElementById("fPlayerId");
  var playerDataNode = document.getElementById("simPlayerData");
  var playerData = playerDataNode ? JSON.parse(playerDataNode.textContent) : [];

  var CHUNK = Number(form.dataset.chunkSeconds) || 10;
  var FPS = Number(form.dataset.fps) || 30;
  var HALF_WIDTH = 100;
  var HALF_HEIGHT = 42.5;

  // Seconds at the start of each period, for the clock. The feed counts from
  // the first puck drop, so a period clock is a subtraction.
  var PERIOD_START = { "1": 0, "2": 1200, "3": 2400, "4": 3600 };

  var state = {
    plan: null,
    windows: [],
    windowIndex: 0,
    nextSecond: 0,
    buffer: [],
    current: null,
    played: 0,
    playhead: 0,
    playing: false,
    loading: false,
    finished: false,
    lastStamp: 0,
    reqId: 0
  };

  var nodes = new Map();
  var puckNode = null;
  var statusTimer = null;

  function say(message) {
    status.textContent = message;
  }

  function debouncedStatus(message) {
    if (statusTimer) clearTimeout(statusTimer);
    statusTimer = setTimeout(function () { say(message); }, 400);
  }

  function filters() {
    var query = new URLSearchParams();
    new FormData(form).forEach(function (value, key) {
      if (value !== "") query.set(key, value);
    });
    return query;
  }

  // ── Plan ──────────────────────────────────────────────────────────────

  function resetPlayback() {
    state.windows = [];
    state.windowIndex = 0;
    state.nextSecond = 0;
    state.buffer = [];
    state.current = null;
    state.played = 0;
    state.playhead = 0;
    state.finished = false;
    state.loading = false;
    setPlaying(false);
    draw();
    bar.style.width = "0%";
  }

  async function loadPlan() {
    var request = ++state.reqId;
    resetPlayback();
    var query = filters();
    if (!query.get("game")) {
      say("Choose a game.");
      return;
    }
    say("Reading the plan\u2026");

    var response, plan;
    try {
      response = await fetch("/api/sim/plan?" + query.toString());
      plan = await response.json();
    } catch (error) {
      say("Could not reach the server.");
      return;
    }
    if (request !== state.reqId) return;      // a newer filter change won
    if (plan.error) {
      say(plan.error);
      return;
    }

    state.plan = plan;
    state.windows = plan.windows || [];
    state.finished = state.windows.length === 0;
    state.nextSecond = state.windows.length ? state.windows[0][0] : 0;

    title.textContent = plan.home_team + " vs " + plan.away_team;
    if (plan.mode === "event") {
      sub.textContent = plan.events.length + " \u00d7 " + eventSelect.value +
        " \u00b7 the event and the seconds either side";
      legendNote.textContent = plan.events.length
        ? "one window per event"
        : "";
    } else {
      sub.textContent = "Every tracked frame that matches the slicers";
      legendNote.textContent = "";
    }

    if (!state.windows.length) {
      say("Nothing matches these slicers.");
      return;
    }

    say("Fetching frames\u2026");
    await fill();
    if (request !== state.reqId) return;
    advance();
    draw();
    setPlaying(true);
  }

  // ── Frames ────────────────────────────────────────────────────────────

  async function fill() {
    if (state.loading || state.finished) return;
    while (state.windowIndex < state.windows.length) {
      var window = state.windows[state.windowIndex];
      if (state.nextSecond > window[1]) {
        state.windowIndex += 1;
        state.nextSecond = state.windows[state.windowIndex]
          ? state.windows[state.windowIndex][0]
          : 0;
        continue;
      }

      var from = state.nextSecond;
      var to = Math.min(window[1], from + CHUNK - 1);
      var query = filters();
      query.set("t0", from);
      query.set("t1", to);

      state.loading = true;
      var response, data;
      try {
        response = await fetch("/api/sim/frames?" + query.toString());
        data = await response.json();
      } catch (error) {
        state.loading = false;
        say("Could not reach the server.");
        return;
      }
      state.loading = false;

      if (data.error) {
        say(data.error);
        return;
      }
      if (data.frames && data.frames.length) {
        Array.prototype.push.apply(state.buffer, data.frames);
      }
      state.nextSecond = to + 1;
      debouncedStatus(state.buffer.length + " frames ready");
      return;
    }
    state.finished = true;
  }

  function advance() {
    if (!state.buffer.length) return false;
    state.current = state.buffer.shift();
    state.played += 1;
    if (state.plan && state.plan.frames) {
      bar.style.width = Math.min(100, (100 * state.played) / state.plan.frames) + "%";
    }
    return true;
  }

  // ── Playback ──────────────────────────────────────────────────────────

  function setPlaying(play) {
    state.playing = play;
    playButton.textContent = play ? "Pause" : "Play";
    playButton.classList.toggle("is-playing", play);
    if (play) {
      state.lastStamp = 0;
      requestAnimationFrame(tick);
    }
  }

  function tick(stamp) {
    if (!state.playing) return;
    if (!state.lastStamp) state.lastStamp = stamp;

    var elapsed = (stamp - state.lastStamp) / 1000;
    state.lastStamp = stamp;
    if (elapsed > 1) elapsed = 1;         // a tab that was in the background

    state.playhead += elapsed * FPS * Number(speedSelect.value);

    var moved = false;
    while (state.playhead >= 1) {
      state.playhead -= 1;
      if (!advance()) {
        state.playhead = 0;
        break;
      }
      moved = true;
    }

    if (moved) draw();

    if (state.buffer.length < CHUNK * FPS) fill();

    if (!state.buffer.length) {
      if (state.windowIndex >= state.windows.length) {
        setPlaying(false);
        say("End of the plan.");
        return;
      }
      say("Waiting for frames\u2026");
    }

    requestAnimationFrame(tick);
  }

  // ── Drawing ───────────────────────────────────────────────────────────

  function svgX(x) { return Number(x) + HALF_WIDTH; }
  function svgY(y) { return HALF_HEIGHT - Number(y); }

  function playerNode(player) {
    var key = player.s + "|" + (player.d === null ? player.j : player.d);
    var node = nodes.get(key);
    if (node) return node;

    var group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    var circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("r", "3.4");
    circle.setAttribute("class", "sim-player sim-" + (player.s === "H" ? "home" : "away"));
    var label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("class", "sim-jersey");
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("dominant-baseline", "central");

    group.appendChild(circle);
    group.appendChild(label);
    playerLayer.appendChild(group);

    node = { group: group, circle: circle, label: label };
    nodes.set(key, node);
    return node;
  }

  function draw() {
    var frame = state.current;
    var seen = new Set();

    if (frame) {
      (frame.players || []).forEach(function (player) {
        var node = playerNode(player);
        var key = player.s + "|" + (player.d === null ? player.j : player.d);
        seen.add(key);
        node.group.removeAttribute("display");
        node.group.setAttribute(
          "transform",
          "translate(" + svgX(player.x).toFixed(2) + " " + svgY(player.y).toFixed(2) + ")"
        );
        node.label.textContent = player.j || "";
        node.circle.classList.toggle("has-puck", player.p === 1);
      });

      if (frame.puck) {
        if (!puckNode) {
          puckNode = document.createElementNS("http://www.w3.org/2000/svg", "circle");
          puckNode.setAttribute("r", "1.5");
          puckNode.setAttribute("class", "sim-puck");
          puckLayer.appendChild(puckNode);
        }
        puckNode.removeAttribute("display");
        puckNode.setAttribute(
          "transform",
          "translate(" + svgX(frame.puck[0]).toFixed(2) + " " + svgY(frame.puck[1]).toFixed(2) + ")"
        );
      } else if (puckNode) {
        puckNode.setAttribute("display", "none");
      }

      clock.textContent = clockText(frame);
    }

    nodes.forEach(function (node, key) {
      if (!seen.has(key)) node.group.setAttribute("display", "none");
    });
  }

  function clockText(frame) {
    var start = PERIOD_START[frame.period] || 0;
    var seconds = Math.max(0, frame.t - start);
    var minutes = Math.floor(seconds / 60);
    var rest = seconds % 60;
    var period = frame.period === "4" ? "OT" : "P" + frame.period;
    return period + " \u00b7 " +
      (minutes < 10 ? "0" : "") + minutes + ":" + (rest < 10 ? "0" : "") + rest;
  }

  // ── Menus ─────────────────────────────────────────────────────────────

  function teamsInGame(gameId) {
    // game_id is "<date> <away> @ <home>", so the two names come apart on the @.
    var parts = String(gameId || "").split("@");
    if (parts.length !== 2) return null;
    var away = parts[0].trim().split(" ").slice(1).join(" ");
    var home = parts[1].trim();
    return away && home ? [away, home] : null;
  }

  function option(value, label) {
    var element = document.createElement("option");
    element.value = value;
    element.textContent = label;
    return element;
  }

  /* Narrow the team and player menus to the game that is selected.  The full
     list is every player in the drop; picking a game should not leave 250 names
     that were never on the ice. */
  function rebuildMenus() {
    var teams = teamsInGame(gameSelect.value);
    var keptTeam = teamSelect.value;
    var keptPlayer = playerSelect.value;

    teamSelect.length = 0;
    teamSelect.appendChild(option("", "All"));
    (teams || []).forEach(function (team) {
      teamSelect.appendChild(option(team, team));
    });
    if (keptTeam && (!teams || teams.indexOf(keptTeam) >= 0)) {
      teamSelect.value = keptTeam;
    }

    playerSelect.length = 0;
    playerSelect.appendChild(option("", "All"));
    var shown = playerData.filter(function (player) {
      return !teams || teams.indexOf(player.team) >= 0;
    });
    shown.forEach(function (player) {
      playerSelect.appendChild(
        option(String(player.player_id), "#" + player.jersey + " \u00b7 " + player.team)
      );
    });
    if (keptPlayer && shown.some(function (p) { return String(p.player_id) === keptPlayer; })) {
      playerSelect.value = keptPlayer;
    }
  }

  // ── Controls ──────────────────────────────────────────────────────────

  playButton.addEventListener("click", function () {
    if (!state.windows.length) return;
    if (state.playing) {
      setPlaying(false);
      return;
    }
    if (state.finished && !state.buffer.length) {
      loadPlan();
      return;
    }
    setPlaying(true);
  });

  restartButton.addEventListener("click", loadPlan);

  speedSelect.addEventListener("change", function () {
    state.lastStamp = 0;
  });

  function syncWindowRow() {
    windowRow.hidden = eventSelect.value === "";
  }

  eventSelect.addEventListener("change", function () {
    syncWindowRow();
    loadPlan();
  });

  form.addEventListener("change", function (event) {
    if (event.target === eventSelect) return;      // the event select has its own
    if (event.target === gameSelect) rebuildMenus();
    loadPlan();
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    loadPlan();
  });

  syncWindowRow();
  rebuildMenus();
  loadPlan();
})();
