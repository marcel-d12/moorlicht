"""Konsolen-Mitschrift aus Zuschauersicht (alles sichtbar, in der dritten Person)."""

from __future__ import annotations

from .prompts import join_names, quote


class Narrator:
    def __init__(self, show_notes: bool = True, stream=None):
        self.names: list[str] = []
        self.roles: dict[int, str] = {}
        self.show_notes = show_notes
        self.stream = stream

    def tag(self, seat: int) -> str:
        role = self.roles.get(seat)
        return f"{self.names[seat]} [{role}]" if role else self.names[seat]

    def line(self, e: dict) -> str | None:
        t, d = e["type"], e["data"]
        nm = lambda s: self.names[s]
        if t == "meta":
            return f"Moorlicht · Backend {d['backend']} · Modell {d['model']} · Erzähler {d['storyteller']}"
        if t == "game_start":
            self.names = [p["name"] for p in d["players"]]
            return f"Spielstart (Seed {d['seed']}): " + ", ".join(
                f"{p['seat'] + 1} {p['name']}" for p in d["players"])
        if t == "role":
            self.roles[d["seat"]] = d["role"]["name"]
            return f"  {nm(d['seat'])} ist {d['role']['artikel']} {d['role']['name']} ({d['role']['typ']})"
        if t == "evil_info":
            return "  Tarnrollen des Moor-Teams: " + join_names(d["bluffs"])
        if t == "phase":
            label = "Nacht" if d["kind"] == "night" else "Tag"
            return f"\n{'=' * 22} {label} {d['n']} {'=' * 22}"
        if t == "night_choice":
            return f"  · {self.tag(d['seat'])}: {d['text']}"
        if t == "night_info":
            return f"  · Erzähler → {self.tag(d['seat'])}: {d['text']}"
        if t == "private_note":
            return f"  · (nur für {self.tag(d['seat'])}) {d['text']}"
        if t == "st_note":
            return f"  ✎ {d['text']}"
        if t == "st_decision":
            return f"  ✎ Erzähler entscheidet: {d['options'][d['chosen']]} – {d['reason']}"
        if t == "dawn":
            dead = [nm(s) for s in d["deaths"]]
            return "Morgengrauen: " + (f"gestorben: {join_names(dead)}." if dead else "niemand ist gestorben.")
        if t == "whisper_pairs":
            pairs = ", ".join(f"{nm(a)} ↔ {nm(b)}" for a, b in d["pairs"]) or "keine Gespräche"
            return f"\n— Flüsterrunde {d['round']}: {pairs}"
        if t == "whisper":
            return f"  (flüstert) {self.tag(d['from'])} → {nm(d['to'])}: {quote(d['text'])}"
        if t == "discussion_start":
            return f"\n— Öffentliche Diskussion, Runde {d['round']}"
        if t == "speech":
            if not d["text"].strip():
                return f"{self.tag(d['seat'])} sagt nichts."
            verb = "verteidigt sich" if d.get("context") == "defense" else "sagt"
            return f"{self.tag(d['seat'])} {verb}: {quote(d['text'])}"
        if t == "nominations_open":
            return f"\n— Nominierungen (nötig: {d['threshold']} Ja-Stimmen)"
        if t == "nomination":
            return f"\n▶ {self.tag(d['by'])} nominiert {self.tag(d['target'])}: {quote(d['reason'])}"
        if t == "vote":
            ghost = " (Geisterstimme)" if d["ghost"] and d["yes"] else ""
            comment = f" – {quote(d['comment'])}" if d["comment"].strip() else ""
            return f"    {self.tag(d['voter'])}: {'JA' if d['yes'] else 'nein'}{ghost}{comment}"
        if t == "vote_result":
            block = nm(d["block"]) if d["block"] is not None else "niemand"
            return f"  ⇒ {d['yes']} Ja (nötig {d['needed']}) · Richtblock: {block}"
        if t == "execution":
            if d["executed"] is None:
                return "\n✝ Keine Hinrichtung heute."
            return f"\n✝ {self.tag(d['executed'])} wird hingerichtet."
        if t == "shot":
            return f"✝ {self.tag(d['by'])} (Jäger) erschießt {self.tag(d['target'])}."
        if t in ("note", "thought") and self.show_notes:
            return f"      ✍ {nm(d['seat'])} notiert: {d['text']}"
        if t == "fallback":
            who = nm(d["seat"]) if d["seat"] is not None else "Erzähler"
            return f"  ⚠ Rückfallaktion für {who} ({d['kind']}): {d['error']}"
        if t == "game_end":
            s = d["stats"]
            reveal = ", ".join(f"{r['name']}={r['role']}" for r in d["roles"])
            return (
                f"\n{'#' * 60}\nSpielende: {d['summary']}\nRollen: {reveal}\n"
                f"LLM-Aufrufe: {s['calls']} · Tokens ein/aus: {s['tokens_in']}/{s['tokens_out']} · "
                f"Kosten laut Backend: {s['cost_usd']:.2f} USD · Dauer: {s['seconds']:.0f}s"
            )
        return None

    def __call__(self, e: dict) -> None:
        text = self.line(e)
        if text is not None:
            print(text, file=self.stream, flush=True)
