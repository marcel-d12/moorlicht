"""Attrappe des ``claude``-Programms für die Tests des CLI-Backends.

Versteht genau die Argumente, die ``ClaudeCLIBackend`` übergibt, merkt sich Sitzungen in
Dateien im Arbeitsverzeichnis und protokolliert jeden Aufruf nach ``calls.jsonl``.
Steuerwörter im Prompt lösen Fehlerfälle aus:

* ``FLAG_ALWAYS`` – das Hauptmodell lehnt immer ab (Sicherheitsfilter)
* ``FLAG_ONCE``   – nur der erste Aufruf mit diesem Prompt wird abgelehnt
* ``CRASH``       – kaputte Ausgabe
"""

import hashlib
import json
import os
import sys

MAIN_MODEL = "claude-fable-5-2"


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "purge":
        with open("purged.txt", "w", encoding="utf-8") as fh:
            fh.write(" ".join(args))
        print("Purged")
        return 0

    def value(flag):
        return args[args.index(flag) + 1] if flag in args else None

    prompt = sys.stdin.read()
    model = value("--model")
    schema = json.loads(value("--json-schema"))
    system = open(value("--system-prompt-file"), encoding="utf-8").read()
    new_id, resume_id = value("--session-id"), value("--resume")
    record = {"args": args, "prompt": prompt, "model": model, "system": system, "cwd": os.getcwd(),
              "mode": "new" if new_id else "resume" if resume_id else "stateless"}
    with open("calls.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # Eine Datei je Sitzung: gleichzeitige Aufrufe verschiedener Spieler stören sich nicht.
    def session_file(session_id):
        return f"session-{session_id}.json"

    def reply(payload):
        print(json.dumps(payload))
        return 0

    def error(text):
        return reply({"is_error": True, "result": text, "usage": {"input_tokens": 3}, "total_cost_usd": 0.001})

    if "CRASH" in prompt:
        print("kaputt")
        return 1
    if resume_id and not os.path.exists(session_file(resume_id)):
        return error(f"No conversation found with session ID: {resume_id}")
    flagged = "API Error: Fable 5.2's safeguards flagged this message."
    if "FLAG_ALWAYS" in prompt and model == MAIN_MODEL:
        return error(flagged)
    once = "flagged-" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    if "FLAG_ONCE" in prompt and not os.path.exists(once):
        open(once, "w").close()
        return error(flagged)

    session_id = new_id or resume_id
    total = 0.01
    if session_id:
        path = session_file(session_id)
        history = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else []
        history.append(prompt)
        json.dump(history, open(path, "w", encoding="utf-8"))
        total = 0.01 * len(history)
    data = {}
    if "wahl" in schema["properties"] and "# Antwortformat" in prompt:
        # Festes Antwortformat der Spieler: erlaubte Werte stehen im Prompt.
        block = prompt.rsplit("# Antwortformat", 1)[1]
        for line in block.splitlines():
            if not line.startswith("- "):
                continue
            slot, _, rest = line[2:].partition(": ")
            if rest.startswith("leer lassen"):
                data[slot] = ""
            elif rest.startswith("genau einer dieser Werte: "):
                data[slot] = rest[len("genau einer dieser Werte: "):].split(" – ")[0].split(" | ")[0]
            else:
                data[slot] = f"{slot} von {model}"
        schema = {"properties": {}}
    for key, spec in schema["properties"].items():
        if "enum" in spec:
            data[key] = spec["enum"][0]
        elif spec.get("type") == "boolean":
            data[key] = True
        else:
            data[key] = f"{key} von {model}"
    return reply({
        "is_error": False, "structured_output": data, "result": json.dumps(data),
        "session_id": session_id, "total_cost_usd": total,
        "usage": {"input_tokens": 2, "output_tokens": 50, "cache_read_input_tokens": 1000 if resume_id else 0,
                  "cache_creation_input_tokens": 100},
    })


if __name__ == "__main__":
    sys.exit(main())
