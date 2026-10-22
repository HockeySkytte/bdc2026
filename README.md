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
| Overview | what is in the file, and how big it is |
| Possession | who is holding the puck, by team, by player and by strength state |
| Pressure | how far every player is from the puck, and who is defending |
| Space | where the players are, as a density map over the rink |
| Situations | possession split by the state of the game |
| Data & Method | what happens to the raw files, and what to be careful about |

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

The pages read `data/sample_summary.json`.  It is written by `build_sample.py`,
which reads one complete game out of the raw tracking files in
`../data/bigdata_cup_2026` and computes the same aggregates the database queries
will return later.

```bash
pip install -r requirements-dev.txt
python build_sample.py
```

That script needs pandas, which the site itself does not, so it is kept out of
`requirements.txt` and a deploy never installs it.  The JSON is committed, so a
clone runs without the raw files and without pandas.

The shape of that file is the contract between the generator and the app:

```
meta        what the sample is: game, teams, frames, players, possession rate
periods     one row per period
possession  by team, top players, by strength state
pressure    distance buckets, defensive roles, by team
space       a density grid keyed "column,row", plus thirds of the rink
```

`data_source.py` is the only module that reads anything.  When the tables land in
Supabase, that file swaps its `load()` for a query and the templates do not
change.

## Deploying on Railway

The folder is its own project: `Procfile`, `railway.toml`, `requirements.txt` and
`.python-version` are all here, so pointing Railway at this directory is enough.

* `railway.toml` sets the start command and the healthcheck.
* `/healthz` answers 200 only when the aggregates can be read, and 503 with the
  reason when they cannot, so a broken deploy fails the healthcheck instead of
  serving an error page to visitors.
* `SECRET_KEY` should be set in the Railway variables.  Without it a random key
  is generated at boot, which is fine until a restart silently invalidates every
  unlock cookie.

## Files

| file | what it does |
| --- | --- |
| `app.py` | routes, and one gate in `before_request` rather than a check in every page |
| `config.py` | branding, access and paths; the same shape as the event app |
| `auth.py` | the shared-password gate: HMAC-signed cookie, password inside the label |
| `data_source.py` | the only module that reads data |
| `rink.py` | rink geometry, and the cells the position map is drawn from |
| `build_sample.py` | development utility: raw tracking files to `sample_summary.json` |
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
