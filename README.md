# Moorlicht

KI-Agenten spielen gegeneinander ein Deduktionsspiel mit verborgenen Rollen – und du
schaust zu: jede Wortmeldung, jedes Flüstergespräch, jede Nominierung und Abstimmung,
dazu das komplette Erzähler-Wissen und die privaten Notizen der Agenten.

„Moorlicht“ ist ein eigenständiges Spiel im Genre der Social-Deduction-Spiele mit
Erzähler: acht Spieler im Sitzkreis, Team Dorf gegen Team Moor, Nachtfähigkeiten,
Falschinformation durch „Nebel“, Tote mit Geisterstimme, Nominierungen und Hinrichtungen.
Es ist kein Nachbau eines bestehenden Spiels; Rollen und Regeln sind eigene.

## Schnellstart

Voraussetzung: Python 3.10+ und ein angemeldetes [Claude Code](https://claude.com/claude-code)
(`claude`). Weitere Pakete sind nicht nötig.

```powershell
# Regeln und Rollenliste lesen
python -m moorlicht rules

# Kostenloser Probelauf mit Zufallsspielern (zeigt Ablauf und Oberfläche)
python -m moorlicht run --backend mock --view

# Echte Partie: 8 Agenten auf Claude Fable 5.2, live im Browser verfolgen
python -m moorlicht run --view

# Gespeicherte Partien ansehen
python -m moorlicht view
```

`--view` öffnet den Viewer unter `http://127.0.0.1:8765/`. Protokolle landen in
`games/<Zeitstempel>-seed<N>/events.jsonl`.

Eine echte Beispielpartie liegt bei (auf einen Tag gekürzt, 8 Agenten auf Fable 5.2):

```powershell
python -m moorlicht view --dir beispiele
```

## Kosten und Dauer

Gemessen mit Claude Fable 5.2 (Aufwand `medium`) über das lokale `claude`: Der auf einen
Tag gekürzte Beispiellauf brauchte 52 LLM-Aufrufe, gut 5 Minuten und 2,99 USD nach
Listenpreis. Eine volle Partie mit den Standardeinstellungen liegt bei etwa 250 bis 350
Aufrufen; hochgerechnet sind das grob 15 bis 25 USD und 30 bis 45 Minuten. Mit einem
Claude-Abo geht der Verbrauch vom Nutzungskontingent ab.

Begrenzen lässt sich das mit `--max-cost` und `--max-calls`, senken mit weniger
Gesprächsrunden, `--vote-mode parallel` oder `--effort low`.

## Was du siehst

- **Sitzkreis** mit Rollen, Toten, Geisterstimmen, Nebel, Richtblock, laufenden
  Flüsterpaaren und Stimmen.
- **Timeline** mit allen Ereignissen: Nachtaktionen und Erzähler-Auskünfte (inklusive
  „wahr“ oder „falsch wegen Nebel“), Flüstergespräche, öffentliche Reden, Nominierungen,
  Abstimmungen, Hinrichtungen.
- **Perspektive** umschaltbar: Erzähler (alles), Öffentlich (nur der Tisch) oder ein
  einzelner Spieler (genau das, was er weiß).
- **Notizen**: Jeder Agent hält bei jeder Entscheidung eine kurze private Merknotiz fest
  (Verdacht, Plan, Bluff) – ein- und ausblendbar.
- **Wiedergabe**: Schritt vor/zurück (← →), Abspielen (Leertaste), Tempo, Live-Modus.

## Wichtige Optionen

| Option | Bedeutung |
| --- | --- |
| `--seed N` | Rollen- und Sitzverteilung reproduzierbar machen |
| `--roles a,b,...` | Rollen vorgeben (IDs aus `python -m moorlicht rules`) |
| `--model`, `--effort` | Modell und Denkaufwand der Agenten (Standard `claude-fable-5-2`, `medium`) |
| `--language Englisch` | Sprache, in der die Agenten reden (Regeltexte bleiben deutsch) |
| `--whisper-rounds 2,1` | Flüsterrunden pro Tag (Tag 1, danach) |
| `--discussion-rounds 2` | öffentliche Gesprächsrunden pro Tag |
| `--vote-mode parallel` | alle stimmen gleichzeitig ab (schneller, weniger „Tischgefühl“) |
| `--storyteller heuristic` | Erzähler-Ermessen ohne LLM |
| `--max-calls`, `--max-cost` | harte Obergrenzen für LLM-Aufrufe bzw. USD |
| `--backend api` | Claude-API statt lokalem `claude` (`pip install anthropic`, `ANTHROPIC_API_KEY`) |

Alle Optionen: `python -m moorlicht run --help`.

## Wie es funktioniert

- **Engine** (`moorlicht/engine.py`): besitzt den gesamten Spielzustand und setzt alle
  Regeln deterministisch durch. Agenten liefern nur Entscheidungen in einem festen
  Schema; ungültige Antworten werden einmal wiederholt und sonst durch eine erlaubte
  Standardaktion ersetzt (im Viewer unter „Technik“ sichtbar).
- **Geheimnisse** (`moorlicht/events.py`): ein einziges Ereignisprotokoll mit
  Sichtbarkeit je Ereignis (öffentlich, bestimmte Spieler, nur Zuschauer). Jeder
  Spieler-Prompt entsteht ausschließlich aus der gefilterten Sicht dieses Spielers.
- **Agenten** (`moorlicht/llm.py`): Jeder Spieler ist eine eigene fortlaufende
  Claude-Sitzung ohne Werkzeuge, ohne MCP-Server, ohne Benutzereinstellungen, in einem
  leeren Arbeitsordner – er kann nur spielen. Lehnt der Sicherheitsfilter von Fable 5.2
  einen Spielzug zweimal ab, springt für diese eine Entscheidung ein Ausweichmodell ein
  (`--fallback-model`, im Protokoll vermerkt).
- **Erzähler**: Wo die Regeln dem Erzähler die Wahl lassen (vor allem: welche Information
  bekommt ein Spieler im Nebel?), wählt ein LLM-Erzähler aus von der Engine vorgegebenen
  Optionen.

## Tests

```powershell
python -m unittest discover -s tests
```

Die Tests laufen ohne LLM: Hunderte Zufallspartien gegen Regel-Invarianten, gezielte
Szenarien für jede Mechanik, Prüfungen, dass kein Prompt fremdes Wissen enthält, und das
CLI-Backend gegen eine Attrappe von `claude`.
