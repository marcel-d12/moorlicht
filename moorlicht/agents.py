"""Die „Gehirne“ der Spieler und der Erzähler.

* ``LLMBrain``   – jede Entscheidung ist ein strukturierter LLM-Aufruf.
* ``MockBrain``  – zufällige, formal gültige Antworten (kostenlos; für Tests und zum
  Ausprobieren der Oberfläche).
* ``HeuristicStoryteller`` / ``LLMStoryteller`` – treffen die wenigen Entscheidungen,
  die die Regeln dem Erzähler überlassen. Die Engine gibt die erlaubten Optionen vor
  und prüft die Antwort; der Erzähler kann also nichts Regelwidriges tun.
"""

from __future__ import annotations

import random
import threading

from .llm import LLMResult


# Ein einziges, immer gleiches Antwortschema für alle Spielerentscheidungen. Das Schema
# wird dem Modell als Werkzeugdefinition ganz am Anfang des Prompts mitgegeben; bliebe es
# nicht konstant, wäre bei jedem Aufruf der Prompt-Cache der ganzen Sitzung verloren.
# Welche Werte gerade erlaubt sind, steht deshalb im Abschnitt „Antwortformat“ des
# Prompts, und die Engine prüft die Antwort gegen das genaue Schema der Entscheidung.
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "wahl": {
            "type": "string",
            "description": "Deine Wahl: genau einer der unter „Antwortformat“ genannten "
                           "Werte – oder leer, wenn dort keine Wahl verlangt ist.",
        },
        "wahl2": {
            "type": "string",
            "description": "Zweite Wahl, nur wenn das Antwortformat sie verlangt; sonst leer.",
        },
        "text": {
            "type": "string",
            "description": "Was du sagst oder flüsterst, falls das Antwortformat es "
                           "verlangt; sonst leer.",
        },
        "notiz": {
            "type": "string",
            "description": "Kurze Merknotiz für dich selbst (ein bis drei Sätze).",
        },
    },
    "required": ["wahl", "wahl2", "text", "notiz"],
    "additionalProperties": False,
}

_YES = {"ja", "yes", "true"}
_NO = {"nein", "no", "false"}
_TRIM = " \t\n„“”\"'.!"


def answer_format(schema: dict) -> tuple[str, dict[str, str]]:
    """Übersetzt das genaue Schema einer Entscheidung in den Prompt-Abschnitt
    „Antwortformat“ und liefert die Zuordnung der festen Antwortfelder
    (wahl, wahl2, text) zu den Feldern dieses Schemas."""
    choices, texts = [], []
    for key, spec in schema["properties"].items():
        if key == "notiz":
            continue
        (choices if "enum" in spec or spec.get("type") == "boolean" else texts).append(key)
    if len(choices) > 2 or len(texts) > 1:
        raise ValueError(f"Schema passt nicht in das feste Antwortformat: {list(schema['properties'])}")
    mapping = dict(zip(("wahl", "wahl2"), choices))
    if texts:
        mapping["text"] = texts[0]
    lines = ["# Antwortformat"]
    for slot in ("wahl", "wahl2", "text"):
        key = mapping.get(slot)
        if key is None:
            lines.append(f"- {slot}: leer lassen")
            continue
        spec = schema["properties"][key]
        description = spec.get("description", "").strip()
        if "enum" in spec:
            lines.append(f"- {slot}: genau einer dieser Werte: {' | '.join(spec['enum'])} – {description}")
        elif spec.get("type") == "boolean":
            lines.append(f"- {slot}: genau einer dieser Werte: ja | nein – {description}")
        else:
            lines.append(f"- {slot}: {description}")
    lines.append("- notiz: kurze Merknotiz für dich selbst (ein bis drei Sätze)")
    return "\n".join(lines), mapping


def to_spec(answer: dict, schema: dict, mapping: dict[str, str]) -> dict:
    """Antwort im festen Format zurück in die Felder des genauen Schemas. Schreibweisen
    werden großzügig erkannt; was nicht passt, bleibt stehen und fällt in der Prüfung
    der Engine auf."""
    data: dict = {}
    for slot, key in mapping.items():
        raw = answer.get(slot)
        spec = schema["properties"][key]
        if not isinstance(raw, str):
            data[key] = raw
            continue
        value = raw.strip()
        folded = value.strip(_TRIM).casefold()
        if "enum" in spec:
            data[key] = next((o for o in spec["enum"] if o.casefold() == folded), value)
        elif spec.get("type") == "boolean":
            data[key] = True if folded in _YES else False if folded in _NO else value
        else:
            data[key] = value
    if "notiz" in schema["properties"]:
        data["notiz"] = answer.get("notiz")
    return data


class LLMBrain:
    def __init__(self, backend):
        self.backend = backend

    def decide(self, seat: int, kind: str, system: str, user: str, schema: dict,
               delta: str | None = None) -> LLMResult:
        block, mapping = answer_format(schema)
        # Jeder Platz hat seine eigene fortlaufende Sitzung (sofern das Backend das kann).
        res = self.backend.complete(
            system, f"{user}\n\n{block}", ANSWER_SCHEMA, session=f"platz-{seat + 1}",
            delta=None if delta is None else f"{delta}\n\n{block}",
        )
        if isinstance(res.data, dict):
            res.data = to_spec(res.data, schema, mapping)
        return res


