# Poker Trainer

A browser table where you play hands yourself - check, call, raise, fold - against
the bots from the [`pokerbot`](../pokerbot) project (Yosief, GTO, and the practice
archetypes like NIT, MANIAC, STATION). Pick who sits in each seat, play the hand to
the end, see the result.

This is a separate project from `pokerbot/`, but it shares its code rather than
copying it: `server.py` adds the pokerbot checkout to `sys.path` and imports its
modules (`cards`, `protocol`, `ranges`, `strategy`, `opponents`, `bot`, `gto`)
directly, so there's one source of truth for every bot's logic. A sibling
`../pokerbot` is used when it exists. Otherwise the app uses `vendor/pokerbot`,
a Git submodule of [YosiefAbraham/pokerbot](https://github.com/YosiefAbraham/pokerbot),
which is how the Vercel deploy finds the bots.

## Quick start

No dependencies beyond the Python standard library. Locally, keep a `pokerbot`
checkout next to this folder, or clone with the submodule:

```
git clone --recurse-submodules https://github.com/YosiefAbraham/poker-trainer.git
```

```
python3 server.py              # http://localhost:8100
python3 server.py --port 8080
```

Open the page, pick 2-8 seats and who plays each one (you, plus any mix of bots),
set blinds and starting stack, and start the table.

## How it works

- **`interactive.py`** is a from-scratch hand engine, adapted from
  `pokerbot/engine.py`'s `_Hand`. The only real difference: `betting_round()` is a
  generator. For every bot seat it calls `bot.act(state)` exactly like the original
  dealer; at your seat it `yield`s the `GameState` instead and waits to be sent an
  action. That's what lets a browser click pause a hand mid-street without turning
  the rest of the table into bots-only replay. Nothing in `pokerbot/` is modified.
- **`server.py`** is a small stdlib HTTP server (`http.server`, like
  `pokerbot/dashboard.py`). Each table is a session kept in memory:
  - `POST /api/table` - create a table (seats, blinds, stack) and deal hand 1.
  - `POST /api/act` - send your action (`fold` / `check` / `call` / `raise` with `to`).
  - `POST /api/next_hand` - deal the next hand; stacks carry over with a rebuy on busting.
  - `GET /api/state` - re-fetch the current hand, for recovering after a page reload.
  - `GET /api/bots` - the roster of bots a seat can be given.
- **`static/index.html`** is a single-file frontend (no build step, no external
  libraries): a setup screen, an oval table with seats positioned around it, your
  hole cards and action controls, an action log, and an end-of-hand summary (net
  chips, any hands shown at showdown, your hand's postflop class each street via
  `strategy.classify`).

Analysis is end-of-hand only by design - no live per-decision grading. Nothing
about a table is stored on the server: the session id is a token with the seat
list, the deal seed, and the actions so far, and each request replays the hand.
That's what lets the same table keep going on Vercel, where one request may land
on a fresh process. There's no auth. The hosted app is the Flask app in
`server.py` (`app`), which is what Vercel runs. Locally, `python server.py`
still uses the standard-library server and does not need Flask installed.
