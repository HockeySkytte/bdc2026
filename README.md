# Big Data Cup 2026 tracking

A Flask site for the tracking data.  Six pages, all of them built from one
aggregate summary, and a data layer thin enough that moving the numbers into
Supabase changes one file.

The design is the same as the event app in `challenge/`: a 260px sidebar with the
club gradient, a sticky tab strip, panels and tables on the same dark palette.
`static/styles.css` is copied from there unchanged, and everything this site
needed beyond it is in `static/tracking.css`.

## Pages

| page | question it answers |
| --- | --- |
| Theory | what defense means here, and the metrics this project defines |
| Simulation | what the play looked like, frame by frame, with the slicers applied |
| Puck vs. Player | where the puck goes, and where the players are, in the same frames |
| Skaters | how one player compares with the rest of his position |
| Data and Method | what happens to the raw files, and what to be careful about |

## Running it

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
python app.py                   # http://127.0.0.1:5003
```

The site is public unless `APP_PASSWORD` is set, in which case every page sits
behind the same shared-password gate the event app uses.  That is the one rule
that is deliberately inverted: a public analysis site should come up working on a
fresh deploy, where the event app failed closed because it was a private
submission.

```bash
APP_PASSWORD=secret python app.py
```

## Where the numbers come from

The four analysis pages read the Supabase tables directly. `data/sample_summary.json`
is still there for `build_sample.py` and is what `/download/summary` hands out, but
no page is drawn from it any more - the shell pages that were are gone.


## The repository

This folder is its own git repository. To publish it:

```bash
cd bdc2026
git remote add origin https://github.com/HockeySkytte/bdc2026.git
git push -u origin main
```

Nothing secret is committed: `.env` is ignored and `.env.example` holds the names
without the values. `data/sample_summary.json` is committed on purpose - `/healthz`
reads it and the Data page quotes two numbers from it, so a clone runs without the
raw tracking files, which are far too big for a repository.

The four scripts that are not imported by the app are the ones that built the
data: `load_supabase.py`, `build_frames.py`, `add_boxids.py` and `zones.py`.
`build_sample.py` regenerates the sample summary from the raw files. They are all
in the repo because the Data page shows two of them, and because a reviewer should
be able to see how the tables were made.

## Deploying on Railway

The folder is its own project: `Procfile`, `railway.toml`, `requirements.txt` and
`.python-version` are all here, so pointing Railway at this directory is enough.
Railpack reads `requirements.txt`, the `Procfile` starts gunicorn, and the
healthcheck is `/healthz`.

Set two variables in the Railway project:

| name | why |
| --- | --- |
| `DATABASE_URL` | required. The Supabase connection string. Use the **session pooler on 5432**, not the transaction pooler on 6543 - a multi-statement read will not survive transaction mode. The app rewrites the port if you paste the wrong one, but the string should be right. |
| `SECRET_KEY` | recommended. Without it a random key is generated at boot, which works until a restart silently invalidates every unlock cookie. |
| `APP_PASSWORD` | optional. Unset leaves the site public; set it and every page sits behind the gate. |

`/healthz` reports the sample summary **and** the database, and answers 503 with
the reason if either cannot be reached. A deploy with a wrong `DATABASE_URL`
fails its healthcheck rather than coming up and serving errors, which is what you
want from something with a public URL.

Three things in the start command are deliberate:

* `--workers 3`. The pages read Supabase, and a cold query can hold a worker for
  seconds. Two workers means a slow page can block the healthcheck.
* `--timeout 180`. A zone-map query on a cold cache has taken over two minutes on
  this instance. At the gunicorn default of 30 the worker would be killed
  mid-request and the visitor would see a 502.
* gzip is on, in `app.py` rather than in a proxy. The coloured cleaning script is
  367 kB of markup and about 20 kB compressed; the API responses come down by
  roughly the same factor.


## The simulation

`/` is the simulation.  The slicer panel picks a game, then narrows it by
strength state, score state, team, player, jersey, position, side and event.
Event is `Full game` by default; pick a type and two more fields appear for how
many seconds either side of it to play.

The browser never holds a game - that is 105,000 frames - so the page asks two
questions and walks the answers:

* `/api/sim/plan` returns the seconds that match, as runs.  A player filter turns
  into that player's shifts, an event filter into each event's window, and
  overlapping windows are merged so nothing replays.
* `/api/sim/frames?t0=&t1=` returns the coordinates for one ten second window.

Playback counts frames rather than wall-clock time, so a slow response pauses the
clock instead of skipping the play.  Speed is a multiplier on the frame rate.

Two decisions worth knowing:

**State filters have their own indexes.**  A filter on strength or score state
cannot use `(game_id, time_elapsed)` - those columns come after the game id - so
it used to read the heap.  `(game_id, strength_state, time_elapsed)` and
`(game_id, score_state, time_elapsed)` make the plan query an index-only scan.
That alone was not enough: index-only scans still did 170,000 heap fetches
because the table had been updated since the load, so the visibility map was
stale.  `VACUUM (ANALYZE)` fixed it - 31 seconds once, and heap fetches went to
zero.  Timings for one game, cold:

| plan | before | after |
| --- | --- | --- |
| full game | 1.06s | 0.25s |
| strength 5v5 | 7.99s | 0.16s |
| score 0 | 1.29s | 0.14s |
| one player | 4.67s | 0.20s |
| event, 3s either side | 0.20s | 0.10s |

Plans are also cached per filter combination, so going back to a set of slicers
is instant.

**The plan is per second, not per frame.**  A frame is an (image id, frame
number, second) triple and the client only ever asks for a second range, so
seconds are the finest thing the plan needs - and `distinct time_elapsed` can be
answered from the index without touching the heap.  Grouping by frame instead
reads every row of the game: 843,000 of them, which took 26 seconds against a
cold cache.

## Puck vs. Player

`/puck-player` asks one question twice: where is the puck, and where are the
players, in the frames the slicers keep.  Team is required; game, jersey,
position, side, strength state, score state, team in possession, player in
possession and zone narrow it further.

A jersey, position or side slicer does two things: it keeps only the frames that
player was on the ice for, and it keeps only those players' positions on the map.
Picking a position and still being shown the whole team reads as a filter that
does not work.  A zone selection is the opposite case - the frames follow the
puck, and every player in them is drawn.

Selecting zones is one click per third - Defensive, Neutral, Offensive - or click
single boxes to build your own selection.

Strength state defaults to 5v5, because that is the game most of the time.

**Frame filters and row filters are different things.**  The puck's zone, the
strength and score state and who has possession are facts about a frame.  Picking
a zone therefore means "keep the frames where the puck was in this zone", and then
show every player in those frames - not "keep the player rows whose own zone is
this zone", which would answer a different question and put dots only inside the
selection.  The first version had exactly that bug; with `O05` selected it
returned player positions in one zone instead of 28.

The same rule covers the three possession slicers.  Team in possession reads the
frame's `possession_team` (our team, the opponent, or nobody within reach of the
puck), which partitions the frames exactly - 18,030 + 18,757 + 35,877 = 72,664 for
one game.  Player in possession narrows to frames where the selected jersey is
carrying.

**The slicers cascade downwards.**  The team decides which games are offered, and
the game decides which jerseys - a number that never skated in the chosen game is
not in the menu.  Nothing cascades upwards: choosing a game never narrows the team
list, because the team is the question and the game is the situation.  The
per-game player lists come from `bdc2026_game_players`, 410 rows built alongside
the frames, because the events table misses players who never touched the puck -
three in one game, one of them with 23,719 tracked frames.

**One row per frame.**  `build_frames.py` writes `bdc2026_frames`: 959,007 rows
carrying what is true of the frame - the puck's zone and coordinates, the states,
who had possession - instead of repeating it on all eight player rows.  Every
frame-level slicer is answered from that table, which is small enough to scan for
a whole game in a moment.  The player maps then read the tracking table for those
frames only.

**The view is attack-normalised.**  Stored coordinates have the home team
defending `+x`, so a row is mirrored when the selected team was the home team in
that game - `x` and `y` both negated, `boxid` swapped `O` to `D` - which puts the
selected team's offensive zone on the right.

What it took to get from 100 seconds to about two:

| change | before | after |
| --- | --- | --- |
| frame facts in their own table | - | one scan of 959,000 rows instead of 7,432,491 |
| covering indexes on tracking | 30s a query | 130 ms |
| `work_mem` raised from 4 MB | 110 MB of temp spill per query | none |
| one statement for all three maps | 5 round trips, 95 ms each | 1 |
| responses cached per filter set | - | repeat views are instant |

`VACUUM` matters as much as the indexes: index-only scans reported heap fetches
until it ran, because the table had been updated since the load.

### What is left, and why more indexes will not fix it

Two more things are in the app now: every result is cached per filter combination,
and the default view of all twenty (game, team) pairs is computed in a background
thread on the first request anyone makes.  Repeats are a dictionary lookup.

The first computation of a *new* combination is still slow, and the measurements
say it is disk, not planning.  On this instance, after the IO budget is spent:

| what | cost |
| --- | --- |
| one game, one team, cold | 10 to 20 seconds |
| the same query once Supabase has the pages cached | 1 to 4 seconds |
| a warmed or repeated request | under 10 ms |

The indexes are not the missing piece.  `(game_id, team, boxid, player_jersey,
x_feet, y_feet)` already holds every column the player maps read, and the planner
uses it.  The cost is the shape of the read: 296,000 player rows per game and team
is 194 MB of heap through a bitmap heap scan, and an index-only scan does not win
because the rows are not stored in game order (correlation -0.20, so the index
entries point all over the table).

The two things that would actually make it fast, in order of effort:

1. **Precompute the aggregates.** A build step, like `build_frames.py`, writing
   counts per (game, team, jersey, zone, and each filter dimension).  The page then
   reads a table of thousands of rows instead of millions.  That is a real design
   job because the filter dimensions multiply, and it is not a one-line index.
2. **A bigger instance.** The baseline disk throughput is the ceiling; the same
   queries ran in 130 ms warm.

`All games` is deliberately not offered.  Without a game id the covering index
cannot narrow the scan and the player maps read all 7.4 million rows - 40 to 60
seconds.  It is a one-line change in the template if it is ever wanted.

## Data & Method

`/data` has three tabs. **Data & method** walks the cleaning in nine steps and
then says what to be careful about - overtime event coordinates are
attack-normalised rather than absolute, and player positions and sides are
estimated from neutral zone faceoffs with ten games of evidence behind them.
**Cleaning** prints `data_cleaning.sql` in full, annotated. **Supabase** explains
the copy and prints `load_supabase.py`.

Both documents are read from the files themselves, cached on modification time,
so the page cannot drift from what ran.

## Skaters

`/skaters` is one player's card, built the way the main Moncton app builds its
own: metric name, value, and a bar whose length is the player's **percentile**
within a comparison group, coloured on the same red-white-blue gradient. The
pool is the player's own position, and only players with at least 100 seconds of
tracked time are in it.

Metrics where less is better - the shot counts and the share of defensive time
spent as the primary defender - have the percentile flipped before the bar is
drawn, so a long blue bar always means good.

The **Totals or rates** slicer divides by the player's own tracked time. Both
counts and zone durations scale, because a player who skates twice as much
accumulates twice as much of both, and that is ice time talking rather than
defending: on the default player, defensive zone time reads the 99th percentile
in totals and the 50th per 60. Tracked time itself, the distances in feet and the
percentages are left alone.

## Loading the tables into Supabase

`load_supabase.py` reloads the three tables and then indexes them. It does not
copy anything itself: the copy is `scripts/reload_supabase_table_from_mysql.py`
from the parent repo, the same loader every other table in this project uses, so
there is one implementation of that logic rather than two.

```bash
python load_supabase.py --dry-run        # what it would do
python load_supabase.py                  # reload all three, then index
python load_supabase.py --indexes-only   # data already there, just index
```

It reads `MYSQL_*` and `DATABASE_URL` from the repo's `.env`, and switches a
`pooler.supabase.com:6543` URL to the session pooler on 5432, because a COPY and
an index build both outlive what transaction pooling will hold.

Four things that cost time, in the order they bite:

**Storage.** `bdc2026_tracking_cleaned` is 7.4 million rows and 2.2 GB with its
indexes. Over its storage limit Supabase writes `default_transaction_read_only =
on` into `postgresql.auto.conf`, and every write then fails with
`ReadOnlySqlTransaction`. Free space before loading, not during.

**Indexes.** They are what consume the disk IO budget, not the copy: on this
project the load took about nine minutes and the two large indexes took eleven
and nineteen minutes, because the budget was already spent. Build them once,
after the load, and keep the list short - each one on the tracking table is
roughly 200 MB written.

**Statement timeouts.** The default is two minutes. The loader sets it to zero
for its session, but a full-table COPY through the pooler still needs the
session endpoint.

**Reserved words.** `position` is renamed to `player_position` after loading,
because `POSITION` is a reserved word in PostgreSQL and would need quoting in
every query for ever.

## Rink boxes

`add_boxids.py` adds `boxid` (from the player's coordinates) and `boxid_puck`
(from the puck's) to `bdc2026_tracking_cleaned`, using the 58 polygons in
`HockeyRinkZones.geojson` - the same file the challenge project used.

```bash
python add_boxids.py --dry-run
python add_boxids.py
```

It runs in Supabase, not MySQL: PostGIS is enabled, the 58 zones go into
`bdc2026_rink_zones` with a GiST index, and each game is updated in one pass that
sets both columns. One UPDATE rather than two, so the row is rewritten once.

The rules match the old loader, and PostGIS was checked against it: 3,009 random
points plus every edge case I could think of, zero differences from the shapely
implementation.

* a point on a shared edge belongs to several boxes and the **lowest id wins**;
* a point past the rounded corners gets NULL, which is about 0.2% of player
  points and 4.4% of puck points - the tracker puts the puck on or beyond the
  board line, and the polygons deliberately stop at the boards;
* every one of the 58 zones is used, and no row carries an id that is not in the
  zone table.

**The labels are home-relative in this table.** Coordinates were rotated so the
home team defends `+x`, and the zone file calls the `+x` end `O`. So here `O` is
the home team's *defensive* end. In the challenge project the coordinates were
attack-normalised, so `O` meant the attacking end. Flip `O` and `D` if you need
an attack-relative label.

## Files

| file | what it does |
| --- | --- |
| `app.py` | routes, and one gate in `before_request` rather than a check in every page |
| `config.py` | branding, access and paths; the same shape as the event app |
| `auth.py` | the shared-password gate: HMAC-signed cookie, password inside the label |
| `db.py` | the only place that knows a connection string; a small pool in front of psycopg |
| `simulation.py` | the plan and the frame queries the simulation page asks for |
| `zone_map.py` | the zone counts and average positions behind Puck vs. Player |
| `zones.py` | the 58 rink polygons as SVG paths; writes `static/rink-zones.json` |
| `build_frames.py` | builds `bdc2026_frames`, one row per tracked frame |
| `data_source.py` | the only module that reads the summary behind the aggregate pages |
| `rink.py` | rink geometry, and the cells the position map is drawn from |
| `build_sample.py` | development utility: raw tracking files to `sample_summary.json` |
| `load_supabase.py` | reloads the three tables through the shared MySQL loader, then indexes and analyzes them |
| `add_boxids.py` | adds `boxid` and `boxid_puck` in Supabase from the 58-zone rink geometry |
| `templates/` | the shell, the gate, and one file per page |
| `static/styles.css` | the shared design system, copied unchanged |
| `static/tracking.css` | the components this site added |

## Things worth knowing about the data

These are in the Data & Method page too, because they decide whether a number on
the other pages can be trusted.

* **`player_id` is a track, not a person.**  The tracker re-issues ids when it
  loses and re-acquires a player: 1,962 of them for 42 players in one game, 132
  of them on the goalie alone.  Anything per player groups on `(team, jersey)`.
* **Goalies are marked `Go`** in the jersey column.  It is the only goalie marker
  in the file, so it is carried into `is_goalie` rather than cast away.
* **Blank and `n/a` mean different things.**  Puck rows carry `n/a` in the team
  and id columns and blanks in the coordinates.  Both have to become NULL before
  the columns can be typed.
* **Not every player is in every frame.**  Around eight players per frame, not
  twelve, so a cell count means "seen here" and not "was here".
* **Some rows have no clock.**  9,075 of 8.4 million across the drop cannot be
  placed in time.  They are dropped, and the count is kept.
* **Overtime is treated like period 2** when the coordinates are mirrored.  Worth
  confirming by eye per game, because it decides whether overtime is mirrored at
  all.
