"""Kommandozeile: ``python -m moorlicht run | view | rules``."""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import prompts
from .agents import HeuristicStoryteller, LLMBrain, LLMStoryteller, MockBrain
from .engine import Config, Game
from .events import SPECTATOR, EventLog
from .llm import DEFAULT_FALLBACK_MODEL, DEFAULT_MODEL, LLMError, make_backend
from .narrate import Narrator
from .roles import DISTRIBUTION, DOERFLER, EIGENBROETLER, ROLES, SCHERGE, UNHOLD
from .server import open_browser, start_server, viewer_url

DEFAULT_GAMES_DIR = "games"


def _int_list(value: str) -> tuple[int, ...]:
    items = tuple(int(x) for x in value.split(",") if x.strip())
    if not items or any(i < 0 for i in items):
        raise argparse.ArgumentTypeError("erwartet z. B. 2,1")
    return items


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m moorlicht",
        description="Moorlicht: KI-Agenten spielen ein Deduktionsspiel mit verborgenen Rollen.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="eine Partie spielen lassen")
    run.add_argument("--seed", type=int, default=None, help="Zufalls-Seed für Rollen und Sitzordnung")
    run.add_argument("--players", type=int, default=8, choices=sorted(DISTRIBUTION), help="Spielerzahl (Standard 8)")
    run.add_argument("--roles", default=None, help="Rollen vorgeben, z. B. seherin,heilerin,... (IDs, siehe „rules“)")
    run.add_argument("--backend", default="auto", choices=["auto", "claude-cli", "api", "mock"],
                     help="auto = API mit ANTHROPIC_API_KEY, sonst lokales „claude“; mock = Zufallsspieler ohne Kosten")
    run.add_argument("--model", default=DEFAULT_MODEL, help=f"Modell der Agenten (Standard {DEFAULT_MODEL})")
    run.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"],
                     help="Denkaufwand pro Entscheidung (Standard medium)")
    run.add_argument("--fallback-model", default=DEFAULT_FALLBACK_MODEL,
                     help="Ausweichmodell, falls der Sicherheitsfilter des Hauptmodells einen Spielzug "
                          f"zweimal ablehnt (Standard {DEFAULT_FALLBACK_MODEL}; „none“ = keines; nur claude-cli)")
    run.add_argument("--stateless", action="store_true",
                     help="keine fortlaufende Sitzung je Spieler: jeder Aufruf enthält den ganzen "
                          "Verlauf (teurer; nur claude-cli)")
    run.add_argument("--storyteller", default="auto", choices=["auto", "llm", "heuristic"],
                     help="Erzähler-Ermessen: llm oder heuristic (auto = llm außer bei mock)")
    run.add_argument("--language", default="Deutsch", help="Sprache, in der die Agenten reden")
    run.add_argument("--max-days", type=int, default=7)
    run.add_argument("--whisper-rounds", type=_int_list, default=(2, 1),
                     help="Flüsterrunden pro Tag, z. B. 2,1 = zwei an Tag 1, danach eine")
    run.add_argument("--whisper-turns", type=int, default=2, help="Nachrichten pro Person und Flüstergespräch")
    run.add_argument("--discussion-rounds", type=int, default=2)
    run.add_argument("--nomination-passes", type=int, default=2)
    run.add_argument("--max-nominations", type=int, default=4)
    run.add_argument("--vote-mode", default="sequential", choices=["sequential", "parallel"],
                     help="sequential = reihum wie am Tisch; parallel = schneller, alle gleichzeitig")
    run.add_argument("--concurrency", type=int, default=4, help="gleichzeitige LLM-Aufrufe")
    run.add_argument("--timeout", type=int, default=600, help="Sekunden pro LLM-Aufruf")
    run.add_argument("--max-calls", type=int, default=900, help="harte Obergrenze für LLM-Aufrufe")
    run.add_argument("--max-cost", type=float, default=None, help="harte Obergrenze in USD (laut Backend)")
    run.add_argument("--out", default=DEFAULT_GAMES_DIR, help="Ordner für Spielprotokolle")
    run.add_argument("--view", action="store_true", help="Viewer starten und im Browser öffnen")
    run.add_argument("--port", type=int, default=8765)
    run.add_argument("--quiet", action="store_true", help="keine Mitschrift in der Konsole")
    run.add_argument("--no-notes", action="store_true", help="Notizen der Agenten nicht in der Konsole zeigen")

    view = sub.add_parser("view", help="Viewer für gespeicherte oder laufende Partien starten")
    view.add_argument("game", nargs="?", default=None, help="Name eines Spielordners (Standard: das neueste)")
    view.add_argument("--dir", default=DEFAULT_GAMES_DIR, help="Ordner mit den Spielprotokollen")
    view.add_argument("--port", type=int, default=8765)
    view.add_argument("--no-browser", action="store_true")

    sub.add_parser("rules", help="Regeln und Rollenliste ausgeben")
    return parser


