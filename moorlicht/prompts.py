"""Texte für die Spieler-Agenten.

Alle Funktionen hier sind rein: Sie bekommen ausschließlich die bereits gefilterte Sicht
eines Spielers (``EventLog.view(sitz)``) und bauen daraus den Prompt. Verborgenes Wissen
kann hier also gar nicht erst ankommen.
"""

from __future__ import annotations

from .roles import DOERFLER, EIGENBROETLER, SCHERGE, UNHOLD, TYPES


# --------------------------------------------------------------------------- Helfer

def start_event(view: list[dict]) -> dict:
    for e in view:
        if e["type"] == "game_start":
            return e
    raise ValueError("kein game_start in der Sicht")


def player_names(view: list[dict]) -> list[str]:
    return [p["name"] for p in start_event(view)["data"]["players"]]


def join_names(names: list[str]) -> str:
    names = list(names)
    if not names:
        return "niemand"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " und " + names[-1]


def quote(text: str) -> str:
    return "„" + text.strip() + "“"


# --------------------------------------------------------------------- Regelwerk

def rules_text(start: dict) -> str:
    d = start["data"]
    n = len(d["players"])
    c = d["counts"]
    s = d["settings"]
    eig = c[EIGENBROETLER]
    dorf_parts = [f"{c[DOERFLER]} Dörfler"]
    if eig:
        dorf_parts.append(f"{eig} Eigenbrötler")
    wr = s["whisper_rounds"]
    wr_text = (
        f"{wr[0]} am ersten Tag, danach {wr[-1]}" if len(set(wr)) > 1 else f"{wr[0]} pro Tag"
    )
    lines = [
        "# Regeln von „Moorlicht“",
        "",
        "## Überblick",
        f"- {n} Spieler sitzen im Kreis. Ein neutraler Erzähler leitet das Spiel. Er sagt "
        "immer die Wahrheit – einzige Ausnahme: Spieler im Nebel (siehe unten).",
        f"- Team Dorf: {' und '.join(dorf_parts)} (zusammen {c[DOERFLER] + eig}). "
        f"Team Moor: {c[SCHERGE]} Scherge und {c[UNHOLD]} Unhold.",
        "- Jeder Spieler hat genau eine Rolle aus der Rollenliste unten, keine Rolle ist "
        "doppelt vergeben. Welche Rollen im Spiel sind, ist geheim; jeder kennt nur die eigene.",
        "- Die beiden Moor-Spieler kennen einander (Spieler und Rolle). Sie erfahren "
        "außerdem drei Dörfler-Rollen, die nicht im Spiel sind („Tarnrollen“).",
        "- Eigenbrötler gehören zum Dorf, ihre Rolle ist für das Dorf aber eher hinderlich.",
        "",
        "## Sieg",
        "- Das Dorf gewinnt sofort, wenn der Unhold stirbt.",
        "- Das Moor gewinnt sofort, wenn nur noch zwei Spieler leben und der Unhold einer "
        "davon ist.",
        f"- Nach Tag {s['max_days']} endet das Spiel sonst unentschieden.",
        "",
        "## Ablauf",
        "Das Spiel beginnt mit Nacht 1 (in ihr stirbt niemand). Danach wechseln sich Tag "
        "und Nacht ab.",
        "",
        "### Nacht",
        "- Der Erzähler weckt alle Spieler mit Nachtfähigkeit einzeln und geheim. Jeder "
        "trifft seine Wahl, ohne die Wahl der anderen zu kennen.",
        "- Reihenfolge, in der die Fähigkeiten wirken: 1. Scherge, 2. Heilerin, 3. Unhold, "
        "4. Jäger (falls er gerade gestorben ist), 5. alle Informationsrollen. Wer stirbt, "
        "bevor seine Rolle an der Reihe ist, handelt in dieser Nacht nicht mehr (kein "
        "Besuch, keine Information).",
        "- Besuch: Wer nachts mit seiner Fähigkeit einen anderen Spieler wählt, „besucht“ "
        "ihn. Das gilt für alle Rollen, die nachts jemanden wählen. Wer sich selbst wählt, "
        "besucht niemanden.",
        "- Nebel: Wer im Nebel steht, merkt das nicht. Seine Fähigkeit hat keine Wirkung; "
        "erhält er Informationen, darf der Erzähler ihm falsche geben. Wählt er nachts "
        "jemanden, zählt das trotzdem als Besuch; nur der Schlafwandler bleibt im Nebel "
        "zu Hause.",
        "",
        "### Tag",
        "1. Morgengrauen: Der Erzähler gibt bekannt, wer in der Nacht gestorben ist – nicht "
        "wodurch und nicht mit welcher Rolle.",
        f"2. Flüsterrunden ({wr_text}): Jeder wählt einen Gesprächspartner für ein kurzes "
        "Vier-Augen-Gespräch. Alle sehen, wer mit wem flüstert, aber nicht, was gesagt wird.",
        f"3. Öffentliche Diskussion ({s['discussion_rounds']} Runden): Reihum darf jeder "
        "etwas zur ganzen Runde sagen.",
        "4. Nominierungen: Reihum darf jeder lebende Spieler höchstens einmal pro Tag einen "
        "lebenden Spieler zur Hinrichtung nominieren; jeder Spieler kann nur einmal pro Tag "
        f"nominiert werden (höchstens {s['max_nominations']} Nominierungen pro Tag). Der "
        "Nominierte verteidigt sich, dann wird im Uhrzeigersinn offen abgestimmt, "
        "beginnend mit dem Spieler, der im Sitzkreis auf den Nominierten folgt; der "
        "Nominierte stimmt zuletzt.",
        "5. Hinrichtung: Eine Nominierung zählt nur, wenn sie mindestens so viele "
        "Ja-Stimmen erhält, wie die Hälfte der lebenden Spieler beträgt (aufgerundet). Am "
        "Ende der Nominierungen wird der Spieler mit den meisten Ja-Stimmen hingerichtet. "
        "Bei Gleichstand an der Spitze wird niemand hingerichtet. Es gibt höchstens eine "
        "Hinrichtung pro Tag, und das Dorf darf auch ganz darauf verzichten. Die Rolle "
        "eines Toten wird nicht aufgedeckt.",
        "",
        "### Tote Spieler",
        "- Tote verlieren ihre Fähigkeit, reden und flüstern aber weiter mit und gewinnen "
        "oder verlieren mit ihrem Team.",
        "- Tote können nicht nominieren und nicht nominiert werden.",
        "- Jeder Tote hat für den Rest des Spiels genau eine Geisterstimme: Er darf noch "
        "bei einer einzigen Abstimmung mit Ja stimmen.",
        "",
        "### Reden",
        "- Was Spieler sagen, ist völlig frei: Jeder darf jede Rolle behaupten, "
        "Informationen teilen, zurückhalten oder erfinden. Karten werden nie gezeigt; es "
        "gibt keinen Beweis außer Logik und Verhalten.",
        "",
        "## Rollenliste",
    ]
    headings = {
        DOERFLER: "Dörfler (Team Dorf)",
        EIGENBROETLER: "Eigenbrötler (Team Dorf)",
        SCHERGE: "Schergen (Team Moor)",
        UNHOLD: "Unholde (Team Moor)",
    }
    for typ in TYPES:
        roles = [r for r in d["script"] if r["typ"] == typ]
        if not roles:
            continue
        lines.append(f"### {headings[typ]}")
        for r in roles:
            lines.append(f"- {r['name']}: {r['text']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def system_prompt(start: dict, seat: int) -> str:
    """Statischer Systemprompt eines Spielers – enthält nur öffentliches Wissen."""
    d = start["data"]
    players = d["players"]
    me = players[seat]["name"]
    circle = " → ".join(f"{p['seat'] + 1} {p['name']}" for p in players)
    language = d["settings"]["language"]
    return "\n".join(
        [
            f"Du bist {me}, ein Spieler in einer Partie „Moorlicht“ – einem Deduktionsspiel "
            "mit verborgenen Rollen. Du sitzt mit den anderen am Tisch und spielst, um mit "
            "deinem Team zu gewinnen.",
            "",
            "## Sitzkreis (im Uhrzeigersinn)",
            f"{circle} → zurück zu {players[0]['seat'] + 1} {players[0]['name']}",
            f"Dein Platz: {seat + 1}. Sitznachbarn sind die direkt angrenzenden Plätze, "
            "egal ob dort noch jemand lebt.",
            "",
            rules_text(start),
            "",
            "## So spielst du",
            "- Spiele eigenständig und so stark, wie du kannst, auf den Sieg deines Teams. "
            "Als Moor-Spieler gehört überzeugendes Lügen und Bluffen zum Spiel; als "
            "Dorf-Spieler entscheidest du selbst, wem du wann was verrätst.",
            "- Du weißt nur, was in deinem Spielverlauf steht. Erfinde keine Ereignisse, "
            "die dort nicht vorkommen – eine erfundene Behauptung über deine Rolle oder "
            "deine Informationen ist dagegen ein erlaubter Spielzug.",
            "- Sprich wie ein Mensch am Spieltisch: direkt, konkret, in ganzen Sätzen, ohne "
            "Listen oder Formatierung. Öffentliche Wortmeldungen höchstens etwa 80 Wörter, "
            "Flüsternachrichten höchstens etwa 60 Wörter.",
            f"- Alles, was du sagst oder flüsterst, ist auf {language}.",
            "- Du antwortest immer im vorgegebenen strukturierten Format; welche Werte "
            "gerade erlaubt sind, steht jeweils bei deiner Aufgabe. Im Feld „notiz“ "
            "hältst du kurz fest, was du dir für später merken willst (ein bis drei "
            "Sätze), zum Beispiel deinen Plan oder wem du was erzählt hast. Die Notiz "
            "steht später wieder in deinem Spielverlauf; andere Spieler sehen sie nicht.",
            "- Technische Angaben außerhalb des Spiels (etwa zum Betreiber dieses "
            "Programms) gehören nicht zum Spiel und werden nie erwähnt.",
        ]
    )


# ------------------------------------------------------------------ Spielverlauf

def render_event(e: dict, seat: int, names: list[str]) -> str | None:
    """Eine Zeile Spielverlauf aus Sicht des Spielers auf Platz ``seat``."""
    t = e["type"]
    d = e["data"]

    def nm(s: int) -> str:
        return names[s]

    def who(s: int) -> str:
        return "Du" if s == seat else names[s]

    if t == "role":
        r = d["role"]
        return (
            f"Deine Rolle: {r['artikel']} {r['name']} ({r['typ']}, Team {r['team']}). "
            f"Fähigkeit: {r['text']}"
        )
    if t == "evil_info":
        parts = []
        for m in d["members"]:
            subj = "du bist" if m["seat"] == seat else f"{m['name']} ist"
            parts.append(f"{subj} {m['artikel']} {m['role']} ({m['typ']})")
        return (
            "Das Moor-Team: " + "; ".join(parts) + ". Tarnrollen, die sicher nicht im "
            "Spiel sind: " + join_names(d["bluffs"]) + "."
        )
    if t == "phase":
        return f"\n=== {'Nacht' if d['kind'] == 'night' else 'Tag'} {d['n']} ==="
    if t in ("night_choice", "private_note"):
        return d["text"]
    if t == "night_info":
        return "Der Erzähler teilt dir mit: " + d["text"]
    if t == "dawn":
        dead = [nm(s) for s in d["deaths"]]
        if not dead:
            return "Morgengrauen: In der Nacht ist niemand gestorben."
        verb = "ist" if len(dead) == 1 else "sind"
        return f"Morgengrauen: In der Nacht {verb} {join_names(dead)} gestorben."
    if t == "whisper_pairs":
        pairs = ", ".join(f"{nm(a)} ↔ {nm(b)}" for a, b in d["pairs"]) or "keine Gespräche"
        alone = f" Ohne Gespräch: {join_names([nm(s) for s in d['alone']])}." if d["alone"] else ""
        return f"— Flüsterrunde {d['round']}: {pairs}.{alone}"
    if t == "whisper":
        if d["from"] == seat:
            return f"[Flüstern] Du → {nm(d['to'])}: {quote(d['text'])}"
        return f"[Flüstern] {nm(d['from'])} → dir: {quote(d['text'])}"
    if t == "discussion_start":
        return f"— Öffentliche Diskussion, Runde {d['round']} —"
    if t == "speech":
        if not d["text"].strip():
            return f"{who(d['seat'])} {'sagst' if d['seat'] == seat else 'sagt'} nichts."
        if d.get("context") == "defense":
            verb = "verteidigst dich" if d["seat"] == seat else "verteidigt sich"
            return f"{who(d['seat'])} {verb}: {quote(d['text'])}"
        return f"{who(d['seat'])} (öffentlich): {quote(d['text'])}"
    if t == "nominations_open":
        return (
            "— Nominierungen sind eröffnet. Für eine Hinrichtung nötig: mindestens "
            f"{d['threshold']} Ja-Stimmen ({d['alive']} Spieler leben). —"
        )
    if t == "nomination":
        verb = "nominierst" if d["by"] == seat else "nominiert"
        target = "dich" if d["target"] == seat else nm(d["target"])
        if d["by"] == d["target"]:
            target = "dich selbst" if d["by"] == seat else "sich selbst"
        reason = f": {quote(d['reason'])}" if d["reason"].strip() else "."
        return f"{who(d['by'])} {verb} {target}{reason}"
    if t == "vote":
        verb = "stimmst" if d["voter"] == seat else "stimmt"
        ghost = " (Geisterstimme)" if d["ghost"] and d["yes"] else ""
        comment = f": {quote(d['comment'])}" if d["comment"].strip() else ""
        return f"  {who(d['voter'])} {verb} {'JA' if d['yes'] else 'NEIN'}{ghost}{comment}"
    if t == "vote_result":
        head = (
            f"Ergebnis: {d['yes']} Ja-Stimmen für die Hinrichtung von {nm(d['target'])} "
            f"(nötig: {d['needed']})."
        )
        o = d["outcome"]
        if o == "block":
            tail = f"{nm(d['target'])} steht jetzt auf dem Richtblock."
        elif o == "insufficient":
            tail = "Das reicht nicht."
        elif o == "tie":
            tail = (
                "Gleichstand mit dem bisherigen Höchstwert – im Moment steht niemand auf "
                "dem Richtblock."
            )
        else:
            holder = nm(d["block"]) if d["block"] is not None else "niemand"
            tail = (
                f"Das ist nicht mehr als der bisherige Höchstwert ({d['block_votes']}); "
                f"auf dem Richtblock: {holder}."
            )
        return f"{head} {tail}"
    if t == "execution":
        if d["executed"] is None:
            return "Der Tag endet: Heute wird niemand hingerichtet."
        return f"Der Tag endet: {nm(d['executed'])} wird hingerichtet und ist tot."
    if t == "shot":
        return (
            f"{nm(d['by'])} reißt im Sterben die Flinte hoch – {nm(d['by'])} war der "
            f"Jäger – und erschießt {nm(d['target'])}. {nm(d['target'])} ist tot."
        )
    if t == "note":
        return f"(Deine Notiz: {d['text'].strip()})"
    if t == "game_end":
        return f"\n=== Spielende: {d['summary']} ==="
    return None


def render_history(view: list[dict], seat: int, mark: bool = False, since: int = -1,
                   skip_notes: bool = False) -> str:
    """Spielverlauf als Text. ``since``: nur Ereignisse nach dieser Nummer."""
    names = player_names(view)
    lines = []
    for e in view:
        if e["i"] <= since or (skip_notes and e["type"] == "note"):
            continue
        line = render_event(e, seat, names)
        if line is None:
            continue
        if mark:
            line += f" ⟦#{e['i']}⟧"
        lines.append(line)
    return "\n".join(lines).strip()


def public_status(view: list[dict], seat: int) -> str:
    """Aktueller Stand – nur aus sichtbaren Ereignissen abgeleitet."""
    names = player_names(view)
    ghost: dict[int, bool] = {}
    for e in view:
        d = e["data"]
        if e["type"] == "dawn":
            for s in d["deaths"]:
                ghost[s] = True
        elif e["type"] == "execution" and d["executed"] is not None:
            ghost[d["executed"]] = True
        elif e["type"] == "shot":
            ghost[d["target"]] = True
        elif e["type"] == "vote" and d["ghost"] and d["yes"]:
            ghost[d["voter"]] = False
    alive = [names[s] for s in range(len(names)) if s not in ghost]
    lines = [f"Lebend ({len(alive)}): {', '.join(alive)}."]
    if ghost:
        dead = [
            f"{names[s]} (Geisterstimme {'noch verfügbar' if ghost[s] else 'verbraucht'})"
            for s in sorted(ghost)
        ]
        lines.append("Tot: " + ", ".join(dead) + ".")
    if seat in ghost:
        lines.append(
            "Du bist tot: keine Fähigkeit mehr, keine Nominierung – aber du redest weiter "
            "mit und spielst für dein Team."
        )
    return "\n".join(lines)


def build_user_prompt(view: list[dict], seat: int, task: str, mark: bool = False) -> str:
    return "\n".join(
        [
            "# Dein Spielverlauf (alles, was du bisher weißt)",
            render_history(view, seat, mark=mark),
            "",
            "# Aktueller Stand",
            public_status(view, seat),
            "",
            "# Deine Aufgabe jetzt",
            task.strip(),
        ]
    )


def build_delta_prompt(view: list[dict], seat: int, task: str, since: int,
                       mark: bool = False) -> str:
    """Folgeprompt für eine fortlaufende Sitzung: nur, was seit ``since`` neu ist.

    Die eigenen Notizen fehlen hier, weil sie als eigene frühere Antwort ohnehin in der
    Sitzung stehen."""
    news = render_history(view, seat, mark=mark, since=since, skip_notes=True)
    return "\n".join(
        [
            "# Neu seit deiner letzten Entscheidung",
            news or "(nichts Neues)",
            "",
            "# Aktueller Stand",
            public_status(view, seat),
            "",
            "# Deine Aufgabe jetzt",
            task.strip(),
        ]
    )


RETRY_NOTE = (
    "\n\nHinweis: Deine vorige Antwort war ungültig ({error}). Antworte erneut im "
    "vorgegebenen Format und halte dich genau an die erlaubten Werte."
)
