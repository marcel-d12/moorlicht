"""Tests für die Moorlicht-Engine – laufen ohne LLM und ohne Kosten.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from moorlicht import prompts  # noqa: E402
from moorlicht.agents import (  # noqa: E402
    ANSWER_SCHEMA, HeuristicStoryteller, LLMBrain, MockBrain, answer_format, to_spec,
)
from moorlicht.engine import Config, Game, match_pairs  # noqa: E402
from moorlicht.events import EventLog, visible_to  # noqa: E402
from moorlicht.llm import ClaudeCLIBackend, LLMResult  # noqa: E402
from moorlicht.roles import (  # noqa: E402
    DISTRIBUTION, DOERFLER, DORF, EIGENBROETLER, MOOR, ROLES, SCHERGE, UNHOLD,
)

TOKEN = re.compile(r"«[^«»]+»")
MARK = re.compile(r"⟦#(\d+)⟧")


def mock_game(seed: int, n: int = 8, error_rate: float = 0.0, record: bool = False, **cfg):
    config = Config(n_players=n, seed=seed, concurrency=1, max_calls=None, **cfg)
    brain = MockBrain(seed, error_rate=error_rate, record=record)
    log = EventLog()
    game = Game(config, brain, HeuristicStoryteller(seed), log)
    game.run()
    return game, log, brain


class ScriptedBrain:
    """Antwortet über eine Policy-Funktion; alles Unbestimmte ist passiv."""

    def __init__(self):
        self.policy = lambda seat, kind, schema, user: None
        self.asked: list[tuple[int, str]] = []

    def decide(self, seat, kind, system, user, schema, delta=None):
        self.asked.append((seat, kind))
        data = {}
        for key, spec in schema["properties"].items():
            if "enum" in spec:
                data[key] = "niemand" if "niemand" in spec["enum"] else spec["enum"][0]
            elif spec.get("type") == "boolean":
                data[key] = False
            else:
                data[key] = "x"
        if kind == "speech":
            data["rede"] = ""
        data.update(self.policy(seat, kind, schema, user) or {})
        return LLMResult(data=data, model="scripted")


def scripted_game(roles: list[str], seed: int = 1, lie: float = 1.0, **cfg):
    config = Config(n_players=len(roles), seed=seed, roles=roles, concurrency=1,
                    max_calls=None, **cfg)
    brain = ScriptedBrain()
    log = EventLog()
    game = Game(config, brain, HeuristicStoryteller(seed, lie_probability=lie), log)
    return game, log, brain


def events(log, *types):
    return [e for e in log.events if e["type"] in types]


# ---------------------------------------------------------------- Invarianten

class Replay:
    """Spielt das öffentliche Protokoll unabhängig von der Engine nach."""

    def __init__(self, test: unittest.TestCase, game: Game, log: EventLog):
        self.t, self.game, self.log = test, game, log

    def check(self) -> None:
        t, game, log = self.t, self.game, self.log
        n = game.n
        role = {p.seat: p.role for p in game.players}
        alive = set(range(n))
        ghost_used: set[int] = set()
        max_noms = game.cfg.max_nominations
        day_state = None
        current = None
        ends = events(log, "game_end")
        t.assertEqual(len(ends), 1)
        t.assertIs(log.events[-1], ends[0])

        for e in log.events:
            d, typ = e["data"], e["type"]
            vis = e["vis"]
            t.assertTrue(vis in ("public", "spectator") or
                         (isinstance(vis, list) and vis and all(0 <= s < n for s in vis)))
            if typ == "dawn":
                if e["round"] == 1:
                    t.assertEqual(d["deaths"], [], "in Nacht 1 stirbt niemand")
                t.assertEqual(len(set(d["deaths"])), len(d["deaths"]))
                for s in d["deaths"]:
                    t.assertIn(s, alive, "niemand stirbt zweimal")
                    alive.discard(s)
                day_state = {"nominators": set(), "nominated": set(), "block": None,
                             "best": 0, "count": 0, "blackmailed": None}
            elif typ == "private_note" and "erpresst" in d["text"]:
                day_state["blackmailed"] = d["seat"]
            elif typ == "nominations_open":
                t.assertEqual(d["alive"], len(alive))
                t.assertEqual(d["threshold"], math.ceil(len(alive) / 2))
            elif typ == "nomination":
                by, target = d["by"], d["target"]
                t.assertIn(by, alive, "nur Lebende nominieren")
                t.assertIn(target, alive, "nur Lebende werden nominiert")
                t.assertNotIn(by, day_state["nominators"], "eine Nominierung pro Tag")
                t.assertNotIn(target, day_state["nominated"], "einmal pro Tag nominiert")
                t.assertNotEqual(by, day_state["blackmailed"], "Erpresste nominieren nicht")
                day_state["nominators"].add(by)
                day_state["nominated"].add(target)
                day_state["count"] += 1
                t.assertLessEqual(day_state["count"], max_noms)
                current = {"target": target, "voters": [], "yes": []}
            elif typ == "vote":
                v = d["voter"]
                t.assertEqual(d["target"], current["target"])
                t.assertNotIn(v, current["voters"], "eine Stimme pro Abstimmung")
                t.assertNotEqual(v, day_state["blackmailed"], "Erpresste stimmen nicht ab")
                t.assertEqual(d["ghost"], v not in alive)
                current["voters"].append(v)
                if v not in alive:
                    t.assertNotIn(v, ghost_used, "Geisterstimme nur einmal")
                if d["yes"]:
                    current["yes"].append(v)
                    if v not in alive:
                        ghost_used.add(v)
            elif typ == "vote_result":
                yes = len(current["yes"])
                needed = math.ceil(len(alive) / 2)
                t.assertEqual(d["yes"], yes)
                t.assertEqual(d["yes_voters"], current["yes"])
                t.assertEqual(d["needed"], needed)
                # Reihenfolge: im Uhrzeigersinn ab dem Platz nach dem Nominierten
                order = [(current["target"] + 1 + k) % n for k in range(n)]
                t.assertEqual(current["voters"], [s for s in order if s in current["voters"]])
                if yes >= needed:
                    if yes > day_state["best"]:
                        day_state["block"], day_state["best"] = current["target"], yes
                    elif yes == day_state["best"]:
                        day_state["block"] = None
                t.assertEqual(d["block"], day_state["block"])
            elif typ == "execution":
                t.assertEqual(d["executed"], day_state["block"])
                if d["executed"] is not None:
                    t.assertIn(d["executed"], alive)
                    alive.discard(d["executed"])
            elif typ == "shot":
                t.assertEqual(role[d["by"]].id, "jaeger")
                t.assertNotIn(d["by"], alive)
                t.assertIn(d["target"], alive)
                alive.discard(d["target"])

        t.assertEqual(alive, {p.seat for p in game.players if p.alive},
                      "öffentliches Protokoll und Engine-Zustand stimmen überein")
        end = ends[0]["data"]
        unhold_alive = game.unhold().alive
        if end["winner"] == DORF:
            t.assertFalse(unhold_alive)
        elif end["winner"] == MOOR:
            t.assertTrue(unhold_alive)
            t.assertLessEqual(len(alive), 2)
        else:
            t.assertIsNone(end["aborted"])
            t.assertEqual(game.round, game.cfg.max_days)
            t.assertTrue(unhold_alive)
            t.assertGreater(len(alive), 2)


class InvariantTests(unittest.TestCase):
    def test_setup_distribution(self):
        for n in DISTRIBUTION:
            for seed in range(40):
                game = Game(Config(n_players=n, seed=seed), MockBrain(seed),
                            HeuristicStoryteller(seed), EventLog())
                game.setup()
                roles = [p.role for p in game.players]
                self.assertEqual(len(set(roles)), n)
                got = tuple(sum(1 for r in roles if r.typ == t)
                            for t in (DOERFLER, EIGENBROETLER, SCHERGE, UNHOLD))
                self.assertEqual(got, DISTRIBUTION[n])
                self.assertEqual(len(game.bluffs), 3)
                for bluff in game.bluffs:
                    self.assertEqual(bluff.typ, DOERFLER)
                    self.assertNotIn(bluff, roles)

    def test_many_seeds(self):
        winners = {DORF: 0, MOOR: 0, None: 0}
        seen_roles = set()
        for n in DISTRIBUTION:
            for seed in range(120):
                game, log, _ = mock_game(seed, n=n, error_rate=0.04)
                with self.subTest(n=n, seed=seed):
                    Replay(self, game, log).check()
                winners[game.winner] += 1
                seen_roles |= {p.role.id for p in game.players}
        self.assertEqual(seen_roles, {r.id for r in ROLES}, "jede Rolle kam vor")
        self.assertGreater(winners[DORF], 0)
        self.assertGreater(winners[MOOR], 0)

    def test_parallel_votes_and_threads(self):
        for seed in range(15):
            config = Config(seed=seed, concurrency=4, vote_mode="parallel", max_calls=None)
            log = EventLog()
            game = Game(config, MockBrain(seed), HeuristicStoryteller(seed), log)
            game.run()
            with self.subTest(seed=seed):
                Replay(self, game, log).check()

    def test_deterministic(self):
        def strip(log):
            # Zeitstempel und Laufzeiten sind naturgemäß nicht reproduzierbar.
            out = []
            for e in log.events:
                if e["type"] == "usage":
                    continue
                e = {k: v for k, v in e.items() if k != "ts"}
                if e["type"] == "game_end":
                    stats = {k: v for k, v in e["data"]["stats"].items() if k != "seconds"}
                    e["data"] = {**e["data"], "stats": stats}
                out.append(e)
            return out
        _, a, _ = mock_game(11)
        _, b, _ = mock_game(11)
        self.assertEqual(strip(a), strip(b))

    def test_budget_stops_game(self):
        config = Config(seed=5, concurrency=1, max_calls=30)
        log = EventLog()
        game = Game(config, MockBrain(5), HeuristicStoryteller(5), log)
        game.run()
        end = log.events[-1]
        self.assertEqual(end["type"], "game_end")
        self.assertEqual(end["data"]["aborted"], "budget")
        self.assertLessEqual(game.calls, 30)

    def test_dead_backend_aborts(self):
        class Dead:
            def decide(self, *a, **k):
                return LLMResult(error="kaputt")
        log = EventLog()
        game = Game(Config(seed=1, concurrency=1), Dead(), HeuristicStoryteller(1), log)
        game.run()
        self.assertEqual(log.events[-1]["data"]["aborted"], "aborted")
        self.assertLessEqual(game.calls, 8)


# -------------------------------------------------------------------- Geheimnisse

class LeakTests(unittest.TestCase):
    def test_prompts_contain_only_visible_events(self):
        for seed in range(25):
            game, log, brain = mock_game(seed, record=True, mark_prompts=True)
            by_token: dict[str, list[dict]] = {}
            for e in log.events:
                for token in TOKEN.findall(str(e["data"])):
                    by_token.setdefault(token, []).append(e)
            self.assertTrue(brain.prompts)
            for seat, kind, system, user in brain.prompts:
                # 1) Jede gerenderte Zeile stammt aus einem für den Platz sichtbaren Ereignis.
                for i in MARK.findall(user):
                    self.assertTrue(visible_to(log.events[int(i)], seat),
                                    f"Platz {seat} sieht Ereignis {i}: {log.events[int(i)]}")
                # 2) Jeder Freitext-Schnipsel ist über ein sichtbares Ereignis dorthin gelangt.
                for token in TOKEN.findall(user):
                    sources = by_token.get(token, [])
                    self.assertTrue(any(visible_to(e, seat) for e in sources),
                                    f"Platz {seat} sieht fremden Text {token}")
                self.assertFalse(MARK.search(system) or TOKEN.search(system))
            # Dasselbe gilt für die Folgeprompts fortlaufender Sitzungen.
            self.assertEqual(len(brain.deltas), len(brain.prompts))
            for seat, kind, delta in brain.deltas:
                for i in MARK.findall(delta):
                    self.assertTrue(visible_to(log.events[int(i)], seat))
                for token in TOKEN.findall(delta):
                    self.assertTrue(any(visible_to(e, seat) for e in by_token.get(token, [])),
                                    f"Platz {seat} sieht fremden Text {token} im Folgeprompt")

    def test_deltas_cover_history_exactly_once(self):
        # Alle Folgeprompts eines Spielers zusammen enthalten jedes für ihn sichtbare
        # Ereignis genau einmal (bis auf seine eigenen Notizen) – nichts fehlt, nichts doppelt.
        game, log, brain = mock_game(6, record=True, mark_prompts=True)
        for p in game.players:
            seen: list[int] = []
            last_full = ""
            for (seat, kind, system, user), (_, _, delta) in zip(brain.prompts, brain.deltas):
                if seat != p.seat or "Deine vorige Antwort war ungültig" in delta:
                    continue
                seen += [int(i) for i in MARK.findall(delta)]
                last_full = user
            self.assertEqual(len(seen), len(set(seen)))
            self.assertEqual(seen, sorted(seen))
            expected = [int(i) for i in MARK.findall(last_full)
                        if log.events[int(i)]["type"] != "note"]
            self.assertEqual(seen, expected)
            self.assertTrue(all(visible_to(log.events[i], p.seat) for i in seen))

    def test_private_text_stays_private(self):
        game, log, brain = mock_game(4, record=True)
        whispers = events(log, "whisper")
        notes = events(log, "note")
        self.assertTrue(whispers and notes)
        for (seat, kind, system, user), (_, _, delta) in zip(brain.prompts, brain.deltas):
            for e in whispers:
                if seat not in e["vis"]:
                    self.assertNotIn(e["data"]["text"], user + delta)
            for e in notes:
                if e["data"]["seat"] != seat:
                    self.assertNotIn(e["data"]["text"], user + delta)

    def test_view_filter(self):
        game, log, _ = mock_game(2)
        for p in game.players:
            view = log.view(p.seat)
            for e in view:
                self.assertTrue(e["vis"] == "public" or p.seat in e["vis"])
            types = {e["type"] for e in view}
            self.assertFalse(types & {"grimoire", "st_note", "st_decision", "usage", "meta", "fallback"})
            own_roles = [e for e in view if e["type"] == "role"]
            self.assertEqual([e["data"]["seat"] for e in own_roles], [p.seat])
            knows_evil = any(e["type"] == "evil_info" for e in view)
            self.assertEqual(knows_evil, p.role.team == MOOR)

    def test_system_prompt_is_public_only(self):
        # Gleiche Tischrunde, andere Rollenverteilung -> identischer Systemprompt.
        a = Game(Config(seed=1), MockBrain(1), HeuristicStoryteller(1), EventLog())
        b = Game(Config(seed=2), MockBrain(2), HeuristicStoryteller(2), EventLog())
        a.setup()
        b.setup()
        self.assertNotEqual([p.role for p in a.players], [p.role for p in b.players])
        for seat in range(8):
            self.assertEqual(a._system[seat], b._system[seat])

    def test_tasks_do_not_name_foreign_roles(self):
        names = {r.name for r in ROLES}
        for seed in range(25):
            game, log, brain = mock_game(seed, record=True)
            for seat, kind, system, user in brain.prompts:
                task = user.split("# Deine Aufgabe jetzt", 1)[1]
                status = user.split("# Aktueller Stand", 1)[1].split("# Deine Aufgabe jetzt", 1)[0]
                own = game.players[seat].role
                allowed = {own.name}
                if own.typ == UNHOLD:
                    allowed.add("Pechvogel")
                mentioned = {nm for nm in names if nm in task}
                self.assertLessEqual(mentioned, allowed, f"Aufgabe für {own.name}: {task}")
                self.assertFalse({nm for nm in names if nm in status})


# ---------------------------------------------------------------------- Mechanik

class MechanicsTests(unittest.TestCase):
    def test_match_pairs(self):
        order = [0, 1, 2, 3, 4, 5]
        self.assertEqual(match_pairs({0: 1, 1: 0, 2: 3, 3: 2, 4: None, 5: None}, order), [(0, 1), (2, 3)])
        # 0 und 2 wollen beide 1; 1 will 2 -> gegenseitig gibt es nicht, 0 kommt zuerst dran.
        self.assertEqual(match_pairs({0: 1, 1: 2, 2: 1, 3: None, 4: None, 5: None}, order), [(1, 2)])
        self.assertEqual(match_pairs({0: 1, 1: 3, 2: 1, 3: 5, 4: None, 5: 0}, order), [(0, 1), (3, 5)])
        # Wer mit niemandem reden will, wird nicht angesprochen.
        self.assertEqual(match_pairs({0: 1, 1: None, 2: 0, 3: None, 4: None, 5: None}, order), [(2, 0)])
        for pairs in (match_pairs({i: (i * 7 + 3) % 6 for i in order}, order),):
            flat = [s for pair in pairs for s in pair]
            self.assertEqual(len(flat), len(set(flat)))

    def test_protection_armor_tracking(self):
        roles = ["seherin", "faehrtenleser", "nachtwaechter", "heilerin", "schmiedin",
                 "mueller", "verleumder", "irrlicht"]
        game, log, brain = scripted_game(roles, max_days=4)
        game.setup()
        R = {p.role.id: p for p in game.players}
        nm = lambda rid: R[rid].name
        plan = {
            1: {"seherin": "mueller", "verleumder": "heilerin", "faehrtenleser": "verleumder",
                "nachtwaechter": "heilerin"},
            2: {"seherin": "irrlicht", "verleumder": "heilerin", "faehrtenleser": "irrlicht",
                "nachtwaechter": "schmiedin", "heilerin": "seherin", "irrlicht": "schmiedin"},
            3: {"seherin": "verleumder", "verleumder": "nachtwaechter", "faehrtenleser": "heilerin",
                "nachtwaechter": "nachtwaechter", "heilerin": "faehrtenleser", "irrlicht": "faehrtenleser"},
            4: {"seherin": "nachtwaechter", "verleumder": "seherin", "faehrtenleser": "seherin",
                "nachtwaechter": "seherin", "heilerin": "seherin", "irrlicht": "schmiedin"},
        }

        def policy(seat, kind, schema, user):
            if kind == "night":
                rid = game.players[seat].role.id
                return {"ziel": nm(plan[game.round][rid])}
        brain.policy = policy
        game.round = 1
        infos = {}
        for rnd in range(1, 5):
            game.round = rnd
            game.run_night()
            infos[rnd] = {game.players[e["data"]["seat"]].role.id: e["data"]["text"]
                          for e in events(log, "night_info") if e["round"] == rnd}
            game.run_day()
        dawns = [e["data"]["deaths"] for e in events(log, "dawn")]
        self.assertEqual(dawns, [[], [], [], [R["schmiedin"].seat]])
        self.assertTrue(R["schmiedin"].armor_used)
        self.assertFalse(R["schmiedin"].alive)
        # Nacht 1
        self.assertIn("„Moor“", infos[1]["seherin"])                      # Müller erscheint als Moor
        self.assertIn(f"hat in dieser Nacht {nm('heilerin')} besucht", infos[1]["faehrtenleser"])
        self.assertTrue(infos[1]["nachtwaechter"].endswith(": 1."))        # nur der Verleumder
        # Nacht 2
        self.assertIn("„Dorf“", infos[2]["seherin"])                      # Unhold erscheint als Dorf
        self.assertIn(f"hat in dieser Nacht {nm('schmiedin')} besucht", infos[2]["faehrtenleser"])
        self.assertTrue(infos[2]["nachtwaechter"].endswith(": 1."))        # nur das Irrlicht
        # Nacht 3
        self.assertIn("„Moor“", infos[3]["seherin"])                      # der Scherge
        self.assertIn(f"hat in dieser Nacht {nm('faehrtenleser')} besucht", infos[3]["faehrtenleser"])
        self.assertTrue(infos[3]["nachtwaechter"].endswith(": 1."))        # sich selbst beobachtet: der Verleumder kam
        # Nacht 4: Verleumdung gilt nur für eine Nacht; die Seherin prüft den Nachtwächter.
        self.assertIn("„Dorf“", infos[4]["seherin"])
        self.assertIn(f"hat in dieser Nacht {nm('nachtwaechter')} besucht", infos[4]["faehrtenleser"])
        self.assertTrue(infos[4]["nachtwaechter"].endswith(": 3."))        # Verleumder, Fährtenleser, Heilerin
        # Die Heilerin durfte nie zweimal hintereinander denselben schützen.
        heal = [e["data"]["text"] for e in events(log, "night_choice") if e["data"]["seat"] == R["heilerin"].seat]
        self.assertEqual(len(heal), 3)

    def test_healer_cannot_repeat(self):
        roles = ["seherin", "faehrtenleser", "nachtwaechter", "heilerin", "schmiedin",
                 "mueller", "verleumder", "irrlicht"]
        game, log, brain = scripted_game(roles)
        game.setup()
        healer = game.by_role("heilerin")
        healer.last_target = (healer.seat + 1) % 8
        game.round = 2
        req = game._night_request(healer, first=False, double=False)
        self.assertNotIn(game.name(healer.last_target), req.schema["properties"]["ziel"]["enum"])
        self.assertNotIn(healer.name, req.schema["properties"]["ziel"]["enum"])
        self.assertIsNone(game._night_request(healer, first=True, double=False))

    def test_fog_pechvogel_hunter(self):
        roles = ["seherin", "rutengaengerin", "laternentraeger", "archivarin", "jaeger",
                 "pechvogel", "nebelkraehe", "nachtmahr"]
        game, log, brain = scripted_game(roles, lie=1.0)
        game.setup()
        R = {p.role.id: p for p in game.players}
        nm = lambda rid: R[rid].name
        night = {
            1: {"nachtmahr": {"ziel": nm("seherin")}, "nebelkraehe": {"ziel": nm("rutengaengerin")},
                "seherin": {"ziel": nm("nebelkraehe")}, "rutengaengerin": {"ziel": nm("rutengaengerin")}},
            2: {"nachtmahr": {"ziel": nm("jaeger"), "ziel2": nm("archivarin")},
                "nebelkraehe": {"ziel": nm("laternentraeger")},
                "seherin": {"ziel": nm("nachtmahr")}, "rutengaengerin": {"ziel": nm("jaeger")}},
        }

        def policy(seat, kind, schema, user):
            rid = game.players[seat].role.id
            if kind == "night":
                return night[game.round][rid]
            if kind == "nominate" and rid == "seherin" and game.round == 1:
                return {"nominiert": nm("pechvogel"), "begruendung": "Test"}
            if kind == "vote":
                return {"stimme": True}
            if kind == "shot":
                return {"ziel": nm("nachtmahr")}
        brain.policy = policy
        winner = game.run()
        self.assertEqual(winner, DORF)
        info1 = {game.players[e["data"]["seat"]].role.id: e["data"]["text"]
                 for e in events(log, "night_info") if e["round"] == 1}
        self.assertIn("„Dorf“", info1["seherin"])  # im Nebel belogen: die Nebelkrähe ist Moor
        true_count = sum(1 for s in (-1, 0, 1)
                         if game.players[(R["rutengaengerin"].seat + s) % 8].role.team == MOOR)
        self.assertNotIn(f"Anzahl „Moor“: {true_count}.", info1["rutengaengerin"])
        dist = (R["nachtmahr"].seat - R["laternentraeger"].seat) % 8
        if dist != 4:
            expected = "näher im Uhrzeigersinn" if dist < 4 else "näher gegen den Uhrzeigersinn"
            self.assertIn(expected, info1["laternentraeger"])
        in_play = {p.role.name for p in game.players}
        named = info1["archivarin"].split(": ", 1)[1].rstrip(".").split(" und ")
        self.assertEqual(len(named), 2)
        self.assertFalse(set(named) & in_play)
        execution = events(log, "execution")[0]["data"]
        self.assertEqual(execution["executed"], R["pechvogel"].seat)
        dawn2 = events(log, "dawn")[1]["data"]["deaths"]
        self.assertEqual(set(dawn2), {R["jaeger"].seat, R["archivarin"].seat, R["nachtmahr"].seat})
        self.assertTrue(R["seherin"].perma_fog)
        Replay(self, game, log).check()

    def test_fogged_abilities_fail(self):
        roles = ["seherin", "heilerin", "schmiedin", "jaeger", "archivarin",
                 "pechvogel", "nebelkraehe", "nachtmahr"]
        game, log, brain = scripted_game(roles)
        game.setup()
        R = {p.role.id: p for p in game.players}
        nm = lambda rid: R[rid].name
        night = {
            1: {"nachtmahr": nm("schmiedin"), "nebelkraehe": nm("seherin"), "seherin": nm("heilerin")},
            # Heilerin im Nebel schützt den Jäger; der Jäger stirbt trotzdem.
            2: {"nachtmahr": nm("jaeger"), "nebelkraehe": nm("heilerin"), "heilerin": nm("jaeger"),
                "seherin": nm("heilerin")},
            # Schmiedin steht dauerhaft im Nebel: keine Rüstung.
            3: {"nachtmahr": nm("schmiedin"), "nebelkraehe": nm("seherin"), "heilerin": nm("seherin"),
                "seherin": nm("heilerin")},
        }

        def policy(seat, kind, schema, user):
            if kind == "night":
                return {"ziel": night[game.round][game.players[seat].role.id]}
            if kind == "shot":
                return {"ziel": nm("archivarin")}
        brain.policy = policy
        for rnd in (1, 2, 3):
            game.round = rnd
            game.run_night()
            game.run_day()
        dawns = [e["data"]["deaths"] for e in events(log, "dawn")]
        # Schutz im Nebel wirkungslos: Der Jäger stirbt und nimmt (unvernebelt) jemanden mit.
        self.assertEqual(dawns[1], [R["jaeger"].seat, R["archivarin"].seat])
        self.assertIn((R["jaeger"].seat, "shot"), brain.asked)
        self.assertEqual(dawns[2], [R["schmiedin"].seat])    # Rüstung im Nebel wirkungslos
        self.assertFalse(R["schmiedin"].armor_used)
        self.assertIsNone(game.winner)
        # Ein Jäger im Nebel schießt nicht.
        hunter_game, hunter_log, hunter_brain = scripted_game(roles)
        hunter_game.setup()
        H = {p.role.id: p for p in hunter_game.players}

        def hunter_policy(seat, kind, schema, user):
            rid = hunter_game.players[seat].role.id
            if kind == "night" and rid == "nebelkraehe":
                return {"ziel": H["jaeger"].name if hunter_game.round == 2 else H["seherin"].name}
            if kind == "night" and rid == "nachtmahr":
                return {"ziel": H["jaeger"].name}
            if kind == "night" and rid == "heilerin":
                return {"ziel": H["seherin"].name}
        hunter_brain.policy = hunter_policy
        for rnd in (1, 2):
            hunter_game.round = rnd
            hunter_game.run_night()
            hunter_game.run_day()
        self.assertEqual(events(hunter_log, "dawn")[1]["data"]["deaths"], [H["jaeger"].seat])
        self.assertNotIn((H["jaeger"].seat, "shot"), hunter_brain.asked)

    def test_blackmail_tie_ghost_vote_and_evil_win(self):
        roles = ["faehrtenleser", "nachtwaechter", "archivarin", "laternentraeger",
                 "menschenkenner", "schlafwandler", "erpresser", "irrlicht"]
        game, log, brain = scripted_game(roles, max_days=7)
        game.setup()
        R = {p.role.id: p for p in game.players}
        nm = lambda rid: R[rid].name
        kills = {2: "menschenkenner", 3: "faehrtenleser", 4: "archivarin", 5: "laternentraeger",
                 6: "schlafwandler"}
        yes_votes = {
            # (Tag, Nominierter) -> Rollen, die mit Ja stimmen
            (1, "archivarin"): {"nachtwaechter", "laternentraeger", "schlafwandler", "irrlicht"},
            (1, "laternentraeger"): {"archivarin", "nachtwaechter", "erpresser", "irrlicht"},
            (2, "nachtwaechter"): {"menschenkenner", "laternentraeger", "archivarin", "irrlicht"},
            (2, "laternentraeger"): {"nachtwaechter"},
        }
        noms = {(1, "nachtwaechter"): "archivarin", (1, "archivarin"): "laternentraeger",
                (2, "laternentraeger"): "nachtwaechter", (2, "nachtwaechter"): "laternentraeger"}
        current = {}
        asked = []

        def policy(seat, kind, schema, user):
            rid = game.players[seat].role.id
            asked.append((game.round, rid, kind))
            if kind == "night":
                if rid == "erpresser":
                    return {"ziel": nm("faehrtenleser") if game.round == 1 else nm("schlafwandler")}
                if rid == "menschenkenner":
                    return {"ziel": nm("irrlicht"), "rolle": "Irrlicht"}
                if rid == "irrlicht":
                    return {"ziel": nm(kills[game.round])}
                options = schema["properties"]["ziel"]["enum"]
                return {"ziel": options[0]}
            if kind == "nominate":
                target = noms.get((game.round, rid))
                if target and nm(target) in schema["properties"]["nominiert"]["enum"]:
                    current["target"] = target
                    return {"nominiert": nm(target), "begruendung": "Test"}
            if kind == "vote":
                return {"stimme": rid in yes_votes.get((game.round, current.get("target")), set())}
        brain.policy = policy
        winner = game.run()
        self.assertEqual(winner, MOOR)
        Replay(self, game, log).check()

        info = [e["data"]["text"] for e in events(log, "night_info")
                if e["data"]["seat"] == R["menschenkenner"].seat]
        self.assertEqual(len(info), 1)
        self.assertTrue(info[0].endswith("– Ja."))
        self.assertEqual(sum(1 for _, rid, k in asked if rid == "menschenkenner" and k == "night"), 1)

        # Der Erpresste redet mit, wird aber weder nach Nominierung noch nach Stimme gefragt.
        day1 = [e for e in log.events if e["round"] == 1 and e["phase"] == "day"]
        victim = R["faehrtenleser"].seat
        self.assertFalse([e for e in day1 if e["type"] == "vote" and e["data"]["voter"] == victim])
        self.assertIn((1, "faehrtenleser", "speech"), asked)
        self.assertNotIn((1, "faehrtenleser", "nominate"), asked)
        self.assertNotIn((1, "faehrtenleser", "vote"), asked)
        self.assertIn((2, "faehrtenleser", "vote"), asked)
        self.assertNotIn((2, "schlafwandler", "vote"), asked)
        results = [e["data"] for e in day1 if e["type"] == "vote_result"]
        self.assertEqual([r["outcome"] for r in results], ["block", "tie"])
        executions = [e["data"]["executed"] for e in events(log, "execution")]
        self.assertIsNone(executions[0])                       # Gleichstand -> keine Hinrichtung
        self.assertEqual(executions[1], R["nachtwaechter"].seat)

        ghost = R["menschenkenner"].seat
        ghost_votes = [e["data"] for e in events(log, "vote") if e["data"]["voter"] == ghost and e["round"] >= 2]
        self.assertEqual([v["yes"] for v in ghost_votes].count(True), 1)
        self.assertTrue(ghost_votes[-1]["yes"])                # nach dem Ja nicht mehr gefragt
        self.assertTrue(all(v["ghost"] for v in ghost_votes))
        self.assertFalse(R["menschenkenner"].ghost_vote)

    def test_appears_moor(self):
        roles = ["seherin", "rutengaengerin", "heilerin", "schmiedin", "jaeger",
                 "mueller", "verleumder", "irrlicht"]
        game, _, _ = scripted_game(roles)
        game.setup()
        R = {p.role.id: p for p in game.players}
        self.assertFalse(game.appears_moor(R["irrlicht"], for_seherin=True))
        self.assertTrue(game.appears_moor(R["irrlicht"], for_seherin=False))
        self.assertTrue(game.appears_moor(R["verleumder"], for_seherin=True))
        self.assertTrue(game.appears_moor(R["mueller"], for_seherin=True))
        self.assertFalse(game.appears_moor(R["jaeger"], for_seherin=True))
        game.framed.add(R["jaeger"].seat)
        self.assertTrue(game.appears_moor(R["jaeger"], for_seherin=False))
        R["mueller"].fogged = True
        self.assertFalse(game.appears_moor(R["mueller"], for_seherin=True))


class PromptTests(unittest.TestCase):
    def test_prompt_renders(self):
        game, log, brain = mock_game(9, record=True)
        seat, kind, system, user = brain.prompts[-1]
        self.assertIn("# Regeln von „Moorlicht“", system)
        self.assertIn("Deine Rolle:", user)
        self.assertIn("# Aktueller Stand", user)
        for p in game.players:
            text = prompts.build_user_prompt(log.view(p.seat), p.seat, "Test")
            self.assertIn(f"Deine Rolle: {p.role.artikel} {p.role.name}", text)
            self.assertNotIn("None", text)


class AnswerFormatTests(unittest.TestCase):
    """Übersetzung zwischen dem festen Antwortformat der Agenten und den genauen Schemata."""

    def collect_schemas(self):
        schemas = []
        mock = MockBrain(5)

        class Spy:
            def decide(self, seat, kind, system, user, schema, delta=None):
                schemas.append((kind, schema))
                return mock.decide(seat, kind, system, user, schema)
        game = Game(Config(seed=5, concurrency=1, max_calls=None), Spy(), HeuristicStoryteller(5), EventLog())
        game.run()
        return schemas

    def test_every_request_fits_the_fixed_format(self):
        kinds = set()
        for kind, schema in self.collect_schemas():
            kinds.add(kind)
            block, mapping = answer_format(schema)
            self.assertTrue(block.startswith("# Antwortformat"))
            self.assertEqual(set(mapping.values()) | {"notiz"}, set(schema["properties"]))
            self.assertLessEqual(set(mapping), {"wahl", "wahl2", "text"})
            for slot in ("wahl", "wahl2", "text", "notiz"):
                self.assertIn(f"- {slot}: ", block)
        self.assertLessEqual({"night", "whisper_choice", "whisper_reply", "speech", "nominate", "vote"}, kinds)
        self.assertEqual(ANSWER_SCHEMA["required"], ["wahl", "wahl2", "text", "notiz"])

    def test_to_spec(self):
        vote = {"properties": {"stimme": {"type": "boolean", "description": "ja/nein"},
                               "kommentar": {"type": "string"}, "notiz": {"type": "string"}}}
        block, mapping = answer_format(vote)
        self.assertEqual(mapping, {"wahl": "stimme", "text": "kommentar"})
        self.assertIn("- wahl: genau einer dieser Werte: ja | nein", block)
        self.assertIn("- wahl2: leer lassen", block)
        got = to_spec({"wahl": " Ja. ", "wahl2": "", "text": " ok ", "notiz": "n"}, vote, mapping)
        self.assertEqual(got, {"stimme": True, "kommentar": "ok", "notiz": "n"})
        self.assertIs(to_spec({"wahl": "NEIN", "wahl2": "", "text": "", "notiz": ""}, vote, mapping)["stimme"], False)
        self.assertEqual(to_spec({"wahl": "vielleicht", "text": ""}, vote, mapping)["stimme"], "vielleicht")

        night = {"properties": {"ziel": {"type": "string", "enum": ["Anna", "Bruno", "niemand"]},
                                "rolle": {"type": "string", "enum": ["Seherin", "Jäger"]},
                                "notiz": {"type": "string"}}}
        block, mapping = answer_format(night)
        self.assertEqual(mapping, {"wahl": "ziel", "wahl2": "rolle"})
        self.assertIn("Anna | Bruno | niemand", block)
        self.assertIn("- text: leer lassen", block)
        got = to_spec({"wahl": "„bruno“", "wahl2": "JÄGER", "text": "", "notiz": ""}, night, mapping)
        self.assertEqual((got["ziel"], got["rolle"]), ("Bruno", "Jäger"))
        self.assertEqual(to_spec({"wahl": "Zorro", "wahl2": "Jäger"}, night, mapping)["ziel"], "Zorro")

    def test_llm_brain_translates_both_ways(self):
        seen = {}

        class Backend:
            def complete(self, system, user, schema, session=None, delta=None):
                seen.update(user=user, schema=schema, session=session, delta=delta)
                return LLMResult(data={"wahl": "bruno", "wahl2": "", "text": "Weil er lügt.", "notiz": "merken"})
        spec = {"properties": {"nominiert": {"type": "string", "enum": ["Anna", "Bruno", "niemand"], "description": "Wen"},
                               "begruendung": {"type": "string", "description": "Warum"},
                               "notiz": {"type": "string"}}}
        res = LLMBrain(Backend()).decide(3, "nominate", "SYS", "VERLAUF", spec, delta="NEU")
        self.assertEqual(res.data, {"nominiert": "Bruno", "begruendung": "Weil er lügt.", "notiz": "merken"})
        self.assertIs(seen["schema"], ANSWER_SCHEMA)
        self.assertEqual(seen["session"], "platz-4")
        self.assertTrue(seen["user"].startswith("VERLAUF") and "# Antwortformat" in seen["user"])
        self.assertTrue(seen["delta"].startswith("NEU") and "Anna | Bruno | niemand" in seen["delta"])


class CLIBackendTests(unittest.TestCase):
    """Das CLI-Backend gegen eine Attrappe von ``claude`` (tests/fake_claude.py)."""

    SCHEMA = {"type": "object", "properties": {"ziel": {"type": "string", "enum": ["Anna", "Bruno"]},
                                                "notiz": {"type": "string"}},
              "required": ["ziel", "notiz"], "additionalProperties": False}

    def backend(self, **kw):
        fake = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_claude.py")
        backend = ClaudeCLIBackend(exe=[sys.executable, fake], timeout=60, **kw)
        self.addCleanup(backend.close)
        return backend

    def calls(self, backend):
        with open(os.path.join(backend.workdir, "calls.jsonl"), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]

    def test_isolation_flags(self):
        backend = self.backend()
        res = backend.complete("SYSTEM", "Hallo", self.SCHEMA, session="platz-1", delta="Delta")
        self.assertEqual(res.data["ziel"], "Anna")
        call = self.calls(backend)[0]
        args = call["args"]
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(args[args.index("--setting-sources") + 1], "")
        for flag in ("-p", "--strict-mcp-config", "--safe-mode", "--disable-slash-commands"):
            self.assertIn(flag, args)
        self.assertEqual(call["system"], "SYSTEM")
        project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertNotIn(os.path.normcase(project), os.path.normcase(call["cwd"]))

    def test_sessions_send_full_then_delta(self):
        backend = self.backend()
        a1 = backend.complete("S", "VOLL-1", self.SCHEMA, session="platz-1", delta="DELTA-1")
        a2 = backend.complete("S", "VOLL-2", self.SCHEMA, session="platz-1", delta="DELTA-2")
        b1 = backend.complete("S", "VOLL-B", self.SCHEMA, session="platz-2", delta="DELTA-B")
        a3 = backend.complete("S", "VOLL-3", self.SCHEMA, session="platz-1", delta="DELTA-3")
        calls = self.calls(backend)
        self.assertEqual([c["mode"] for c in calls], ["new", "resume", "new", "resume"])
        self.assertEqual([c["prompt"] for c in calls], ["VOLL-1", "DELTA-2", "VOLL-B", "DELTA-3"])
        sid = lambda c: c["args"][c["args"].index("--session-id" if c["mode"] == "new" else "--resume") + 1]
        self.assertEqual(sid(calls[0]), sid(calls[1]))
        self.assertEqual(sid(calls[0]), sid(calls[3]))
        self.assertNotEqual(sid(calls[0]), sid(calls[2]))
        # claude meldet in einer Sitzung die aufgelaufene Summe; verbucht wird die Differenz.
        for res in (a1, a2, b1, a3):
            self.assertAlmostEqual(res.cost_usd, 0.01)
        self.assertEqual([r.extra["session"] for r in (a1, a2, b1, a3)],
                         ["neu", "fortgesetzt", "neu", "fortgesetzt"])

    def test_error_starts_fresh_session(self):
        backend = self.backend()
        backend.complete("S", "VOLL-1", self.SCHEMA, session="p", delta="DELTA-1")
        bad = backend.complete("S", "VOLL-2", self.SCHEMA, session="p", delta="CRASH")
        self.assertIsNone(bad.data)
        self.assertFalse(bad.flagged)
        ok = backend.complete("S", "VOLL-3", self.SCHEMA, session="p", delta="DELTA-3")
        self.assertIsNotNone(ok.data)
        calls = self.calls(backend)
        self.assertEqual([c["mode"] for c in calls], ["new", "resume", "new"])
        self.assertEqual(calls[2]["prompt"], "VOLL-3")

    def test_safeguard_retry_and_fallback_model(self):
        backend = self.backend(fallback_model="claude-opus-5-5")
        once = backend.complete("S", "VOLL FLAG_ONCE", self.SCHEMA, session="p", delta="x")
        self.assertIsNotNone(once.data)
        self.assertEqual(once.model, "claude-fable-5-2")
        self.assertEqual(once.extra["safeguard_retries"], 1)
        self.assertNotIn("fallback_model", once.extra)
        always = backend.complete("S", "VOLL FLAG_ALWAYS", self.SCHEMA, session="q", delta="x")
        self.assertIsNotNone(always.data)
        self.assertEqual(always.model, "claude-opus-5-5")
        self.assertTrue(always.extra["fallback_model"])
        self.assertEqual(always.data["notiz"], "notiz von claude-opus-5-5")
        models = [c["model"] for c in self.calls(backend)]
        self.assertEqual(models, ["claude-fable-5-2"] * 4 + ["claude-opus-5-5"])
        # Danach beginnt der Spieler wieder eine frische Sitzung auf dem Hauptmodell.
        after = backend.complete("S", "VOLL-NEU", self.SCHEMA, session="q", delta="DELTA")
        self.assertEqual(after.model, "claude-fable-5-2")
        self.assertEqual(self.calls(backend)[-1]["prompt"], "VOLL-NEU")

    def test_no_fallback_model(self):
        backend = self.backend(fallback_model=None)
        res = backend.complete("S", "FLAG_ALWAYS", self.SCHEMA, session="p", delta="x")
        self.assertIsNone(res.data)
        self.assertTrue(res.flagged)
        self.assertEqual(len(self.calls(backend)), 2)

    def test_stateless_mode(self):
        backend = self.backend(sessions=False)
        backend.complete("S", "VOLL-1", self.SCHEMA, session="p", delta="DELTA-1")
        backend.complete("S", "VOLL-2", self.SCHEMA, session="p", delta="DELTA-2")
        calls = self.calls(backend)
        self.assertEqual([c["mode"] for c in calls], ["stateless", "stateless"])
        self.assertEqual([c["prompt"] for c in calls], ["VOLL-1", "VOLL-2"])
        self.assertTrue(all("--no-session-persistence" in c["args"] for c in calls))

    def test_game_runs_on_cli_backend(self):
        # Eine ganze (kurze) Partie über das CLI-Backend mit der Attrappe.
        from moorlicht.agents import LLMStoryteller
        backend = self.backend()
        log = EventLog()
        config = Config(seed=9, concurrency=3, max_days=1, whisper_rounds=(1,), discussion_rounds=1,
                        nomination_passes=1, max_nominations=1, max_calls=None)
        heuristic = HeuristicStoryteller(9)
        game = Game(config, LLMBrain(backend), LLMStoryteller(backend, heuristic), log)
        game.run()
        end = log.events[-1]
        self.assertEqual(end["type"], "game_end")
        self.assertIsNone(end["data"]["aborted"])
        self.assertEqual(game.fallbacks, 0)
        usage = events(log, "usage")
        self.assertTrue(usage)
        self.assertTrue(all(u["data"]["error"] is None for u in usage))
        by_seat = {}
        for call in self.calls(backend):
            if call["mode"] != "stateless":
                by_seat.setdefault(call["system"], []).append(call["mode"])
        self.assertEqual(len(by_seat), 8)
        for modes in by_seat.values():
            self.assertEqual(modes[0], "new")
            self.assertTrue(all(m == "resume" for m in modes[1:]))
        backend.close()
        self.assertFalse(os.path.exists(backend.workdir))


if __name__ == "__main__":
    unittest.main()
