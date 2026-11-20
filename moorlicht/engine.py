"""Die Spiel-Engine von Moorlicht.

Die Engine besitzt den vollständigen Spielzustand und setzt alle Regeln deterministisch
durch. Agenten liefern nur Entscheidungen (Rede, Wahl, Stimme); jede Entscheidung wird
gegen ein Schema und die Regeln geprüft, einmal wiederholt und notfalls durch eine
erlaubte Standardaktion ersetzt.
"""

from __future__ import annotations

import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from . import prompts
from .events import PUBLIC, SPECTATOR, EventLog
from .llm import LLMError, LLMResult
from .roles import (
    DISTRIBUTION,
    DOERFLER,
    DORF,
    EIGENBROETLER,
    MOOR,
    PLAYER_NAMES,
    ROLE_BY_ID,
    ROLES,
    SCHERGE,
    UNHOLD,
    Role,
    roles_of,
)

NOBODY = "niemand"


@dataclass
class Config:
    n_players: int = 8
    seed: int = 0
    roles: list[str] | None = None        # erzwungene Rollen (IDs), sonst zufällig
    max_days: int = 7
    whisper_rounds: tuple[int, ...] = (2, 1)  # Tag 1, Tag 2, ... (letzter Wert gilt weiter)
    whisper_turns: int = 2                # Nachrichten pro Person und Gespräch
    discussion_rounds: int = 2
    nomination_passes: int = 2
    max_nominations: int = 4
    vote_mode: str = "sequential"         # "sequential" (wie am Tisch) oder "parallel"
    language: str = "Deutsch"
    concurrency: int = 4
    max_calls: int | None = 900
    max_cost: float | None = None
    mark_prompts: bool = False            # nur für Tests: Ereignisnummern in Prompts


@dataclass
class Player:
    seat: int
    name: str
    role: Role
    alive: bool = True
    ghost_vote: bool = True
    fogged: bool = False          # Nebelkrähe: bis zur nächsten Abenddämmerung
    perma_fog: bool = False       # Nachtmahr: für den Rest des Spiels
    death: str | None = None      # "night" | "execution" | "shot"
    death_round: int | None = None
    last_target: int | None = None  # Heilerin / Nebelkrähe: Wahl der Vornacht
    armor_used: bool = False        # Schmiedin
    insight_used: bool = False      # Menschenkenner


@dataclass
class Request:
    seat: int
    kind: str
    task: str
    schema: dict
    fallback: Callable[[], dict]
    validate: Callable[[dict], str | None] | None = None


class BudgetExceeded(Exception):
    pass


class GameAborted(Exception):
    pass


# ------------------------------------------------------------------------- Helfer

NOTE = {
    "type": "string",
    "description": "Kurze Merknotiz für dich selbst (ein bis drei Sätze).",
}


def obj(**props: dict) -> dict:
    """Antwortschema einer Spielerentscheidung; die Merknotiz steht immer am Ende."""
    props = {**props, "notiz": NOTE}
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def pick(options: list[str], description: str) -> dict:
    return {"type": "string", "enum": list(options), "description": description}


def text(description: str) -> dict:
    return {"type": "string", "description": description}


def clip(value, limit: int) -> str:
    s = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def check_schema(data, schema: dict) -> str | None:
    if not isinstance(data, dict):
        return "Antwort ist kein Objekt"
    for key, spec in schema["properties"].items():
        if key not in data:
            return f"Feld „{key}“ fehlt"
        value = data[key]
        if spec.get("type") == "boolean":
            if not isinstance(value, bool):
                return f"„{clip(value, 40)}“ ist keine gültige Angabe – erlaubt sind nur ja oder nein"
        else:
            if not isinstance(value, str):
                return f"Feld „{key}“ muss Text sein"
            if "enum" in spec and value not in spec["enum"]:
                return (f"„{clip(value, 40)}“ ist kein erlaubter Wert – erlaubt sind: "
                        f"{', '.join(spec['enum'])}")
    return None


def match_pairs(prefs: dict[int, int | None], order: list[int]) -> list[tuple[int, int]]:
    """Bildet Flüsterpaare: erst gegenseitige Wünsche, dann reihum der Wunsch des
    jeweils Nächsten, sofern der Gewünschte noch frei ist und überhaupt reden will.
    Der Erstgenannte eines Paares eröffnet das Gespräch."""
    paired: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for a in order:
        b = prefs.get(a)
        if b is None or a in paired or b in paired or a == b:
            continue
        if prefs.get(b) == a:
            pairs.append((a, b))
            paired |= {a, b}
    for a in order:
        b = prefs.get(a)
        if b is None or a in paired or b in paired or a == b:
            continue
        if prefs.get(b) is None:
            continue
        pairs.append((a, b))
        paired |= {a, b}
    return pairs


# -------------------------------------------------------------------------- Spiel

