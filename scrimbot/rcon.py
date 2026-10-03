from __future__ import annotations

import asyncio
import itertools
import struct

SERVERDATA_RESPONSE_VALUE = 0
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_AUTH = 3

MAX_PACKET = 4096


class RconError(RuntimeError):
    """Any RCON transport / protocol failure."""


class RconAuthError(RconError):
    """The server rejected the RCON password."""


class RconNotReady(RconError):
    """The server took the connection but never finished authenticating.

    Deliberately not an RconAuthError: srcds drops RCON auth while it is
    loading a map, and that is a wait-and-retry, not a bad password.
    """


def _encode(packet_id: int, packet_type: int, body: str) -> bytes:
    payload = struct.pack("<ii", packet_id, packet_type) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


class RconClient:
    """Minimal async Source RCON client (CS:GO, CS2, any Source dedicated server).

    Keeps one lazily-opened connection and reconnects transparently. Written
    against the protocol directly so the bot has no native RCON dependency.
    """

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        *,
        timeout: float = 8.0,
        connector=None,
    ) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._timeout = timeout
        # Opens the (reader, writer) pair. Default: a direct TCP connection.
        # A remote server passes its SSH tunnel here instead.
        self._connector = connector or (
            lambda: asyncio.open_connection(self._host, self._port)
        )
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass

    async def _read_packet(self) -> tuple[int, int, str]:
        assert self._reader is not None
        header = await self._reader.readexactly(4)
        (length,) = struct.unpack("<i", header)
        if not 10 <= length <= MAX_PACKET * 4:
            raise RconError(f"Bogus RCON packet length: {length}")
        data = await self._reader.readexactly(length)
        packet_id, packet_type = struct.unpack("<ii", data[:8])
        body = data[8:-2].decode("utf-8", errors="replace")
        return packet_id, packet_type, body

    async def _connect(self) -> None:
        await self.close()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                self._connector(), self._timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise RconError(
                f"Could not reach {self._host}:{self._port} ({exc or type(exc).__name__})."
            ) from exc

        auth_id = next(self._ids)
        self._writer.write(_encode(auth_id, SERVERDATA_AUTH, self._password))
        await self._writer.drain()

        # The server may send an empty RESPONSE_VALUE before the auth result.
        while True:
            try:
                packet_id, packet_type, _ = await asyncio.wait_for(
                    self._read_packet(), self._timeout
                )
            except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError) as exc:
                await self.close()
                raise RconNotReady(
                    "Server closed the connection during authentication "
                    "(it is probably still loading a map)."
                ) from exc
            if packet_type == SERVERDATA_AUTH_RESPONSE:
                if packet_id == -1:
                    await self.close()
                    raise RconAuthError("RCON authentication failed (wrong password).")
                return

    async def _run(self, command: str, expect_response: bool) -> str:
        if not self.connected:
            await self._connect()
        assert self._writer is not None

        command_id = next(self._ids)
        self._writer.write(_encode(command_id, SERVERDATA_EXECCOMMAND, command))

        if not expect_response:
            # e.g. changelevel: the server stalls and drops us mid-load, so we
            # fire the command, make sure it left the socket, and hang up.
            await self._writer.drain()
            await self.close()
            return ""

        # Trailing empty packet acts as a sentinel so multi-packet responses
        # can be read to completion.
        sentinel_id = next(self._ids)
        self._writer.write(_encode(sentinel_id, SERVERDATA_RESPONSE_VALUE, ""))
        await self._writer.drain()

        chunks: list[str] = []
        while True:
            packet_id, _packet_type, body = await asyncio.wait_for(
                self._read_packet(), self._timeout
            )
            if packet_id == sentinel_id:
                break
            chunks.append(body)
        return "".join(chunks).strip()

    async def execute(self, command: str, *, expect_response: bool = True) -> str:
        """Run a console command. Retries once on a dropped connection."""
        async with self._lock:
            try:
                return await self._run(command, expect_response)
            except RconAuthError:
                raise
            except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, RconError):
                await self.close()
                try:
                    return await self._run(command, expect_response)
                except RconAuthError:
                    raise
                except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
                    await self.close()
                    raise RconError(f"RCON command failed: {exc or type(exc).__name__}") from exc


class NullRconClient(RconClient):
    """Stand-in used when RCON_ENABLED=0 -- logs instead of connecting."""

    def __init__(self) -> None:
        super().__init__("disabled", 0, "")

    async def execute(self, command: str, *, expect_response: bool = True) -> str:
        return f"[dry run] {command}"

    async def close(self) -> None:
        return None
