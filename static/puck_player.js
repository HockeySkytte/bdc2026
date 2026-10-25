/* The Puck vs. Player page.
 *
 * Two rinks drawn from one dataset.  The top colours the 58 rink zones by how
 * many frames had the puck in them; clicking zones narrows both maps to those
 * frames.  The bottom shows the selected team either as a heat map of the same
 * zones or as one average position per player.
 *
 * The zone outlines come from static/rink-zones.json, written by zones.py, so
 * they are fetched once and cached rather than repeated in the page.
 */
(function () {
  "use strict";

  var form = document.getElementById("zoneSlicers");
  if (!form) return;

  var teamSelect = document.getElementById("zTeam");
  var gameSelect = document.getElementById("zGame");
  var jerseySelect = document.getElementById("zJersey");
  var possessionSelect = document.getElementById("zPossession");
  var sub = document.getElementById("zoneSub");
  var puckZones = document.getElementById("puckZones");
  var playerZones = document.getElementById("playerZones");
  var playerDots = document.getElementById("playerDots");
  var puckPeak = document.getElementById("puckPeak");
  var playerNote = document.getElementById("playerNote");
  var playerTitle = document.getElementById("playerTitle");
  var zoneChips = document.getElementById("zoneChips");
  var clearButton = document.getElementById("clearZones");
  var playerDataNode = document.getElementById("zonePlayerData");
  var players = playerDataNode ? JSON.parse(playerDataNode.textContent) : [];
  var gamesNode = document.getElementById("zoneGamesByTeam");
  var gamesByTeam = gamesNode ? JSON.parse(gamesNode.textContent) : {};
  var gamePlayersNode = document.getElementById("zonePlayersByGame");
  var playersByGame = gamePlayersNode ? JSON.parse(gamePlayersNode.textContent) : {};

  var SVG = "http://www.w3.org/2000/svg";
  var state = { zones: null, selected: new Set(), mode: "positions", data: null, request: 0 };

  // ── Helpers ───────────────────────────────────────────────────────────

  function element(name, attributes) {
    var node = document.createElementNS(SVG, name);
    Object.keys(attributes || {}).forEach(function (key) {
      node.setAttribute(key, attributes[key]);
    });
    return node;
  }

  function say(message) { sub.textContent = message; }

  function filters() {
    var query = new URLSearchParams();
    new FormData(form).forEach(function (value, key) {
      if (value !== "") query.set(key, value);
    });
    if (state.selected.size) query.set("zones", Array.from(state.selected).join(","));
    return query;
  }

  // ── Zone shapes ───────────────────────────────────────────────────────

  function drawZones(layer) {
    layer.textContent = "";
    state.zones.forEach(function (zone) {
      var path = element("path", {
        d: zone.d,
        "data-zone": zone.id,
        class: "rink-zone"
      });
      layer.appendChild(path);
    });
  }

  function zoneNodes(layer) {
    var out = {};
    Array.prototype.forEach.call(layer.childNodes, function (node) {
      if (node.getAttribute && node.getAttribute("data-zone")) {
        out[node.getAttribute("data-zone")] = node;
      }
    });
    return out;
  }

  function paint(layer, scaled, { clickable, selected }) {
    var nodes = zoneNodes(layer);
    Object.keys(nodes).forEach(function (id) {
      var node = nodes[id];
      var entry = scaled && scaled.zones ? scaled.zones[id] : null;
      node.setAttribute("fill", entry ? entry.fill : "rgba(255,255,255,0.02)");
      node.classList.toggle("is-selected", Boolean(selected && selected.has(id)));
      node.classList.toggle("is-clickable", Boolean(clickable));
      var title = node.firstChild;
      if (!title || title.nodeName !== "title") {
        title = document.createElementNS(SVG, "title");
        node.appendChild(title);
      }
      title.textContent = id + (entry ? " \u00b7 " + entry.count.toLocaleString() + " frames" : "");
    });
  }

  // ── Player dots ───────────────────────────────────────────────────────

  function drawPlayers(positions, selected) {
    playerDots.textContent = "";
    positions.forEach(function (player) {
      var group = element("g", { transform: "translate(" + player.x + " " + player.y + ")" });
      group.appendChild(element("circle", { r: "3.9", class: "player-mark" }));
      var label = element("text", {
        "text-anchor": "middle",
        "dominant-baseline": "central",
        class: "player-jersey"
      });
      label.textContent = player.jersey;
      group.appendChild(label);
      var title = document.createElementNS(SVG, "title");
      title.textContent = "#" + player.jersey + " \u00b7 " +
        player.frames.toLocaleString() + " frames";
      group.appendChild(title);
      playerDots.appendChild(group);
    });
  }

  // ── Menus ─────────────────────────────────────────────────────────────

  /* The slicers cascade from the top down: the team decides which games are
     offered, and the game decides which jerseys.  Nothing cascades upwards -
     choosing a game never narrows the team list, because the team is the
     question and the game is the situation. */
  function rebuildGames() {
    var games = gamesByTeam[teamSelect.value] || [];
    var kept = gameSelect.value;
    gameSelect.length = 0;
    games.forEach(function (game) {
      var option = document.createElement("option");
      option.value = game;
      option.textContent = game;
      gameSelect.appendChild(option);
    });
    if (games.indexOf(kept) >= 0) gameSelect.value = kept;
  }

  /* The players who were actually in the chosen game, so the jersey menu does
     not offer a number that never skated. */
  function playersInScope() {
    var team = teamSelect.value;
    var known = playersByGame[gameSelect.value];
    return players.filter(function (player) {
      if (player.team !== team) return false;
      if (!known) return true;
      return known.some(function (pair) {
        return pair[0] === team && pair[1] === player.jersey;
      });
    });
  }

  function rebuildJerseys() {
    var team = teamSelect.value;
    var kept = jerseySelect.value;
    var list = playersInScope().filter(function (player) {
      return player.jersey !== "Go";
    });
    jerseySelect.length = 0;
    var all = document.createElement("option");
    all.value = "";
    all.textContent = "All players";
    jerseySelect.appendChild(all);
    list.forEach(function (player) {
      var option = document.createElement("option");
      option.value = player.jersey;
      option.textContent = "#" + player.jersey +
        (player.position ? " \u00b7 " + player.position : "");
      jerseySelect.appendChild(option);
    });
    if (kept && list.some(function (p) { return p.jersey === kept; })) {
      jerseySelect.value = kept;
    }

    // "Team has the puck" should name the team that is actually selected.
    var teamOption = possessionSelect.querySelector('option[value="team"]');
    if (teamOption) teamOption.textContent = team + " has the puck";
  }

  // ── Load ──────────────────────────────────────────────────────────────

  async function load() {
    var request = ++state.request;
    say("Reading the maps\u2026");

    var query = filters();
    var response, data;
    try {
      response = await fetch("/api/zone-map?" + query.toString());
      data = await response.json();
    } catch (error) {
      say("Could not reach the server.");
      return;
    }
    if (request !== state.request) return;
    if (data.error) { say(data.error); return; }

    state.data = data;
    paint(puckZones, data.puck, { clickable: true, selected: state.selected });
    paint(playerZones, data.player, { clickable: false, selected: null });

    var showHeat = state.mode === "heat";
    playerZones.setAttribute("display", showHeat ? "" : "none");
    playerDots.setAttribute("display", showHeat ? "none" : "");
    if (!showHeat) drawPlayers(data.positions, state.selected);

    var where = data.games === 1
      ? (data.home_team === data.team ? "at home to " : "away to ") +
        (data.home_team === data.team ? data.away_team : data.home_team)
      : data.games + " games (" + data.home_games + " at home)";

    var scope = data.selected_frames || data.puck.frames;
    say(data.team + " \u00b7 " + where + " \u00b7 " +
        scope.toLocaleString() + " puck frames" +
        (data.jerseys && data.jerseys.length === 1 ? " \u00b7 #" + data.jerseys[0] : ""));

    puckPeak.textContent = data.puck.peak
      ? data.puck.peak.toLocaleString() + " frames in the busiest zone"
      : "";

    playerTitle.textContent = (data.jerseys && data.jerseys.length === 1)
      ? "Player #" + data.jerseys[0] + " positions"
      : "Player positions";

    playerNote.textContent = data.positions.length === 0
      ? "No player positions match these slicers."
      : showHeat
        ? "Player frames per zone, on the same 58 zones as the puck map above."
        : "One dot per player, at their average position over the frames shown. " +
          "Dots are sized equally: read the count in the tooltip.";

    renderChips();
    updateParts();
  }

  function renderChips() {
    zoneChips.textContent = "";
    var zones = Array.from(state.selected).sort();
    clearButton.hidden = zones.length === 0;
    if (!zones.length) {
      zoneChips.textContent = "No zone filter: every frame is included.";
      return;
    }
    // A whole third selected says its name; a hand-built selection lists a few.
    var thirds = { D: "Defensive third", N: "Neutral third", O: "Offensive third" };
    var whole = wholeThird(zones);
    if (whole) {
      var named = document.createElement("span");
      named.className = "map-chip";
      named.textContent = thirds[whole];
      zoneChips.appendChild(named);
      return;
    }

    zones.slice(0, 6).forEach(function (zone) {
      var chip = document.createElement("span");
      chip.className = "map-chip";
      chip.textContent = zone;
      zoneChips.appendChild(chip);
    });
    if (zones.length > 6) {
      var more = document.createElement("span");
      more.className = "map-chip";
      more.textContent = "+" + (zones.length - 6) + " more";
      zoneChips.appendChild(more);
    }
  }

  /* Which third of the rink a zone belongs to.  The geometry names them O, N and
     D, and those names are already attack-normalised, so the letters are the
     answer with no mapping. */
  function partZones(part) {
    return state.zones
      .filter(function (zone) { return zone.id.charAt(0) === part; })
      .map(function (zone) { return zone.id; });
  }

  function wholeThird(zones) {
    var parts = ["D", "N", "O"];
    for (var i = 0; i < parts.length; i += 1) {
      var ids = partZones(parts[i]);
      var all = ids.length > 0 && zones.length === ids.length &&
        ids.every(function (id) { return state.selected.has(id); });
      if (all) return parts[i];
    }
    return null;
  }

  /* The three buttons light up when their whole third is in the selection. */
  function updateParts() {
    document.querySelectorAll(".map-part").forEach(function (button) {
      var ids = partZones(button.dataset.part);
      var all = ids.length > 0 &&
        ids.every(function (id) { return state.selected.has(id); });
      button.classList.toggle("is-active", all);
    });
  }

  // ── Events ────────────────────────────────────────────────────────────

  puckZones.addEventListener("click", function (event) {
    var target = event.target.closest("[data-zone]");
    if (!target) return;
    var zone = target.getAttribute("data-zone");
    if (state.selected.has(zone)) state.selected.delete(zone);
    else state.selected.add(zone);
    load();
  });

  clearButton.addEventListener("click", function () {
    state.selected.clear();
    updateParts();
    load();
  });

  document.querySelectorAll(".map-part").forEach(function (button) {
    button.addEventListener("click", function () {
      var ids = partZones(button.dataset.part);
      if (!ids.length) return;
      var all = ids.every(function (id) { return state.selected.has(id); });
      ids.forEach(function (id) {
        if (all) state.selected.delete(id);
        else state.selected.add(id);
      });
      updateParts();
      load();
    });
  });

  document.querySelectorAll(".map-mode").forEach(function (button) {
    button.addEventListener("click", function () {
      state.mode = button.dataset.mode;
      document.querySelectorAll(".map-mode").forEach(function (other) {
        other.classList.toggle("is-active", other === button);
      });
      if (state.data) {
        var showHeat = state.mode === "heat";
        playerZones.setAttribute("display", showHeat ? "" : "none");
        playerDots.setAttribute("display", showHeat ? "none" : "");
        if (!showHeat) drawPlayers(state.data.positions, state.selected);
        playerNote.textContent = showHeat
          ? "Player frames per zone, on the same 58 zones as the puck map above."
          : "One dot per player, at their average position over the frames shown.";
      }
    });
  });

  /* Rebuilding a menu changes which values are on offer, and then the maps have
     to follow: without the load() a new game only redrew once a zone was
     clicked. */
  teamSelect.addEventListener("change", function () {
    rebuildGames();
    rebuildJerseys();
    load();
  });
  gameSelect.addEventListener("change", function () {
    rebuildJerseys();
    load();
  });
  form.addEventListener("change", function (event) {
    if (event.target === teamSelect || event.target === gameSelect) return;
    load();
  });
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    load();
  });

  // ── Start ─────────────────────────────────────────────────────────────

  fetch("/static/rink-zones.json")
    .then(function (response) { return response.json(); })
    .then(function (document_) {
      state.zones = document_.zones;
      drawZones(puckZones);
      drawZones(playerZones);
      rebuildGames();
      rebuildJerseys();
      load();
    })
    .catch(function () {
      say("Could not load the rink zones.");
    });
})();