class MockBrain:
    """Antwortet zufällig, aber schema-konform. Mit ``error_rate`` > 0 streut es
    absichtlich ungültige Antworten ein, um Wiederholung und Rückfall zu testen."""

    def __init__(self, seed: int = 0, error_rate: float = 0.0, record: bool = False):
        self.rng = random.Random(seed)
        self.error_rate = error_rate
        self.record = record
        self.prompts: list[tuple[int, str, str, str]] = []
        self.deltas: list[tuple[int, str, str]] = []
        self.counter = 0
        self._lock = threading.Lock()

    def decide(self, seat: int, kind: str, system: str, user: str, schema: dict,
               delta: str | None = None) -> LLMResult:
        with self._lock:
            self.counter += 1
            n = self.counter
            if self.record:
                self.prompts.append((seat, kind, system, user))
                self.deltas.append((seat, kind, delta or ""))
            rng = self.rng
            data: dict = {}
            for key, spec in schema["properties"].items():
                if "enum" in spec:
                    options = spec["enum"]
                    real = [o for o in options if o != "niemand"]
                    if "niemand" in options and (
                        not real
                        or (kind == "nominate" and rng.random() < 0.6)
                        or (kind == "whisper_choice" and rng.random() < 0.15)
                        or (kind == "night" and rng.random() < 0.5)
                    ):
                        data[key] = "niemand"
                    else:
                        data[key] = rng.choice(real)
                elif spec.get("type") == "boolean":
                    data[key] = rng.random() < 0.5
                else:
                    # Eindeutige Marke: so lässt sich prüfen, wer welchen Text zu sehen bekommt.
                    data[key] = f"«{key}:{seat}:{n}»"
            if kind == "speech" and rng.random() < 0.2:
                data["rede"] = ""
            if self.error_rate and rng.random() < self.error_rate:
                broken = rng.choice(["missing", "enum", "none"])
                if broken == "none":
                    return LLMResult(error="simulierter Ausfall", model="mock")
                key = rng.choice(list(data))
                if broken == "missing":
                    del data[key]
                else:
                    data[key] = "«ungültig»" if isinstance(data[key], str) else "ja"
        return LLMResult(data=data, model="mock")


# ------------------------------------------------------------------------ Erzähler

class HeuristicStoryteller:
    """Kostenloser Erzähler: Im Nebel meist eine falsche, sonst eine zufällige Option."""

    name = "heuristic"

    def __init__(self, seed: int = 0, lie_probability: float = 0.85):
        self.rng = random.Random(seed ^ 0x5EED)
        self.lie_probability = lie_probability

    def choose(self, context: str, question: str, options: list[str], truth: int | None):
        if truth is None or len(options) == 1:
            return self.rng.randrange(len(options)), "zufällig gewählt", None
        if self.rng.random() < self.lie_probability:
            wrong = [i for i in range(len(options)) if i != truth]
            return self.rng.choice(wrong), "im Nebel: falsche Information", None
        return truth, "im Nebel: ausnahmsweise die Wahrheit", None


ST_SYSTEM = """Du bist der Erzähler (Spielleiter) einer Partie „Moorlicht“, eines \
Deduktionsspiels mit verborgenen Rollen. Du kennst alle Rollen und bist neutral: Dein \
Ziel ist eine spannende, faire Partie, die möglichst bis zum letzten Tag offen bleibt.

Die Regeln überlassen dir nur wenige Entscheidungen – vor allem, welche Information ein \
Spieler erhält, der im Nebel steht (seine Information darf falsch sein). Dir wird die \
Lage, die Frage und eine nummerierte Liste erlaubter Optionen gezeigt. Wähle genau eine.

Leitlinien: Eine Falschinformation im Nebel soll glaubwürdig sein und zu dem passen, was \
der Spieler bisher erfahren hat, damit er den Nebel nicht sofort bemerkt. In der Regel \
gibst du im Nebel etwas Falsches; die Wahrheit nur, wenn sie das Spiel interessanter \
macht. Hilf im Zweifel dem Team, das gerade im Nachteil ist."""


class LLMStoryteller:
    name = "llm"

    def __init__(self, backend, fallback: HeuristicStoryteller):
        self.backend = backend
        self.fallback = fallback

    def choose(self, context: str, question: str, options: list[str], truth: int | None):
        numbered = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
        truth_line = (
            f"Wahre Antwort: Option {truth + 1} ({options[truth]})."
            if truth is not None
            else "Hier gibt es keine „wahre“ Antwort; die Regeln lassen dir die Wahl."
        )
        user = (
            f"# Lage (vollständiges Erzähler-Wissen)\n{context}\n\n# Entscheidung\n{question}\n"
            f"{truth_line}\n\n# Optionen\n{numbered}"
        )
        schema = {
            "type": "object",
            "properties": {
                "begruendung": {"type": "string", "description": "Ein bis zwei Sätze."},
                "option": {
                    "type": "string",
                    "enum": [str(i + 1) for i in range(len(options))],
                    "description": "Nummer der gewählten Option.",
                },
            },
            "required": ["begruendung", "option"],
            "additionalProperties": False,
        }
        res = self.backend.complete(ST_SYSTEM, user, schema)
        try:
            index = int(res.data["option"]) - 1
            if not 0 <= index < len(options):
                raise ValueError
            return index, str(res.data.get("begruendung", "")).strip(), res
        except (TypeError, KeyError, ValueError):
            index, reason, _ = self.fallback.choose(context, question, options, truth)
            return index, f"{reason} (Rückfall, LLM-Erzähler: {res.error or 'ungültig'})", res
