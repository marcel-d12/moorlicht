"""Kleiner lokaler Webserver für den Zuschauer-Viewer (nur 127.0.0.1)."""

from __future__ import annotations

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

VIEWER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer.html")


def list_games(root: str) -> list[dict]:
    games = []
    if os.path.isdir(root):
        for name in os.listdir(root):
            path = os.path.join(root, name, "events.jsonl")
            if os.path.isfile(path):
                games.append({"name": name, "mtime": os.path.getmtime(path),
                              "size": os.path.getsize(path)})
    games.sort(key=lambda g: g["mtime"], reverse=True)
    return games


def read_events(root: str, game: str, start: int) -> list[dict] | None:
    # Nur direkte Unterordner des Spiele-Verzeichnisses – keine Pfadangaben.
    if not game or game != os.path.basename(game) or game in (".", ".."):
        return None
    path = os.path.join(root, game, "events.jsonl")
    if not os.path.isfile(path):
        return None
    events = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i < start:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                break  # letzte Zeile wird gerade geschrieben
    return events


def make_handler(root: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # still
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status: int = 200) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path in ("/", "/index.html"):
                with open(VIEWER, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            elif url.path == "/api/games":
                self._json(list_games(root))
            elif url.path == "/api/events":
                try:
                    start = max(0, int(query.get("from", ["0"])[0]))
                except ValueError:
                    start = 0
                events = read_events(root, query.get("game", [""])[0], start)
                if events is None:
                    self._json({"error": "Spiel nicht gefunden"}, 404)
                else:
                    self._json(events)
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

    return Handler


def start_server(root: str, port: int = 8765) -> tuple[ThreadingHTTPServer, int]:
    last_error: OSError | None = None
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), make_handler(root))
        except OSError as exc:
            last_error = exc
            continue
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, candidate
    raise OSError(f"kein freier Port ab {port}: {last_error}")


def viewer_url(port: int, game: str | None = None) -> str:
    url = f"http://127.0.0.1:{port}/"
    return url + (f"?game={quote(game)}" if game else "")


def open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # Browser ist Komfort, kein Muss
        pass
