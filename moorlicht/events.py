"""Append-only Ereignisprotokoll mit Sichtbarkeiten.

Jedes Ereignis trägt ein Feld ``vis``:

* ``"public"``     – alle Spieler und Zuschauer sehen es
* ``[sitz, ...]``  – nur diese Spieler (und Zuschauer)
* ``"spectator"``  – nur Zuschauer (Erzähler-Wissen, Technik)

``EventLog.view(sitz)`` ist die EINZIGE Stelle, an der entschieden wird, was ein Spieler
sieht. Alle Spieler-Prompts werden ausschließlich aus dieser Sicht gebaut.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Iterable

PUBLIC = "public"
SPECTATOR = "spectator"


def visible_to(event: dict, seat: int) -> bool:
    vis = event["vis"]
    if vis == PUBLIC:
        return True
    if isinstance(vis, list):
        return seat in vis
    return False


class EventLog:
    def __init__(self, path: str | None = None):
        self.events: list[dict] = []
        self.path = path
        self._lock = threading.Lock()
        self._fh = open(path, "a", encoding="utf-8", newline="\n") if path else None
        self.round = 0
        self.phase = "setup"
        self.listeners: list = []  # z. B. die Konsolen-Mitschrift

    def add(self, type_: str, vis: Any, **data: Any) -> dict:
        if vis not in (PUBLIC, SPECTATOR):
            if not isinstance(vis, (list, tuple, set)) or not all(isinstance(s, int) for s in vis):
                raise ValueError(f"ungültige Sichtbarkeit: {vis!r}")
            vis = sorted(set(vis))
        with self._lock:
            event = {
                "i": len(self.events),
                "ts": round(time.time(), 3),
                "round": self.round,
                "phase": self.phase,
                "type": type_,
                "vis": vis,
                "data": data,
            }
            self.events.append(event)
            if self._fh:
                self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                self._fh.flush()
            for listener in list(self.listeners):
                try:
                    listener(event)
                except Exception:
                    # Eine kaputte Mitschrift (z. B. geschlossene Konsole) darf die
                    # Partie nicht abbrechen.
                    self.listeners.remove(listener)
        return event

    def view(self, seat: int) -> list[dict]:
        """Alles, was der Spieler auf diesem Platz sehen darf – und nichts sonst."""
        with self._lock:
            snapshot = list(self.events)
        return [e for e in snapshot if visible_to(e, seat)]

    def close(self) -> None:
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None


def load_events(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def of_type(events: Iterable[dict], *types: str) -> list[dict]:
    return [e for e in events if e["type"] in types]