def cmd_rules() -> int:
    n = 8
    counts = dict(zip((DOERFLER, EIGENBROETLER, SCHERGE, UNHOLD), DISTRIBUTION[n]))
    cfg = Config()
    start = {"data": {
        "players": [{"seat": i, "name": f"Spieler {i + 1}"} for i in range(n)],
        "script": [r.public() for r in ROLES],
        "counts": counts,
        "settings": {"max_days": cfg.max_days, "whisper_rounds": list(cfg.whisper_rounds),
                     "discussion_rounds": cfg.discussion_rounds,
                     "max_nominations": cfg.max_nominations},
    }}
    print(prompts.rules_text(start))
    print("\nRollen-IDs für --roles: " + ", ".join(r.id for r in ROLES))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    seed = args.seed if args.seed is not None else int(time.time()) % 1_000_000
    cfg = Config(
        n_players=args.players, seed=seed,
        roles=[r.strip() for r in args.roles.split(",")] if args.roles else None,
        max_days=args.max_days, whisper_rounds=args.whisper_rounds,
        whisper_turns=args.whisper_turns, discussion_rounds=args.discussion_rounds,
        nomination_passes=args.nomination_passes, max_nominations=args.max_nominations,
        vote_mode=args.vote_mode, language=args.language, concurrency=args.concurrency,
        max_calls=args.max_calls, max_cost=args.max_cost,
    )
    try:
        # Rollenvorgabe prüfen, bevor ein Protokoll angelegt oder ein Backend gestartet wird.
        Game(cfg, MockBrain(seed), HeuristicStoryteller(seed), EventLog()).setup()
    except ValueError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    backend = None
    if args.backend == "mock":
        brain = MockBrain(seed)
        backend_name, model = "mock", "mock"
    else:
        try:
            fallback = None if args.fallback_model.lower() in ("none", "") else args.fallback_model
            backend = make_backend(args.backend, args.model, args.effort, args.timeout,
                                   fallback_model=fallback, sessions=not args.stateless)
        except LLMError as exc:
            print(f"Fehler: {exc}", file=sys.stderr)
            return 2
        brain = LLMBrain(backend)
        backend_name, model = backend.name, args.model
    heuristic = HeuristicStoryteller(seed)
    use_llm_st = args.storyteller == "llm" or (args.storyteller == "auto" and backend is not None)
    if use_llm_st and backend is None:
        print("Hinweis: --storyteller llm braucht ein LLM-Backend; nutze heuristic.", file=sys.stderr)
        use_llm_st = False
    storyteller = LLMStoryteller(backend, heuristic) if use_llm_st else heuristic

    game_name = f"{time.strftime('%Y%m%d-%H%M%S')}-seed{seed}"
    game_dir = os.path.join(args.out, game_name)
    os.makedirs(game_dir, exist_ok=True)
    path = os.path.join(game_dir, "events.jsonl")
    log = EventLog(path)
    if not args.quiet:
        log.listeners.append(Narrator(show_notes=not args.no_notes))
    log.add("meta", SPECTATOR, backend=backend_name, model=model,
            effort=args.effort if backend else None, storyteller=storyteller.name, game=game_name)

    if args.view:
        try:
            _, port = start_server(os.path.abspath(args.out), args.port)
            url = viewer_url(port, game_name)
            print(f"Viewer: {url}")
            open_browser(url)
        except OSError as exc:
            print(f"Viewer konnte nicht gestartet werden: {exc}", file=sys.stderr)

    game = Game(cfg, brain, storyteller, log)
    try:
        game.run()
    except ValueError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    finally:
        log.close()
        if backend is not None:
            backend.close()
    print(f"\nProtokoll: {path}")
    print(f"Ansehen:   python -m moorlicht view {game_name}" + (f" --dir {args.out}" if args.out != DEFAULT_GAMES_DIR else ""))
    if args.view:
        print("Der Viewer läuft weiter – Strg+C beendet ihn.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    root = os.path.abspath(args.dir)
    if not os.path.isdir(root):
        print(f"Ordner nicht gefunden: {root}", file=sys.stderr)
        return 2
    _, port = start_server(root, args.port)
    url = viewer_url(port, args.game)
    print(f"Viewer läuft: {url}  (Strg+C beendet)")
    if not args.no_browser:
        open_browser(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if args.command == "rules":
        return cmd_rules()
    if args.command == "run":
        return cmd_run(args)
    return cmd_view(args)


__all__ = ["main"]
