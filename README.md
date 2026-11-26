# Moorlicht

*Deutsche Fassung: [README.de.md](README.de.md)*

AI agents play a hidden-role deduction game against each other – and you watch: every
statement, every whispered conversation, every nomination and vote, along with everything
the storyteller knows and the agents' private notes.

Moorlicht is a game of its own in the social deduction genre, run by a storyteller:
eight players seated in a circle, the village team against the moor team, night abilities,
false information through "fog", dead players with a ghost vote, nominations and executions.
It is not a copy of an existing game: the roles are original, while the framework of
night, day, nomination and vote is common in the genre.

## Quick start

Requirements: Python 3.10+ and [Claude Code](https://claude.com/claude-code) (`claude`),
signed in. No other packages are needed.

```powershell
# Read the rules and the role list
python -m moorlicht rules

# Free trial run with random players (shows the game flow and the interface)
python -m moorlicht run --backend mock --view

# Real game: 8 agents on Claude Fable 5.2, followed live in the browser
python -m moorlicht run --view

# Browse saved games
python -m moorlicht view
```

`--view` opens the viewer at `http://127.0.0.1:8765/`. Logs are saved to
`games/<timestamp>-seed<N>/events.jsonl`.

A real example game is included (shortened to one day, 8 agents on Fable 5.2):

```powershell
python -m moorlicht view --dir beispiele
```

## Cost and duration

Measured with Claude Fable 5.2 (effort `medium`) through the local `claude`: the example
run, shortened to one day, took 52 LLM calls, a little over 5 minutes and USD 2.99 at list
prices. A full game with the default settings takes about 250 to 350 calls; extrapolated,
that is roughly USD 15 to 25 and 30 to 45 minutes. With a Claude subscription, the usage
counts against your plan's usage limits.

You can cap this with `--max-cost` and `--max-calls`, and lower it with fewer discussion
rounds, `--vote-mode parallel` or `--effort low`.

## What you see

- **Seating circle** with roles, dead players, ghost votes, fog, the chopping block,
  ongoing whisper pairs and votes.
- **Timeline** with every event: night actions and storyteller information (including
  "true" or "false due to fog"), whispered conversations, public speeches, nominations,
  votes, executions.
- **Perspective** can be switched: storyteller (everything), public (only the table) or a
  single player (exactly what that player knows).
- **Notes**: with every decision, each agent writes a short private note (suspicion, plan,
  bluff) – can be shown or hidden.
- **Playback**: step forward/back (← →), play (space bar), speed, live mode.

## Key options

| Option | Meaning |
| --- | --- |
| `--seed N` | Make the role and seat assignment reproducible |
| `--roles a,b,...` | Set the roles (IDs from `python -m moorlicht rules`) |
| `--model`, `--effort` | Model and reasoning effort of the agents (default `claude-fable-5-2`, `medium`) |
| `--language Englisch` | Language the agents speak (rule texts stay in German) |
| `--whisper-rounds 2,1` | Whisper rounds per day (day 1, then later days) |
| `--discussion-rounds 2` | Public discussion rounds per day |
| `--vote-mode parallel` | Everyone votes at once (faster, less of a "round the table" feel) |
| `--storyteller heuristic` | Storyteller discretion without an LLM |
| `--max-calls`, `--max-cost` | Hard limits on LLM calls and USD, respectively |
| `--backend api` | Claude API instead of the local `claude` (`pip install anthropic`, `ANTHROPIC_API_KEY`) |

All options: `python -m moorlicht run --help`.

## How it works

- **Engine** (`moorlicht/engine.py`): owns the entire game state and enforces all rules
  deterministically. Agents only return decisions in a fixed schema; invalid responses
  are retried once and otherwise replaced by a permitted default action (visible in the
  viewer under "Technik").
- **Secrets** (`moorlicht/events.py`): a single event log with visibility per event
  (public, specific players, spectators only). Each player's prompt is built solely from
  that player's filtered view.
- **Agents** (`moorlicht/llm.py`): each player is a separate, persistent Claude session
  without tools, without MCP servers, without user settings, in an empty working
  directory – it can do nothing but play. If Fable 5.2's safety filter rejects a move
  twice, a fallback model steps in for that one decision (`--fallback-model`, recorded in
  the log).
- **Storyteller**: where the rules leave the storyteller a choice (above all: what
  information does a player in the fog receive?), an LLM storyteller chooses from options
  provided by the engine.

## Tests

```powershell
python -m unittest discover -s tests
```

The tests run without an LLM: hundreds of random games checked against rule invariants,
targeted scenarios for every mechanic, checks that no prompt contains knowledge the player
should not have, and the CLI backend tested against a fake `claude`.
