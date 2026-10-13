"""A tiny local SMTP sink for development and tests -- src/monitoring/alerts/sink.py (ADR-0052).

Binds to 127.0.0.1 on an ephemeral port, records every accepted message, and can simulate
transient/permanent server failures. It is NEVER started automatically (no import side effects);
tests and `scripts/alert_delivery_validation.py` start it explicitly. Delivery to this sink proves
the real message formatting and SMTP client path -- it is NOT evidence that a remote provider
accepted anything.
"""

from __future__ import annotations

import socketserver
import threading
from dataclasses import dataclass, field


@dataclass
class ReceivedMessage:
    mail_from: str
    rcpt_to: list[str]
    raw: str


class _Handler(socketserver.StreamRequestHandler):
    def _reply(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())
        self.wfile.flush()

    def handle(self) -> None:  # noqa: C901 -- a minimal SMTP state machine
        sink: SmtpSink = self.server.sink  # type: ignore[attr-defined]
        mode = sink._next_mode()
        if mode == "drop":
            return  # close without a greeting: a connection-level failure
        self._reply("220 atp-sink ESMTP ready")
        mail_from, rcpts = "", []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            cmd = line.upper()
            if cmd.startswith(("EHLO", "HELO")):
                self._reply("250 atp-sink")
            elif cmd.startswith("MAIL FROM"):
                if mode == "temp":
                    self._reply("451 4.3.0 temporary failure, try again later")
                    continue
                mail_from = line.split(":", 1)[1].strip().strip("<>").split(">")[0]
                self._reply("250 OK")
            elif cmd.startswith("RCPT TO"):
                if mode == "reject_rcpt":
                    self._reply("550 5.1.1 mailbox unavailable")
                    continue
                rcpts.append(line.split(":", 1)[1].strip().strip("<>").split(">")[0])
                self._reply("250 OK")
            elif cmd == "DATA":
                self._reply("354 end data with <CR><LF>.<CR><LF>")
                chunks = []
                while True:
                    part = self.rfile.readline()
                    if not part or part in (b".\r\n", b".\n"):
                        break
                    chunks.append(part.decode("utf-8", "replace"))
                sink._record(ReceivedMessage(mail_from, list(rcpts), "".join(chunks)))
                self._reply("250 OK queued")
            elif cmd == "RSET":
                mail_from, rcpts = "", []
                self._reply("250 OK")
            elif cmd == "NOOP":
                self._reply("250 OK")
            elif cmd == "QUIT":
                self._reply("221 bye")
                return
            else:
                self._reply("502 command not implemented")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@dataclass
class SmtpSink:
    host: str = "127.0.0.1"
    port: int = 0
    messages: list[ReceivedMessage] = field(default_factory=list)
    _modes: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _server: _Server | None = None
    _thread: threading.Thread | None = None

    def start(self) -> "SmtpSink":
        self._server = _Server((self.host, self.port), _Handler)
        self._server.sink = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> "SmtpSink":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- failure scripting: modes are consumed one per connection; empty => accept
    def fail_next(self, mode: str, times: int = 1) -> None:
        if mode not in ("drop", "temp", "reject_rcpt"):
            raise ValueError("mode must be drop | temp | reject_rcpt")
        with self._lock:
            self._modes.extend([mode] * times)

    def _next_mode(self) -> str:
        with self._lock:
            return self._modes.pop(0) if self._modes else "ok"

    def _record(self, msg: ReceivedMessage) -> None:
        with self._lock:
            self.messages.append(msg)