class Game:
    def __init__(self, cfg: Config, brain, storyteller, log: EventLog):
        if cfg.n_players not in DISTRIBUTION:
            raise ValueError(f"Spielerzahl {cfg.n_players} wird nicht unterstützt (7–9).")
        self.cfg = cfg
        self.brain = brain
        self.st = storyteller
        self.log = log
        self.rng = random.Random(cfg.seed)
        self.players: list[Player] = []
        self.bluffs: list[Role] = []
        self.round = 0
        self.winner: str | None = None
        self.end_reason = ""
        # Nacht-/Tageszustand
        self.framed: set[int] = set()
        self.blackmailed: int | None = None
        self.double_kill = False
        self.night_deaths: list[int] = []
        self.nominators: set[int] = set()
        self.nominated: set[int] = set()
        self.block: int | None = None
        self.block_votes = 0
        # Verbrauch
        self.calls = 0
        self.cost = 0.0
        self.tokens_in = 0
        self.tokens_out = 0
        self.fallbacks = 0
        self._backend_errors = 0
        self._lock = threading.Lock()
        self._system: dict[int, str] = {}
        self._seen: dict[int, int] = {}  # Platz -> letztes Ereignis, das schon im Prompt stand
        self._started = time.time()

    # ------------------------------------------------------------------ Zugriff

    @property
    def n(self) -> int:
        return len(self.players)

    def alive(self) -> list[Player]:
        return [p for p in self.players if p.alive]

    def name(self, seat: int) -> str:
        return self.players[seat].name

    def seat_of(self, name: str) -> int:
        for p in self.players:
            if p.name == name:
                return p.seat
        raise KeyError(name)

    def by_role(self, role_id: str) -> Player | None:
        return next((p for p in self.players if p.role.id == role_id), None)

    def unhold(self) -> Player:
        return next(p for p in self.players if p.role.typ == UNHOLD)

    def scherge(self) -> Player | None:
        return next((p for p in self.players if p.role.typ == SCHERGE), None)

    def is_fogged(self, p: Player) -> bool:
        return p.fogged or p.perma_fog

    def has_ability(self, p: Player) -> bool:
        return p.alive and not self.is_fogged(p)

    def appears_moor(self, target: Player, for_seherin: bool) -> bool:
        if target.role.typ == UNHOLD:
            return not for_seherin
        if target.role.typ == SCHERGE:
            return True
        if target.role.id == "mueller" and self.has_ability(target):
            return True
        return target.seat in self.framed

    # ------------------------------------------------------------------- Aufbau

    def setup(self) -> None:
        cfg = self.cfg
        n_d, n_e, n_s, n_u = DISTRIBUTION[cfg.n_players]
        if cfg.roles:
            unknown = [r for r in cfg.roles if r not in ROLE_BY_ID]
            if unknown:
                raise ValueError(
                    f"Unbekannte Rollen-ID: {', '.join(unknown)}. Erlaubt sind: "
                    f"{', '.join(ROLE_BY_ID)}."
                )
            roles = [ROLE_BY_ID[r] for r in cfg.roles]
            got = tuple(sum(1 for r in roles if r.typ == t) for t in (DOERFLER, EIGENBROETLER, SCHERGE, UNHOLD))
            if got != (n_d, n_e, n_s, n_u) or len(set(roles)) != len(roles):
                raise ValueError(
                    f"Rollenvorgabe passt nicht: erwartet {n_d} Dörfler, {n_e} Eigenbrötler, "
                    f"{n_s} Scherge, {n_u} Unhold (ohne Doppelte), erhalten {got}."
                )
        else:
            roles = (
                self.rng.sample(roles_of(DOERFLER), n_d)
                + self.rng.sample(roles_of(EIGENBROETLER), n_e)
                + self.rng.sample(roles_of(SCHERGE), n_s)
                + self.rng.sample(roles_of(UNHOLD), n_u)
            )
        self.rng.shuffle(roles)
        self.players = [Player(i, PLAYER_NAMES[i], roles[i]) for i in range(cfg.n_players)]
        unused = [r for r in roles_of(DOERFLER) if r not in roles]
        self.bluffs = self.rng.sample(unused, min(3, len(unused)))

        self.log.round, self.log.phase = 0, "setup"
        start = self.log.add(
            "game_start",
            PUBLIC,
            game="Moorlicht",
            seed=cfg.seed,
            players=[{"seat": p.seat, "name": p.name} for p in self.players],
            script=[r.public() for r in ROLES],
            counts={DOERFLER: n_d, EIGENBROETLER: n_e, SCHERGE: n_s, UNHOLD: n_u},
            settings={
                "max_days": cfg.max_days,
                "whisper_rounds": list(cfg.whisper_rounds),
                "whisper_turns": cfg.whisper_turns,
                "discussion_rounds": cfg.discussion_rounds,
                "max_nominations": cfg.max_nominations,
                "language": cfg.language,
            },
        )
        for p in self.players:
            self._system[p.seat] = prompts.system_prompt(start, p.seat)
            self.log.add("role", [p.seat], seat=p.seat, role=p.role.public())
        evil = [p for p in self.players if p.role.team == MOOR]
        self.log.add(
            "evil_info",
            [p.seat for p in evil],
            members=[
                {"seat": p.seat, "name": p.name, "role": p.role.name,
                 "artikel": p.role.artikel, "typ": p.role.typ}
                for p in evil
            ],
            bluffs=[r.name for r in self.bluffs],
        )
        self.snapshot("Aufbau")

    def snapshot(self, note: str) -> None:
        self.log.add(
            "grimoire",
            SPECTATOR,
            note=note,
            players=[
                {
                    "seat": p.seat, "name": p.name, "role": p.role.name, "role_id": p.role.id,
                    "typ": p.role.typ, "team": p.role.team, "alive": p.alive,
                    "ghost_vote": p.ghost_vote, "fog": p.fogged, "perma_fog": p.perma_fog,
                    "death": p.death,
                }
                for p in self.players
            ],
            framed=sorted(self.framed),
            blackmailed=self.blackmailed,
            bluffs=[r.name for r in self.bluffs],
        )

    # ------------------------------------------------------- Agenten befragen

    def _reserve_call(self) -> None:
        with self._lock:
            if self.cfg.max_calls is not None and self.calls >= self.cfg.max_calls:
                raise BudgetExceeded(f"Aufruf-Limit erreicht ({self.cfg.max_calls} LLM-Aufrufe)")
            if self.cfg.max_cost is not None and self.cost >= self.cfg.max_cost:
                raise BudgetExceeded(f"Kosten-Limit erreicht ({self.cfg.max_cost:.2f} USD)")
            self.calls += 1

    def _account(self, seat: int | None, kind: str, res: LLMResult, attempt: int) -> None:
        with self._lock:
            self.cost += res.cost_usd or 0.0
            self.tokens_in += res.input_tokens + res.cache_read_tokens + res.cache_write_tokens
            self.tokens_out += res.output_tokens
            if res.data is None:
                self._backend_errors += 1
            else:
                self._backend_errors = 0
            broken = self._backend_errors
        self.log.add(
            "usage", SPECTATOR, seat=seat, kind=kind, attempt=attempt,
            input_tokens=res.input_tokens, cache_read_tokens=res.cache_read_tokens,
            cache_write_tokens=res.cache_write_tokens, output_tokens=res.output_tokens,
            cost_usd=res.cost_usd, seconds=round(res.seconds, 2), error=res.error,
            model=res.model, **res.extra,
        )
        if res.extra.get("fallback_model"):
            who = self.name(seat) if seat is not None else "dem Erzähler"
            self.log.add(
                "st_note", SPECTATOR,
                text=f"Technischer Hinweis: Eine Entscheidung von {who} hat das "
                     f"Ausweichmodell {res.model} getroffen, weil der Sicherheitsfilter "
                     "des Hauptmodells die Anfrage abgelehnt hat.",
            )
        if broken >= 6:
            raise GameAborted(
                f"Sechs LLM-Aufrufe in Folge sind fehlgeschlagen (zuletzt: {res.error}). "
                "Vermutlich ist das Nutzungslimit erreicht oder die Anmeldung abgelaufen."
            )

    def ask(self, req: Request) -> dict:
        """Eine Entscheidung eines Spielers einholen.

        Es entstehen immer zwei Fassungen des Prompts, beide ausschließlich aus der
        gefilterten Sicht des Spielers: ``user`` mit dem vollständigen Spielverlauf und
        ``delta`` mit nur dem, was seit seiner letzten Entscheidung neu ist. Backends mit
        fortlaufender Sitzung je Spieler nutzen ``delta``, alle anderen ``user``.
        """
        view = self.log.view(req.seat)
        mark = self.cfg.mark_prompts
        user = prompts.build_user_prompt(view, req.seat, req.task, mark=mark)
        delta = prompts.build_delta_prompt(
            view, req.seat, req.task, self._seen.get(req.seat, -1), mark=mark)
        self._seen[req.seat] = view[-1]["i"]
        system = self._system[req.seat]
        data, error = None, None
        for attempt in (1, 2):
            self._reserve_call()
            if attempt == 2:
                retry = prompts.RETRY_NOTE.format(error=error)
                user += retry
                delta = retry.strip() + "\n\n# Deine Aufgabe jetzt\n" + req.task.strip()
            res = self.brain.decide(req.seat, req.kind, system, user, req.schema, delta=delta)
            self._account(req.seat, req.kind, res, attempt)
            error = res.error or check_schema(res.data, req.schema)
            if not error and req.validate:
                error = req.validate(res.data)
            if not error:
                data = res.data
                break
        if data is None:
            data = req.fallback()
            with self._lock:
                self.fallbacks += 1
            self.log.add("fallback", SPECTATOR, seat=req.seat, kind=req.kind, error=error, used=data)
        note = clip(data.get("notiz", ""), 1200)
        if note:
            self.log.add("note", [req.seat], seat=req.seat, kind=req.kind, text=note)
        return data

    def ask_one(self, req: Request) -> dict:
        self.log.add("wait", SPECTATOR, seats=[req.seat], kind=req.kind)
        return self.ask(req)

    def ask_many(self, reqs: list[Request]) -> list[dict]:
        if not reqs:
            return []
        self.log.add("wait", SPECTATOR, seats=[r.seat for r in reqs], kind=reqs[0].kind)
        return self._parallel(self.ask, reqs)

    def _parallel(self, fn, items: list) -> list:
        if self.cfg.concurrency <= 1 or len(items) <= 1:
            return [fn(item) for item in items]
        with ThreadPoolExecutor(max_workers=self.cfg.concurrency) as pool:
            return list(pool.map(fn, items))

    def st_choose(self, p: Player, question: str, options: list[str], truth: int | None) -> int:
        """Eine Ermessensentscheidung des Erzählers – immer aus vorgegebenen Optionen."""
        if len(options) == 1:
            return 0
        context = self._st_context(p)
        llm = getattr(self.st, "name", "") == "llm"
        if llm:
            self._reserve_call()
        index, reason, res = self.st.choose(context, question, options, truth)
        if res is not None:
            self._account(None, "storyteller", res, 1)
        if not 0 <= index < len(options):
            index = truth if truth is not None else 0
        self.log.add(
            "st_decision", SPECTATOR, seat=p.seat, question=question, options=options,
            truth=truth, chosen=index, reason=reason,
        )
        return index

    def _st_context(self, p: Player) -> str:
        lines = [f"Nacht {self.round}. Sitzkreis im Uhrzeigersinn:"]
        for q in self.players:
            flags = ["lebt" if q.alive else "tot"]
            if q.perma_fog:
                flags.append("dauerhaft im Nebel (Nachtmahr)")
            elif q.fogged:
                flags.append("im Nebel (Nebelkrähe)")
            if q.seat in self.framed:
                flags.append("heute Nacht verleumdet")
            lines.append(
                f"- Platz {q.seat + 1} {q.name}: {q.role.name} ({q.role.typ}, Team "
                f"{q.role.team}) – {', '.join(flags)}"
            )
        earlier = [
            e["data"]["text"] for e in self.log.view(p.seat) if e["type"] == "night_info"
        ]
        if earlier:
            lines.append(f"Bisherige Informationen an {p.name}:")
            lines.extend(f"- {t}" for t in earlier)
        return "\n".join(lines)

    # --------------------------------------------------------------------- Nacht

    def _night_request(self, p: Player, first: bool, double: bool) -> Request | None:
        rid = p.role.id
        others_alive = [q.name for q in self.alive() if q.seat != p.seat]
        others_all = [q.name for q in self.players if q.seat != p.seat]
        intro = f"Es ist Nacht {self.round}. Der Erzähler weckt dich ({p.role.name})."

        def single(options: list[str], instruction: str) -> Request | None:
            if not options:
                return None
            return Request(
                p.seat, "night", f"{intro} {instruction}",
                obj(ziel=pick(options, "Der Spieler, den du wählst.")),
                fallback=lambda: {"notiz": "", "ziel": self.rng.choice(options)},
            )

        if rid == "seherin":
            return single(others_all, "Wähle einen anderen Spieler, den du prüfen willst.")
        if rid == "faehrtenleser":
            return single(others_alive, "Wähle einen anderen lebenden Spieler, dessen Fährte du verfolgst.")
        if rid == "nachtwaechter":
            return single([q.name for q in self.alive()],
                          "Wähle einen lebenden Spieler (auch dich selbst), dessen Haus du beobachtest.")
        if rid == "rutengaengerin":
            return single([q.name for q in self.players],
                          "Wähle einen Spieler; geprüft werden er und seine beiden Sitznachbarn.")
        if rid in ("heilerin", "nebelkraehe"):
            if rid == "heilerin" and first:
                return None
            options = [nm for nm in others_alive
                       if p.last_target is None or nm != self.name(p.last_target)]
            last = (
                f" Nicht erlaubt ist {self.name(p.last_target)} (deine Wahl der letzten Nacht)."
                if p.last_target is not None else ""
            )
            what = (
                "Wähle einen anderen lebenden Spieler, den du heute Nacht schützt."
                if rid == "heilerin"
                else "Wähle einen anderen lebenden Spieler, den du in Nebel hüllst."
            )
            return single(options, what + last)
        if rid == "verleumder":
            return single(others_all, "Wähle einen anderen Spieler, den du heute Nacht verleumdest.")
        if rid == "erpresser":
            return single(others_alive, "Wähle einen anderen lebenden Spieler, den du für morgen erpresst.")
        if rid == "menschenkenner":
            if p.insight_used:
                return None
            role_names = [r.name for r in ROLES]
            return Request(
                p.seat, "night",
                f"{intro} Du kannst deine einmalige Fähigkeit jetzt einsetzen: Nenne einen "
                "anderen Spieler und eine Rolle, und du erfährst, ob er genau diese Rolle "
                f"hat. Willst du sie aufsparen, wähle als Ziel „{NOBODY}“ (die Rolle ist dann egal).",
                obj(
                    ziel=pick(others_all + [NOBODY], f"Spieler oder „{NOBODY}“."),
                    rolle=pick(role_names, "Die Rolle, nach der du fragst."),
                ),
                fallback=lambda: {"notiz": "", "ziel": NOBODY, "rolle": role_names[0]},
            )
        if p.role.typ == UNHOLD:
            if first:
                if rid != "nachtmahr":
                    return None
                return single(others_all,
                              "Wähle einen anderen Spieler: Er steht für den Rest des Spiels im Nebel.")
            if double and len(others_alive) >= 2:
                def valid(d: dict) -> str | None:
                    return "die beiden Opfer müssen verschieden sein" if d["ziel"] == d["ziel2"] else None

                def fb() -> dict:
                    a, b = self.rng.sample(others_alive, 2)
                    return {"notiz": "", "ziel": a, "ziel2": b}

                return Request(
                    p.seat, "night",
                    f"{intro} Der Pechvogel wurde hingerichtet: Du darfst heute Nacht zwei "
                    "verschiedene lebende Spieler töten.",
                    obj(ziel=pick(others_alive, "Erstes Opfer."),
                        ziel2=pick(others_alive, "Zweites Opfer (ein anderer Spieler).")),
                    fallback=fb, validate=valid,
                )
            return single(others_alive, "Wähle einen anderen lebenden Spieler, der heute Nacht sterben soll.")
        return None

    def _die(self, p: Player, cause: str) -> None:
        p.alive = False
        p.death = cause
        p.death_round = self.round

    def run_night(self) -> None:
        first = self.round == 1
        self.log.round, self.log.phase = self.round, "night"
        self.log.add("phase", PUBLIC, kind="night", n=self.round)
        # Abenddämmerung: Wirkungen des Vortags enden.
        for p in self.players:
            p.fogged = False
        self.framed.clear()
        self.blackmailed = None
        double, self.double_kill = self.double_kill, False
        self.night_deaths = []

        reqs = [r for r in (self._night_request(p, first, double) for p in self.alive()) if r]
        answers = self.ask_many(reqs)
        choice: dict[int, dict] = {r.seat: a for r, a in zip(reqs, answers)}

        visits: dict[int, list[int]] = {}

        def visit(actor: Player, target: int) -> None:
            if target != actor.seat:
                visits.setdefault(actor.seat, []).append(target)

        def note(text_: str) -> None:
            self.log.add("st_note", SPECTATOR, text=text_)

        def told(p: Player, text_: str) -> None:
            self.log.add("night_choice", [p.seat], seat=p.seat, text=text_)

        def fog_suffix(p: Player) -> str:
            return " – wirkungslos, steht selbst im Nebel" if self.is_fogged(p) else ""

        # 1. Scherge
        sch = self.scherge()
        if sch and sch.alive and sch.seat in choice:
            t = self.seat_of(choice[sch.seat]["ziel"])
            visit(sch, t)
            told(sch, f"Du wählst {self.name(t)}.")
            works = not self.is_fogged(sch)
            if sch.role.id == "nebelkraehe":
                sch.last_target = t
                if works:
                    self.players[t].fogged = True
                note(f"{sch.name} (Nebelkrähe) hüllt {self.name(t)} in Nebel{fog_suffix(sch)}.")
            elif sch.role.id == "verleumder":
                if works:
                    self.framed.add(t)
                note(f"{sch.name} (Verleumder) verleumdet {self.name(t)}{fog_suffix(sch)}.")
            elif sch.role.id == "erpresser":
                if works:
                    self.blackmailed = t
                note(f"{sch.name} (Erpresser) erpresst {self.name(t)}{fog_suffix(sch)}.")

        unhold = self.unhold()
        # 1b. Nachtmahr, erste Nacht: dauerhafter Nebel
        if first and unhold.role.id == "nachtmahr" and unhold.seat in choice:
            t = self.seat_of(choice[unhold.seat]["ziel"])
            visit(unhold, t)
            told(unhold, f"Du wählst {self.name(t)}.")
            if not self.is_fogged(unhold):
                self.players[t].perma_fog = True
            note(f"{unhold.name} (Nachtmahr) legt dauerhaften Nebel auf {self.name(t)}{fog_suffix(unhold)}.")

        # 2. Heilerin
        protected: set[int] = set()
        healer = self.by_role("heilerin")
        if healer and healer.alive and healer.seat in choice:
            t = self.seat_of(choice[healer.seat]["ziel"])
            visit(healer, t)
            healer.last_target = t
            told(healer, f"Du wählst {self.name(t)}.")
            if not self.is_fogged(healer):
                protected.add(t)
            note(f"{healer.name} (Heilerin) schützt {self.name(t)}{fog_suffix(healer)}.")

        # 3. Unhold
        if not first and unhold.alive and unhold.seat in choice:
            c = choice[unhold.seat]
            targets = [self.seat_of(c["ziel"])]
            if "ziel2" in c:
                targets.append(self.seat_of(c["ziel2"]))
            told(unhold, f"Du wählst {prompts.join_names([self.name(t) for t in targets])}.")
            for t in targets:
                visit(unhold, t)
                victim = self.players[t]
                if self.is_fogged(unhold):
                    outcome = "der Angriff verpufft, der Unhold steht im Nebel"
                elif not victim.alive:
                    outcome = "war bereits tot"
                elif t in protected:
                    outcome = "überlebt dank der Heilerin"
                elif victim.role.id == "schmiedin" and not victim.armor_used and not self.is_fogged(victim):
                    victim.armor_used = True
                    outcome = "überlebt dank der Rüstung der Schmiedin"
                else:
                    self._die(victim, "night")
                    self.night_deaths.append(t)
                    outcome = "stirbt"
                note(f"{unhold.name} ({unhold.role.name}) greift {victim.name} an: {outcome}.")

        # 4. Jäger
        for seat in list(self.night_deaths):
            hunter = self.players[seat]
            if hunter.role.id != "jaeger":
                continue
            if self.is_fogged(hunter):
                note(f"{hunter.name} (Jäger) stirbt im Nebel – kein Schuss.")
                continue
            target = self._hunter_shot(hunter, night=True)
            if target is not None:
                self.night_deaths.append(target)

        # 5. Schlafwandler
        walker = self.by_role("schlafwandler")
        if walker and self.has_ability(walker):
            options = [q.seat for q in self.alive() if q.seat != walker.seat]
            if options:
                t = self.rng.choice(options)
                visit(walker, t)
                note(f"{walker.name} (Schlafwandler) wandelt zu {self.name(t)}.")

        # 6. Informationsrollen: erst alle Besuche eintragen, dann auswerten.
        info_roles = ("seherin", "rutengaengerin", "menschenkenner", "faehrtenleser", "nachtwaechter")
        acting: list[tuple[Player, int]] = []
        for rid in info_roles:
            p = self.by_role(rid)
            if not p or p.seat not in choice:
                continue
            c = choice[p.seat]
            if c["ziel"] == NOBODY:
                if p.alive:
                    told(p, "Du sparst deine Fähigkeit auf.")
                continue
            if not p.alive:
                told(p, f"Du hattest {c['ziel']} gewählt, bist aber gestorben, bevor du "
                        "an der Reihe warst.")
                continue
            t = self.seat_of(c["ziel"])
            visit(p, t)
            if rid == "menschenkenner":
                p.insight_used = True
                told(p, f"Du fragst den Erzähler: Ist {self.name(t)} „{c['rolle']}“?")
            else:
                told(p, f"Du wählst {self.name(t)}.")
            acting.append((p, t))
        if first:
            for rid in ("laternentraeger", "archivarin"):
                p = self.by_role(rid)
                if p and p.alive:
                    acting.append((p, -1))
        for p, t in acting:
            self._give_info(p, t, choice.get(p.seat, {}), visits)

        self.snapshot(f"Ende von Nacht {self.round}")

    def _give_info(self, p: Player, t: int, c: dict, visits: dict[int, list[int]]) -> None:
        rid = p.role.id
        target = self.players[t] if t >= 0 else None
        n = self.n
        question = ""
        options: list[str] = []
        truth: int | None = None
        render: Callable[[str], str] = lambda o: o

        if rid == "seherin":
            options = ["Dorf", "Moor"]
            truth = 1 if self.appears_moor(target, for_seherin=True) else 0
            question = f"Als was erscheint {target.name} der Seherin {p.name}?"
            render = lambda o: f"{target.name} erscheint als „{o}“."
        elif rid == "rutengaengerin":
            trio = [self.players[(t - 1) % n], target, self.players[(t + 1) % n]]
            count = sum(1 for q in trio if self.appears_moor(q, for_seherin=False))
            options = ["0", "1", "2", "3"]
            truth = count
            question = (
                f"Wie viele der Plätze {trio[0].name}, {trio[1].name}, {trio[2].name} "
                f"erscheinen der Rutengängerin {p.name} als „Moor“?"
            )
            render = lambda o: (
                f"{target.name} mit den Sitznachbarn {trio[0].name} und {trio[2].name} – "
                f"Anzahl „Moor“: {o}."
            )
        elif rid == "menschenkenner":
            options = ["Ja", "Nein"]
            truth = 0 if target.role.name == c["rolle"] else 1
            question = f"{p.name} (Menschenkenner) fragt: Ist {target.name} „{c['rolle']}“?"
            render = lambda o: f"Ist {target.name} „{c['rolle']}“? – {o}."
        elif rid == "faehrtenleser":
            seen = [self.name(s) for s in visits.get(t, [])]
            true_text = prompts.join_names(seen) if seen else "niemanden"
            options = ["niemanden"] + [q.name for q in self.players if q.seat != t]
            if true_text not in options:
                options.append(true_text)
            truth = options.index(true_text)
            question = f"Wen hat {target.name} laut Fährtenleser {p.name} in dieser Nacht besucht?"
            render = lambda o: f"{target.name} hat in dieser Nacht {o} besucht."
        elif rid == "nachtwaechter":
            count = sum(1 for actor, ts in visits.items() if actor != p.seat and t in ts)
            options = [str(i) for i in range(max(4, count + 1))]
            truth = count
            question = (
                f"Wie viele andere Spieler haben {target.name} laut Nachtwächter {p.name} "
                "in dieser Nacht besucht?"
            )
            render = lambda o: f"Anzahl der Besucher bei {target.name} in dieser Nacht (ohne dich): {o}."
        elif rid == "laternentraeger":
            dist = (self.unhold().seat - p.seat) % n
            options = ["im Uhrzeigersinn", "gegen den Uhrzeigersinn"]
            truth = 0 if dist * 2 < n else 1 if dist * 2 > n else None
            question = (
                f"In welcher Richtung sitzt der Unhold von Laternenträger {p.name} aus näher?"
                + (" (Er sitzt genau gegenüber.)" if truth is None else "")
            )
            render = lambda o: f"Der Unhold sitzt von dir aus näher {o}."
        elif rid == "archivarin":
            in_play = {q.role for q in self.players}
            unused = [r.name for r in roles_of(DOERFLER) if r not in in_play]
            used = [r.name for r in roles_of(DOERFLER) if r in in_play and r.id != "archivarin"]
            a, b = self.rng.sample(unused, 2)
            options = [f"{a} und {b}"]
            truth = 0
            if self.is_fogged(p) and used:
                seen_pairs = {frozenset((a, b))}
                candidates = [(self.rng.choice(used), self.rng.choice(unused)) for _ in range(4)]
                if len(used) >= 2:
                    candidates.append(tuple(self.rng.sample(used, 2)))
                for x, y in candidates:
                    if frozenset((x, y)) not in seen_pairs:
                        seen_pairs.add(frozenset((x, y)))
                        options.append(f"{x} und {y}")
            question = f"Welche zwei Dörfler-Rollen nennst du der Archivarin {p.name} als „nicht im Spiel“?"
            render = lambda o: f"Diese beiden Dörfler-Rollen sind nicht im Spiel: {o}."
        else:
            return

        fogged = self.is_fogged(p)
        if truth is not None and not fogged:
            given = truth
        else:
            given = self.st_choose(p, question, options, truth)
        text_ = render(options[given])
        self.log.add("night_info", [p.seat], seat=p.seat, text=text_)
        if truth is None:
            verdict = "freie Wahl des Erzählers"
        elif given == truth:
            verdict = "wahr" + (" (trotz Nebel)" if fogged else "")
        else:
            verdict = f"FALSCH wegen Nebel (wahr wäre: {options[truth]})"
        self.log.add("st_note", SPECTATOR, text=f"Info an {p.name} ({p.role.name}): {text_} – {verdict}.")

    def _hunter_shot(self, hunter: Player, night: bool) -> int | None:
        options = [q.name for q in self.alive()]
        if not options:
            return None
        when = (
            "Du bist in dieser Nacht gestorben." if night else "Du wirst gerade hingerichtet."
        )
        self.log.add(
            "private_note", [hunter.seat], seat=hunter.seat,
            text=f"{when} Als Jäger nimmst du sofort einen lebenden Spieler mit in den Tod.",
        )
        data = self.ask_one(Request(
            hunter.seat, "shot",
            f"{when} Deine Jäger-Fähigkeit löst aus: Wähle jetzt einen lebenden Spieler, der "
            "ebenfalls stirbt." + ("" if night else " Dein Schuss geschieht öffentlich."),
            obj(ziel=pick(options, "Der Spieler, auf den du schießt.")),
            fallback=lambda: {"notiz": "", "ziel": self.rng.choice(options)},
        ))
        t = self.seat_of(data["ziel"])
        self._die(self.players[t], "shot")
        self.log.add("night_choice", [hunter.seat], seat=hunter.seat, text=f"Du schießt auf {self.name(t)}.")
        if night:
            self.log.add("st_note", SPECTATOR, text=f"{hunter.name} (Jäger) erschießt {self.name(t)}.")
        else:
            self.log.add("shot", PUBLIC, by=hunter.seat, target=t)
        return t

    # ----------------------------------------------------------------------- Tag

    def check_win(self) -> bool:
        if self.winner:
            return True
        if not self.unhold().alive:
            self.winner = DORF
            self.end_reason = "Der Unhold ist tot."
        elif len(self.alive()) <= 2:
            self.winner = MOOR
            self.end_reason = "Es leben nur noch zwei Spieler, und der Unhold ist einer davon."
        return self.winner is not None

    def run_day(self) -> None:
        cfg = self.cfg
        self.log.round, self.log.phase = self.round, "day"
        self.log.add("phase", PUBLIC, kind="day", n=self.round)
        self.log.add("dawn", PUBLIC, deaths=list(self.night_deaths))
        if self.check_win():
            return
        if self.blackmailed is not None:
            victim = self.players[self.blackmailed]
            if victim.alive:
                self.log.add(
                    "private_note", [victim.seat], seat=victim.seat,
                    text="Der Erzähler teilt dir mit: Du wirst erpresst. Heute darfst du "
                         "weder nominieren noch abstimmen.",
                )
            else:
                self.blackmailed = None
        self.nominators, self.nominated = set(), set()
        self.block, self.block_votes = None, 0

        idx = min(self.round - 1, len(cfg.whisper_rounds) - 1)
        for r in range(1, cfg.whisper_rounds[idx] + 1):
            self._whisper_round(r)
        for r in range(1, cfg.discussion_rounds + 1):
            self._discussion_round(r)
        self._nominations()
        self._execution()
        self.snapshot(f"Ende von Tag {self.round}")
        self.check_win()

    def _order(self, start: int) -> list[int]:
        return [(start + k) % self.n for k in range(self.n)]

    def _whisper_round(self, r: int) -> None:
        order = self._order((self.round - 1 + r - 1) % self.n)
        reqs = []
        for seat in order:
            others = [q.name for q in self.players if q.seat != seat]

            def valid(d: dict) -> str | None:
                if d["partner"] != NOBODY and not d["eroeffnung"].strip():
                    return "die Eröffnungsnachricht darf nicht leer sein, wenn du einen Partner wählst"
                return None

            reqs.append(Request(
                seat, "whisper_choice",
                f"Flüsterrunde {r} an Tag {self.round}: Mit wem willst du jetzt unter vier "
                "Augen sprechen? Alle sehen, wer mit wem flüstert, aber nicht den Inhalt. "
                "Kommt das Gespräch zustande und du eröffnest es, wird deine "
                f"Eröffnungsnachricht verwendet. Wähle „{NOBODY}“, wenn du mit niemandem "
                "flüstern willst. Wünschen sich mehrere denselben Partner, entscheidet der "
                "Erzähler; vielleicht spricht dich auch jemand anderes an.",
                obj(
                    partner=pick(others + [NOBODY], "Dein Wunschpartner."),
                    eroeffnung=text("Deine erste Flüsternachricht an den Wunschpartner."),
                ),
                fallback=lambda: {"notiz": "", "partner": NOBODY, "eroeffnung": ""},
                validate=valid,
            ))
        answers = self.ask_many(reqs)
        prefs: dict[int, int | None] = {}
        opening: dict[int, str] = {}
        for req, a in zip(reqs, answers):
            prefs[req.seat] = None if a["partner"] == NOBODY else self.seat_of(a["partner"])
            opening[req.seat] = clip(a["eroeffnung"], 700)
        pairs = match_pairs(prefs, order)
        busy = {s for pair in pairs for s in pair}
        self.log.add(
            "whisper_pairs", PUBLIC, round=r, pairs=[list(p) for p in pairs],
            alone=[s for s in order if s not in busy],
        )
        if pairs:
            self.log.add("wait", SPECTATOR, seats=sorted(busy), kind="whisper_reply")
            self._parallel(lambda pair: self._converse(pair, opening, prefs), pairs)

    def _converse(self, pair: tuple[int, int], opening: dict[int, str], prefs: dict) -> None:
        a, b = pair
        total = 2 * self.cfg.whisper_turns
        first = opening[a] if prefs.get(a) == b else ""
        self.log.add("whisper", [a, b], **{"from": a, "to": b, "text": first or "Hast du kurz Zeit?"})
        for k in range(1, total):
            speaker, listener = (b, a) if k % 2 else (a, b)
            closing = (
                " Das ist deine letzte Nachricht in diesem Gespräch." if k >= total - 2 else ""
            )
            data = self.ask(Request(
                speaker, "whisper_reply",
                f"Du flüsterst gerade unter vier Augen mit {self.name(listener)} (das "
                f"Gespräch steht am Ende deines Spielverlaufs). Antworte.{closing}",
                obj(nachricht=text("Deine Flüsternachricht.")),
                fallback=lambda: {"notiz": "", "nachricht": "…"},
            ))
            self.log.add(
                "whisper", [a, b],
                **{"from": speaker, "to": listener, "text": clip(data["nachricht"], 700) or "…"},
            )

    def _discussion_round(self, r: int) -> None:
        self.log.add("discussion_start", PUBLIC, round=r)
        last = r == self.cfg.discussion_rounds
        for seat in self._order((self.round - 1 + r - 1) % self.n):
            data = self.ask_one(Request(
                seat, "speech",
                f"Öffentliche Diskussion an Tag {self.round}, Runde {r} von "
                f"{self.cfg.discussion_rounds}: Du bist an der Reihe. Was sagst du zur "
                "ganzen Runde? Du darfst auch schweigen (dann den Text leer lassen)."
                + (" Danach beginnen die Nominierungen." if last else ""),
                obj(rede=text("Deine öffentliche Wortmeldung (oder leer).")),
                fallback=lambda: {"notiz": "", "rede": ""},
            ))
            self.log.add("speech", PUBLIC, seat=seat, text=clip(data["rede"], 900), context="discussion")

    def _block_text(self) -> str:
        if self.block is not None:
            return (
                f"Auf dem Richtblock steht bisher {self.name(self.block)} mit "
                f"{self.block_votes} Ja-Stimmen; wer ihn verdrängen will, braucht mehr."
            )
        if self.block_votes:
            return (
                f"Im Moment steht niemand auf dem Richtblock (Gleichstand bei "
                f"{self.block_votes} Ja-Stimmen); eine Nominierung braucht mehr als das."
            )
        return "Bisher steht niemand auf dem Richtblock."

    def _nominations(self) -> None:
        cfg = self.cfg
        threshold = math.ceil(len(self.alive()) / 2)
        self.log.add("nominations_open", PUBLIC, threshold=threshold, alive=len(self.alive()))
        order = self._order((self.round - 1) % self.n)
        count = 0
        for pass_no in range(1, cfg.nomination_passes + 1):
            for seat in order:
                if count >= cfg.max_nominations:
                    break
                p = self.players[seat]
                if not p.alive or seat in self.nominators or seat == self.blackmailed:
                    continue
                candidates = [q.name for q in self.alive() if q.seat not in self.nominated]
                if not candidates:
                    break
                final = pass_no == cfg.nomination_passes
                data = self.ask_one(Request(
                    seat, "nominate",
                    f"Nominierungen an Tag {self.round}: Du bist an der Reihe und darfst "
                    "heute noch einen lebenden Spieler zur Hinrichtung nominieren. "
                    f"Nötig sind mindestens {threshold} Ja-Stimmen. {self._block_text()} "
                    f"Heute sind noch {cfg.max_nominations - count} Nominierungen möglich."
                    + (" Das ist deine letzte Gelegenheit heute." if final
                       else " Du kommst später noch einmal an die Reihe, falls du jetzt verzichtest.")
                    + f" Wähle „{NOBODY}“, um nicht zu nominieren.",
                    obj(
                        nominiert=pick(candidates + [NOBODY], f"Wen du nominierst, oder „{NOBODY}“."),
                        begruendung=text("Deine öffentliche Begründung (ein bis drei Sätze; leer, wenn du nicht nominierst)."),
                    ),
                    fallback=lambda: {"notiz": "", "nominiert": NOBODY, "begruendung": ""},
                ))
                if data["nominiert"] == NOBODY:
                    continue
                count += 1
                self._nomination(p, self.players[self.seat_of(data["nominiert"])],
                                 clip(data["begruendung"], 600), threshold)
            if count >= cfg.max_nominations:
                break

    def _nomination(self, by: Player, target: Player, reason: str, threshold: int) -> None:
        self.nominators.add(by.seat)
        self.nominated.add(target.seat)
        self.log.add("nomination", PUBLIC, by=by.seat, target=target.seat, reason=reason)
        if target.seat != by.seat:
            data = self.ask_one(Request(
                target.seat, "defense",
                f"{by.name} hat dich zur Hinrichtung nominiert. Verteidige dich vor der "
                "ganzen Runde, bevor abgestimmt wird.",
                obj(rede=text("Deine öffentliche Verteidigung.")),
                fallback=lambda: {"notiz": "", "rede": ""},
            ))
            self.log.add("speech", PUBLIC, seat=target.seat, text=clip(data["rede"], 900), context="defense")

        voters = []
        for seat in self._order((target.seat + 1) % self.n):
            v = self.players[seat]
            if (v.alive and seat != self.blackmailed) or (not v.alive and v.ghost_vote):
                voters.append(v)
        yes_voters: list[int] = []

        def vote_request(v: Player, position: int | None) -> Request:
            if position is None:
                progress = "Alle stimmen gleichzeitig ab."
            else:
                progress = (
                    f"Bisher {len(yes_voters)} Ja-Stimmen; nach dir stimmen noch "
                    f"{len(voters) - position - 1} Spieler ab."
                )
            ghost = (
                "" if v.alive else
                " Du bist tot und hast noch deine Geisterstimme: Stimmst du jetzt mit Ja, "
                "ist sie für den Rest des Spiels verbraucht."
            )
            subject = "deine eigene Hinrichtung" if v.seat == target.seat else f"die Hinrichtung von {target.name}"
            return Request(
                v.seat, "vote",
                f"Abstimmung über {subject} (nominiert von {by.name}). Nötig sind "
                f"mindestens {threshold} Ja-Stimmen. {self._block_text()} {progress}{ghost} "
                "Stimmst du für die Hinrichtung?",
                obj(
                    stimme={"type": "boolean", "description": "ja = für die Hinrichtung, nein = dagegen."},
                    kommentar=text("Optional: ein kurzer öffentlicher Satz zu deiner Stimme (oder leer)."),
                ),
                fallback=lambda: {"notiz": "", "stimme": False, "kommentar": ""},
            )

        def record(v: Player, data: dict) -> None:
            yes = bool(data["stimme"])
            ghost = not v.alive
            if yes:
                yes_voters.append(v.seat)
                if ghost:
                    v.ghost_vote = False
            self.log.add("vote", PUBLIC, voter=v.seat, target=target.seat, yes=yes,
                         ghost=ghost, comment=clip(data["kommentar"], 200))

        if self.cfg.vote_mode == "parallel":
            answers = self.ask_many([vote_request(v, None) for v in voters])
            for v, data in zip(voters, answers):
                record(v, data)
        else:
            for position, v in enumerate(voters):
                record(v, self.ask_one(vote_request(v, position)))

        yes = len(yes_voters)
        if yes < threshold:
            outcome = "insufficient"
        elif yes > self.block_votes:
            outcome = "block"
            self.block, self.block_votes = target.seat, yes
        elif yes == self.block_votes:
            outcome = "tie"
            self.block = None
        else:
            outcome = "below"
        self.log.add(
            "vote_result", PUBLIC, target=target.seat, by=by.seat, yes=yes, needed=threshold,
            yes_voters=yes_voters, outcome=outcome, block=self.block, block_votes=self.block_votes,
        )

    def _execution(self) -> None:
        if self.block is None:
            self.log.add("execution", PUBLIC, executed=None, votes=0)
            return
        p = self.players[self.block]
        had_ability = self.has_ability(p)
        self._die(p, "execution")
        self.log.add("execution", PUBLIC, executed=p.seat, votes=self.block_votes)
        if not had_ability:
            return
        if p.role.id == "pechvogel":
            self.double_kill = True
            self.log.add("st_note", SPECTATOR,
                         text=f"{p.name} war der Pechvogel: Der Unhold darf in der nächsten Nacht zweimal töten.")
        elif p.role.id == "jaeger":
            self._hunter_shot(p, night=False)

    # --------------------------------------------------------------------- Ablauf

    def run(self) -> str | None:
        error = None
        try:
            if not self.players:
                self.setup()
            self.round = 1
            while True:
                self.run_night()
                self.run_day()
                if self.winner:
                    break
                if self.round >= self.cfg.max_days:
                    self.end_reason = f"Nach Tag {self.cfg.max_days} ist kein Team am Ziel: unentschieden."
                    break
                self.round += 1
        except BudgetExceeded as exc:
            self.end_reason = f"Abgebrochen: {exc}."
            error = "budget"
        except (GameAborted, LLMError) as exc:
            self.end_reason = f"Abgebrochen: {exc}"
            error = "aborted"
        except KeyboardInterrupt:
            self.end_reason = "Abgebrochen durch den Benutzer."
            error = "interrupted"
        except Exception as exc:
            self.end_reason = f"Abgebrochen wegen eines Programmfehlers: {exc!r}"
            error = "error"
            raise
        finally:
            self._finish(error)
        return self.winner

    def _finish(self, error: str | None) -> None:
        if not self.players:
            return
        if self.winner:
            summary = f"Team {self.winner} gewinnt. {self.end_reason}"
        else:
            summary = self.end_reason or "Abgebrochen."
        self.log.phase = "end"
        self.log.add(
            "game_end", PUBLIC, winner=self.winner, reason=self.end_reason, summary=summary,
            aborted=error,
            roles=[
                {"seat": p.seat, "name": p.name, "role": p.role.name, "typ": p.role.typ,
                 "team": p.role.team, "alive": p.alive, "death": p.death,
                 "death_round": p.death_round}
                for p in self.players
            ],
            bluffs=[r.name for r in self.bluffs],
            stats={
                "calls": self.calls, "cost_usd": round(self.cost, 4),
                "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "fallbacks": self.fallbacks, "rounds": self.round,
                "seconds": round(time.time() - self._started, 1),
            },
        )
