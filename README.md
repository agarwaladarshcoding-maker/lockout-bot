<p align="center"><img src="assets/logo.svg" width="128" alt="Lockout logo"></p>

# Lockout Bot

A Discord bot that runs 1v1 **Codeforces Lockout** matches. Two players get the same five problems; the first accepted submission on Codeforces claims a problem and locks the opponent out of it. Most points when time runs out wins.

## What it does

- **Matches:** the challenger picks a base rating `R` (800 to 3100). Problems are `R` to `R+400`, worth 100 to 500 points. Default length 15 minutes.
- **Fair problems:** only problems neither player has ever submitted to, matched by name too so Div. 1 / Div. 2 twins are excluded. Interactive problems are skipped.
- **Handle verification:** a player proves they own a Codeforces handle by submitting a compile error to a set problem within 5 minutes.
- **Judging:** polls the rate-limited Codeforces API, with a grace period plus up to 3 minutes for submissions still in the judging queue at the bell.
- **Ties:** a fresh tie-breaker problem at `R+200`, first AC wins.
- **Rooms:** competition rooms (results saved, winner advances) and practice rooms (never recorded).
- **Admin commands:** create/remove rooms, end matches, force-register, list players and matches, health check.

Full game rules: [LOCKOUT.md](LOCKOUT.md).

## Run

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # add your bot token
.venv/bin/python main.py
```

## Tests

```bash
.venv/bin/python tests/run_tests.py
```

Runs against a fake Discord and fake Codeforces, no network needed.

## Layout

| File | What |
|---|---|
| `main.py` | slash commands, match loop, board rendering |
| `cf.py` | async Codeforces API client with rate limiting and problem cache |
| `db.py` | SQLite storage for players, rooms and results |
