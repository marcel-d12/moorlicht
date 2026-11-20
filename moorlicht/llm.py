"""LLM-Anbindung: ein strukturierter Aufruf = eine Entscheidung eines Agenten.

Zwei Backends mit derselben Schnittstelle
``complete(system, user, schema, session=None, delta=None)``:

* ``ClaudeCLIBackend`` – ruft das installierte ``claude`` (Claude Code) headless auf und
  nutzt dessen bestehende Anmeldung. Jeder Aufruf läuft ohne Werkzeuge, ohne MCP-Server,
  ohne Benutzer-/Projekteinstellungen und in einem leeren Arbeitsverzeichnis außerhalb
  des Projekts – der Agent kann also weder Dateien (etwa das Spielprotokoll) lesen noch
  irgendetwas außerhalb des Spiels tun. Jeder Spieler bekommt eine eigene fortlaufende
  Sitzung: Der erste Aufruf enthält den ganzen bisherigen Verlauf (``user``), jeder
  weitere nur das Neue (``delta``). Das spart den Großteil der Eingabe-Tokens, weil der
  bisherige Verlauf aus dem Prompt-Cache kommt.
* ``AnthropicAPIBackend`` – ruft die Claude-API über das offizielle ``anthropic``-SDK
  auf (benötigt ``pip install anthropic`` und Zugangsdaten, z. B. ANTHROPIC_API_KEY).
  Zustandslos: jeder Aufruf enthält den vollständigen Verlauf.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field

DEFAULT_MODEL = "claude-fable-5-2"
DEFAULT_FALLBACK_MODEL = "claude-opus-5-5"

# Umgebungsvariablen einer laufenden Claude-Code-Sitzung, die ein Kindprozess nicht
# erben soll (sonst hält er sich für einen Teil dieser Sitzung).
_SESSION_ENV = (
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_CHILD_NOTES_DIR",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
)

_FLAGGED = re.compile(r"safeguards? flagged|stop_reason.{0,5}refusal", re.I)


@dataclass
class LLMResult:
    data: dict | None = None
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    seconds: float = 0.0
    model: str = ""
    flagged: bool = False  # vom Sicherheitsfilter des Modells abgelehnt
    extra: dict = field(default_factory=dict)

    def absorb(self, earlier: "LLMResult") -> None:
        """Verbrauch eines vorangegangenen (gescheiterten) Versuchs mitzählen."""
        self.input_tokens += earlier.input_tokens
        self.output_tokens += earlier.output_tokens
        self.cache_read_tokens += earlier.cache_read_tokens
        self.cache_write_tokens += earlier.cache_write_tokens
        self.seconds += earlier.seconds
        if earlier.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + earlier.cost_usd


class LLMError(RuntimeError):
    """Nicht behebbarer Fehler der Anbindung (z. B. nicht angemeldet)."""


# ------------------------------------------------------------------- Claude Code CLI

class ClaudeCLIBackend:
    name = "claude-cli"

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "medium", timeout: int = 600,
                 fallback_model: str | None = DEFAULT_FALLBACK_MODEL, sessions: bool = True,
                 exe: list[str] | None = None):
        if exe is None:
            found = shutil.which("claude")
            if not found:
                raise LLMError(
                    "Das Programm „claude“ (Claude Code) wurde nicht gefunden. Installiere "
                    "es oder nutze --backend api."
                )
            exe = [found]
        self.exe = list(exe)
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.fallback_model = fallback_model if fallback_model != model else None
        self.use_sessions = sessions
        # Leeres Arbeitsverzeichnis außerhalb des Projekts.
        self.workdir = tempfile.mkdtemp(prefix="moorlicht-agent-")
        self.promptdir = tempfile.mkdtemp(prefix="moorlicht-prompts-")
        self.env = {k: v for k, v in os.environ.items() if k not in _SESSION_ENV}
        self._files: dict[str, str] = {}
        self._sessions: dict[str, str] = {}     # Schlüssel -> Sitzungs-ID
        self._billed: dict[str, float] = {}     # Sitzungs-ID -> bisher gemeldete Kosten
        self._lock = threading.Lock()

    def _system_file(self, system: str) -> str:
        digest = hashlib.sha256(system.encode("utf-8")).hexdigest()[:16]
        with self._lock:
            path = self._files.get(digest)
            if path is None:
                path = os.path.join(self.promptdir, f"system-{digest}.txt")
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(system)
                self._files[digest] = path
        return path

    def _run(self, model: str, system: str, prompt: str, schema: dict,
             session_args: list[str], session_id: str | None) -> LLMResult:
        cmd = self.exe + [
            "-p",
            "--model", model,
            "--effort", self.effort,
            "--tools", "",                 # keine eingebauten Werkzeuge
            "--strict-mcp-config",         # keine MCP-Server (Gmail, Drive, ...)
            "--setting-sources", "",       # keine Benutzer-/Projekteinstellungen
            "--safe-mode",                 # keine CLAUDE.md, Skills, Plugins, Hooks
            "--disable-slash-commands",
            "--system-prompt-file", self._system_file(system),
            "--output-format", "json",
            "--json-schema", json.dumps(schema, ensure_ascii=True),
        ] + session_args
        started = time.time()
        res = LLMResult(model=model)
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.workdir,
                env=self.env,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            res.error = f"Zeitüberschreitung nach {self.timeout}s"
            res.seconds = time.time() - started
            return res
        res.seconds = time.time() - started
        try:
            out = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError):
            res.error = (
                f"keine JSON-Ausgabe (Exit-Code {proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[:300]}"
            )
            return res
        usage = out.get("usage") or {}
        res.input_tokens = int(usage.get("input_tokens") or 0)
        res.output_tokens = int(usage.get("output_tokens") or 0)
        res.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
        res.cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
        total = out.get("total_cost_usd")
        if isinstance(total, (int, float)):
            if session_id is None:
                res.cost_usd = float(total)
            else:
                # In einer fortgesetzten Sitzung meldet claude die Summe aller Aufrufe.
                with self._lock:
                    before = self._billed.get(session_id, 0.0)
                    self._billed[session_id] = max(before, float(total))
                res.cost_usd = max(0.0, float(total) - before)
        result_text = str(out.get("result") or "")
        if out.get("is_error"):
            res.flagged = bool(_FLAGGED.search(result_text)) or out.get("stop_reason") == "refusal"
            res.error = f"Fehler von claude: {result_text[:300]}"
            return res
        data = out.get("structured_output")
        if data is None and result_text:
            try:
                data = json.loads(result_text)
            except json.JSONDecodeError:
                data = None
        if not isinstance(data, dict):
            res.flagged = out.get("stop_reason") == "refusal" or bool(_FLAGGED.search(result_text))
            res.error = (
                f"keine strukturierte Antwort (stop_reason={out.get('stop_reason')}): "
                f"{result_text[:200]}"
            )
            return res
        res.data = data
        return res

    def _attempt(self, model: str, system: str, user: str, schema: dict,
                 session: str | None, delta: str | None) -> LLMResult:
        if session is None or not self.use_sessions:
            return self._run(model, system, user, schema, ["--no-session-persistence"], None)
        with self._lock:
            session_id = self._sessions.get(session)
        if session_id is not None and delta is not None:
            res = self._run(model, system, delta, schema, ["--resume", session_id], session_id)
            res.extra["session"] = "fortgesetzt"
        else:
            session_id = str(uuid.uuid4())
            with self._lock:
                self._sessions[session] = session_id
            res = self._run(model, system, user, schema, ["--session-id", session_id], session_id)
            res.extra["session"] = "neu"
        if res.data is None:
            # Nach einem Fehler nicht weiter auf dieser Sitzung aufbauen: Der nächste
            # Aufruf beginnt eine frische Sitzung mit dem vollständigen Verlauf.
            with self._lock:
                if self._sessions.get(session) == session_id:
                    del self._sessions[session]
        return res

    def complete(self, system: str, user: str, schema: dict, session: str | None = None,
                 delta: str | None = None) -> LLMResult:
        res = self._attempt(self.model, system, user, schema, session, delta)
        if not res.flagged:
            return res
        # Der Sicherheitsfilter des Modells schlägt gelegentlich auch bei harmlosen
        # Spielzügen an. Erst frisch wiederholen, dann notfalls das Ausweichmodell.
        again = self._attempt(self.model, system, user, schema, session, None)
        again.absorb(res)
        again.extra["safeguard_retries"] = 1
        if not again.flagged or not self.fallback_model:
            return again
        other = self._run(self.fallback_model, system, user, schema, ["--no-session-persistence"], None)
        other.absorb(again)
        other.extra["safeguard_retries"] = 2
        other.extra["fallback_model"] = True
        return other

    def close(self) -> None:
        # Sitzungsmitschriften der Agenten wieder löschen (nur die dieses Arbeitsordners).
        if self._billed:
            try:
                subprocess.run(self.exe + ["purge", "-y", self.workdir], capture_output=True,
                               text=True, cwd=self.workdir, env=self.env, timeout=60)
            except (OSError, subprocess.SubprocessError):
                pass
        shutil.rmtree(self.workdir, ignore_errors=True)
        shutil.rmtree(self.promptdir, ignore_errors=True)


# ------------------------------------------------------------------------ Claude API

# Listenpreise in USD pro Million Tokens (Eingabe, Ausgabe, Cache-Lesen) – nur für die
# Kostenschätzung im Protokoll; maßgeblich ist die Abrechnung von Anthropic.
_PRICES = {
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-haiku-5-5": (0.1, 0.5, 0.01),
}


class AnthropicAPIBackend:
    name = "api"

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "medium", timeout: int = 600):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - hängt von der Umgebung ab
            raise LLMError(
                "Für --backend api wird das Paket „anthropic“ benötigt: pip install anthropic"
            ) from exc
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(timeout=float(timeout))
        self.model = model
        self.effort = effort

    def complete(self, system: str, user: str, schema: dict, session: str | None = None,
                 delta: str | None = None) -> LLMResult:
        anthropic = self._anthropic
        started = time.time()
        res = LLMResult(model=self.model)
        try:
            # Fable 5.2: Thinking ist immer aktiv (kein thinking-Parameter); die Tiefe
            # steuert output_config.effort. Server-seitige Fallbacks fangen den Fall ab,
            # dass ein Sicherheitsklassifikator eine Anfrage ablehnt.
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as exc:
            raise LLMError(f"Anmeldung an der Claude-API fehlgeschlagen: {exc}") from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"Kein Zugriff auf die Claude-API: {exc}") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"Modell oder Endpunkt nicht gefunden: {exc}") from exc
        except anthropic.BadRequestError as exc:
            res.error = f"Ungültige Anfrage: {exc}"
        except anthropic.RateLimitError as exc:
            res.error = f"Ratenlimit: {exc}"
        except anthropic.APIStatusError as exc:
            res.error = f"API-Fehler {exc.status_code}: {exc}"
        except anthropic.APIConnectionError as exc:
            res.error = f"Verbindungsfehler: {exc}"
        res.seconds = time.time() - started
        if res.error:
            return res
        usage = response.usage
        res.model = getattr(response, "model", self.model) or self.model
        res.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        res.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        res.cache_read_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        res.cache_write_tokens = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        price = _PRICES.get(self.model)
        if price:
            res.cost_usd = (
                res.input_tokens * price[0]
                + res.cache_write_tokens * price[0] * 1.25
                + res.cache_read_tokens * price[2]
                + res.output_tokens * price[1]
            ) / 1_000_000
        # Hat ein server-seitiges Ausweichmodell geantwortet?
        iterations = getattr(usage, "iterations", None) or []
        if any(getattr(entry, "type", "") == "fallback_message" for entry in iterations):
            res.extra["fallback_model"] = True
        if response.stop_reason == "refusal":
            res.flagged = True
            res.error = "Anfrage wurde abgelehnt (stop_reason=refusal)"
            return res
        if response.stop_reason == "max_tokens":
            res.error = "Antwort abgeschnitten (max_tokens)"
            return res
        text = next((b.text for b in response.content if b.type == "text"), None)
        try:
            data = json.loads(text) if text else None
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            res.error = "keine strukturierte Antwort"
            return res
        res.data = data
        return res

    def close(self) -> None:
        pass


def make_backend(kind: str, model: str, effort: str, timeout: int,
                 fallback_model: str | None = DEFAULT_FALLBACK_MODEL, sessions: bool = True):
    """``auto``: API, wenn ANTHROPIC_API_KEY gesetzt und das SDK installiert ist, sonst
    das lokale ``claude``."""
    if kind == "auto":
        kind = "claude-cli"
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                import anthropic  # noqa: F401

                kind = "api"
            except ImportError:
                pass
    if kind == "api":
        return AnthropicAPIBackend(model, effort, timeout)
    if kind == "claude-cli":
        return ClaudeCLIBackend(model, effort, timeout, fallback_model=fallback_model,
                                sessions=sessions)
    raise LLMError(f"unbekanntes Backend: {kind}")
