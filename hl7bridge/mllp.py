"""MLLP (Minimal Lower Layer Protocol) listener and client - the transport VistA HL7 links use.

Frame: <VT> message <FS><CR>. One ACK is returned per framed message, in order.
In deployment this listener sits behind a TLS-terminating load balancer inside the boundary
(or the link runs over the VA site-to-site VPN); MLLP itself has no encryption.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from typing import Callable

SB, EB, CR = b"\x0b", b"\x1c", b"\x0d"
MAX_MESSAGE = 10 * 1024 * 1024

log = logging.getLogger("hl7bridge.mllp")


def frame(text: str) -> bytes:
    return SB + text.encode("utf-8") + EB + CR


def _decode(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("latin-1")  # HL7 v2 default character sets are 8-bit


class MLLPServer:
    def __init__(self, handler: Callable[[str], str], host: str = "127.0.0.1", port: int = 2575):
        self.handler = handler
        self.host, self.port = host, port
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        buf = b""
        try:
            while data := await reader.read(65536):
                buf += data
                if len(buf) > MAX_MESSAGE:
                    log.warning("dropping %s: frame exceeds %d bytes", peer, MAX_MESSAGE)
                    break
                while True:
                    start = buf.find(SB)
                    if start == -1:
                        buf = b""
                        break
                    end = buf.find(EB + CR, start)
                    if end == -1:
                        buf = buf[start:]
                        break
                    payload, buf = buf[start + 1 : end], buf[end + 2 :]
                    loop = asyncio.get_running_loop()
                    ack = await loop.run_in_executor(None, self.handler, _decode(payload))
                    writer.write(frame(ack))
                    await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def _serve(self) -> None:
        self._server = await asyncio.start_server(self._client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()
        log.info("MLLP listening on %s:%d", self.host, self.port)
        async with self._server:
            await self._server.serve_forever()

    def start_in_thread(self) -> "MLLPServer":
        def run() -> None:
            self._loop = asyncio.new_event_loop()
            try:
                self._loop.run_until_complete(self._serve())
            except asyncio.CancelledError:
                pass

        threading.Thread(target=run, name="mllp", daemon=True).start()
        self._ready.wait(5)
        return self

    def stop(self) -> None:
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)


def send(text: str, host: str = "127.0.0.1", port: int = 2575, timeout: float = 10) -> str:
    """Send one message over MLLP and return the ACK text."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(frame(text.replace("\r\n", "\r").replace("\n", "\r").strip()))
        buf = b""
        while not buf.endswith(EB + CR):
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed before ACK")
            buf += chunk
    return _decode(buf.strip(SB).rstrip(CR).rstrip(EB))
