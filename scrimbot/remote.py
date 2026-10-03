"""Reaching a game server's files and RCON, locally or over SSH.

The EU (home) server normally shares a machine with the bot, so its get5 files are plain local
paths. The NA server is a different machine: one SSH connection carries both
SFTP (match configs in, stats files out) and an RCON tunnel, so NA keeps
get5's file mode exactly as EU runs it and its RCON port stays on localhost.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath

log = logging.getLogger(__name__)


class RemoteError(ConnectionError):
    """The SSH link to a remote server failed.

    A ConnectionError (so an OSError) on purpose: RconClient already treats an
    OSError while connecting as "could not reach the server", which is exactly
    what an unreachable SSH host or a closed RCON port behind it means.
    """


# ------------------------------------------------------------------- files


class LocalFiles:
    """get5 files on the bot's own disk -- the EU server."""

    remote = False

    @staticmethod
    def join(*parts: str) -> str:
        return os.path.join(*parts)

    async def write(self, path: str, body: str) -> None:
        await asyncio.to_thread(_write_local, path, body)

    async def read(self, path: str) -> str | None:
        return await asyncio.to_thread(_read_local, path)

    def discard(self, path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass


def _write_local(path: str, body: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)


def _read_local(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


class SftpFiles:
    """get5 files on another machine, over the server's SSH link."""

    remote = True

    def __init__(self, link: "SshLink") -> None:
        self.link = link
        self._pending: set[asyncio.Task] = set()

    @staticmethod
    def join(*parts: str) -> str:
        # Remote paths are POSIX even when the bot runs on Windows.
        return posixpath.join(*parts)

    async def write(self, path: str, body: str) -> None:
        async def op(sftp):
            directory = posixpath.dirname(path)
            if directory:
                await sftp.makedirs(directory, exist_ok=True)
            async with sftp.open(path, "w", encoding="utf-8") as handle:
                await handle.write(body)

        await self.link.sftp_call(op)

    async def read(self, path: str) -> str | None:
        import asyncssh

        async def op(sftp):
            try:
                async with sftp.open(
                    path, "r", encoding="utf-8", errors="replace"
                ) as handle:
                    return await handle.read()
            except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPPermissionDenied):
                return None

        try:
            return await self.link.sftp_call(op)
        except RemoteError as exc:
            log.warning("Could not read %s on %s: %s", path, self.link.name, exc)
            return None

    def discard(self, path: str) -> None:
        """Best-effort delete. Sync like the local one, so it is scheduled."""

        async def op(sftp):
            try:
                await sftp.remove(path)
            except Exception:  # noqa: BLE001 - a leftover config is harmless
                pass

        async def run() -> None:
            try:
                await self.link.sftp_call(op)
            except RemoteError as exc:
                log.debug("Could not remove %s on %s: %s", path, self.link.name, exc)

        try:
            task = asyncio.get_running_loop().create_task(run())
        except RuntimeError:  # no loop (sync caller) -- leave the file
            return
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)


# --------------------------------------------------------------------- ssh


class SshLink:
    """One SSH connection to a remote game server, reopened when it drops.

    Host keys are always checked against ``known_hosts`` -- the RCON password
    rides through this connection, so it must never trust a stranger.
    """

    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        username: str,
        key_path: str,
        known_hosts: str,
        *,
        connect_timeout: float = 15.0,
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.username = username
        self.key_path = key_path
        self.known_hosts = known_hosts
        self.connect_timeout = connect_timeout
        self._conn = None
        self._sftp = None
        self._lock = asyncio.Lock()

    async def _connection(self):
        import asyncssh

        async with self._lock:
            conn = self._conn
            if conn is not None and not conn.is_closed():
                return conn
            self._conn = self._sftp = None
            try:
                self._conn = await asyncio.wait_for(
                    asyncssh.connect(
                        self.host,
                        self.port,
                        username=self.username,
                        client_keys=[self.key_path],
                        known_hosts=self.known_hosts,
                        keepalive_interval=30,
                        keepalive_count_max=3,
                    ),
                    self.connect_timeout,
                )
            except (OSError, asyncio.TimeoutError, asyncssh.Error) as exc:
                raise RemoteError(
                    f"SSH to the {self.name} server failed "
                    f"({type(exc).__name__}: {exc})"
                ) from exc
            log.info("SSH link to the %s server is up", self.name)
            return self._conn

    async def _reset(self) -> None:
        async with self._lock:
            conn, self._conn, self._sftp = self._conn, None, None
        if conn is not None:
            conn.close()

    async def open_tcp(self, host: str, port: int):
        """A (reader, writer) pair to host:port as seen from the remote side.

        This is RconClient's connector for a remote server, so a failure has
        to surface as an OSError for it to read as "server unreachable".
        """
        import asyncssh

        for attempt in (1, 2):
            conn = await self._connection()
            try:
                return await asyncio.wait_for(
                    conn.open_connection(host, port), self.connect_timeout
                )
            except asyncssh.ChannelOpenError as exc:
                # The SSH side is fine; nothing is listening on the RCON port
                # yet (srcds is down or still loading a map).
                raise RemoteError(f"{host}:{port} refused on the {self.name} server ({exc.reason})") from exc
            except (OSError, asyncio.TimeoutError, asyncssh.Error) as exc:
                await self._reset()
                if attempt == 2:
                    raise RemoteError(
                        f"SSH tunnel to the {self.name} server failed ({exc or type(exc).__name__})"
                    ) from exc
        raise RemoteError("unreachable")  # pragma: no cover - loop always returns or raises

    async def sftp_call(self, op):
        """Run ``op(sftp_client)``, reconnecting once if the link went stale."""
        import asyncssh

        for attempt in (1, 2):
            try:
                conn = await self._connection()
                async with self._lock:
                    if self._sftp is None:
                        self._sftp = await conn.start_sftp_client()
                    sftp = self._sftp
                return await op(sftp)
            except RemoteError:
                if attempt == 2:
                    raise
                await self._reset()
            except (OSError, asyncio.TimeoutError, asyncssh.Error) as exc:
                if isinstance(exc, asyncssh.SFTPError) and not isinstance(
                    exc, asyncssh.SFTPConnectionLost
                ):
                    # A real filesystem answer (permission, missing dir):
                    # reconnecting will not change it.
                    raise RemoteError(f"SFTP on the {self.name} server: {exc}") from exc
                await self._reset()
                if attempt == 2:
                    raise RemoteError(
                        f"SFTP to the {self.name} server failed ({exc or type(exc).__name__})"
                    ) from exc
        raise RemoteError("unreachable")  # pragma: no cover

    async def close(self) -> None:
        await self._reset()
