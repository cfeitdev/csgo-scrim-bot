"""The NA link over a real SSH connection.

Starts an in-process SSH server (asyncssh) with SFTP and port forwarding,
plus a minimal Source RCON server behind it, so the tunnel, the SFTP file
layer and host-key checking are exercised on the actual protocols.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import asyncssh

    HAS_ASYNCSSH = True
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    HAS_ASYNCSSH = False

from scrimbot.config import Config  # noqa: E402
from scrimbot.get5 import Get5Manager  # noqa: E402
from scrimbot.match import Match, Player  # noqa: E402
from scrimbot.maps import MAP_POOL  # noqa: E402
from scrimbot.rcon import RconAuthError, RconClient, RconError, _encode  # noqa: E402
from scrimbot.remote import RemoteError, SftpFiles, SshLink  # noqa: E402
from scrimbot.server import ServerController  # noqa: E402

RCON_PASSWORD = "tunnel-pass"


class FakeSrcds:
    """Just enough Source RCON to authenticate and echo commands back."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        try:
            while True:
                (length,) = struct.unpack("<i", await reader.readexactly(4))
                data = await reader.readexactly(length)
                packet_id, packet_type = struct.unpack("<ii", data[:8])
                body = data[8:-2].decode()
                if packet_type == 3:  # auth
                    ok = body == RCON_PASSWORD
                    writer.write(_encode(packet_id, 0, ""))
                    writer.write(_encode(packet_id if ok else -1, 2, ""))
                elif packet_type == 2:  # exec
                    self.commands.append(body)
                    writer.write(_encode(packet_id, 0, f"ran {body}"))
                else:  # the client's sentinel
                    writer.write(_encode(packet_id, 0, ""))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


if HAS_ASYNCSSH:

    class _ForwardingServer(asyncssh.SSHServer):
        def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
            return True  # allow the RCON tunnel


@unittest.skipUnless(HAS_ASYNCSSH, "asyncssh is not installed")
class SshLinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        base = pathlib.Path(self._dir.name)
        self.root = base / "na-root"
        self.root.mkdir()

        host_key = asyncssh.generate_private_key("ssh-ed25519")
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        self.key_path = str(base / "bot_key")
        client_key.write_private_key(self.key_path)
        authorized = asyncssh.import_authorized_keys(
            client_key.export_public_key().decode()
        )

        root = str(self.root)
        self.ssh = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_host_keys=[host_key],
            authorized_client_keys=authorized,
            server_factory=_ForwardingServer,
            sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=root),
        )
        self.ssh_port = self.ssh.sockets[0].getsockname()[1]
        self.known_hosts = str(base / "known_hosts")
        pathlib.Path(self.known_hosts).write_text(
            f"[127.0.0.1]:{self.ssh_port} "
            f"{host_key.export_public_key().decode().strip()}\n"
        )

        self.srcds = FakeSrcds()
        await self.srcds.start()
        self.link = self.make_link()

    async def asyncTearDown(self):
        await self.link.close()
        self.ssh.close()
        await self.ssh.wait_closed()
        await self.srcds.stop()
        self._dir.cleanup()

    def make_link(self, known_hosts=None, port=None) -> SshLink:
        return SshLink(
            "NA",
            "127.0.0.1",
            port or self.ssh_port,
            "scrimbot",
            self.key_path,
            known_hosts or self.known_hosts,
            connect_timeout=5.0,
        )

    def rcon(self, password=RCON_PASSWORD, port=None, link=None) -> RconClient:
        link = link or self.link
        port = port or self.srcds.port
        return RconClient(
            "127.0.0.1",
            port,
            password,
            timeout=5.0,
            connector=lambda: link.open_tcp("127.0.0.1", port),
        )

    # ------------------------------------------------------------------ rcon

    async def test_rcon_runs_through_the_tunnel(self):
        client = self.rcon()
        self.assertEqual(await client.execute("status"), "ran status")
        self.assertEqual(await client.execute("get5_game_state"), "ran get5_game_state")
        self.assertEqual(self.srcds.commands, ["status", "get5_game_state"])
        await client.close()

    async def test_a_wrong_rcon_password_is_still_an_auth_failure(self):
        client = self.rcon(password="nope")
        with self.assertRaises(RconAuthError):
            await client.execute("status")
        await client.close()

    async def test_a_closed_rcon_port_reads_as_unreachable(self):
        await self.srcds.stop()
        client = self.rcon()
        with self.assertRaises(RconError) as ctx:
            await client.execute("status")
        self.assertNotIsInstance(ctx.exception, RconAuthError)
        await client.close()

    async def test_server_wait_until_ready_rides_out_a_closed_port(self):
        port = self.srcds.port
        await self.srcds.stop()

        async def bring_back() -> None:
            await asyncio.sleep(0.3)
            self.srcds.server = await asyncio.start_server(
                self.srcds._handle, "127.0.0.1", port
            )

        task = asyncio.create_task(bring_back())
        controller = ServerController(self.rcon(port=port))
        self.assertTrue(await controller.wait_until_ready(timeout=10.0, interval=0.1))
        await task
        await controller.rcon.close()

    async def test_the_link_reconnects_after_a_drop(self):
        client = self.rcon()
        await client.execute("status")
        # Kill the SSH connection under the client, as a network blip would.
        self.link._conn.close()
        await asyncio.sleep(0.1)
        self.assertEqual(await client.execute("status"), "ran status")
        await client.close()

    # ------------------------------------------------------------------ sftp

    async def test_files_round_trip_over_sftp(self):
        files = SftpFiles(self.link)
        path = "/csgo/addons/sourcemod/configs/get5/m1-abc.cfg"
        await files.write(path, '"Match" { }\n')
        on_disk = self.root / "csgo/addons/sourcemod/configs/get5/m1-abc.cfg"
        self.assertEqual(on_disk.read_text(), '"Match" { }\n')
        self.assertEqual(await files.read(path), '"Match" { }\n')

        files.discard(path)
        for _ in range(100):
            if not on_disk.exists():
                break
            await asyncio.sleep(0.02)
        self.assertFalse(on_disk.exists())

    async def test_a_missing_file_reads_as_none(self):
        files = SftpFiles(self.link)
        self.assertIsNone(await files.read("/csgo/get5_matchstats_nope.cfg"))

    async def test_get5_drives_a_remote_server_end_to_end(self):
        config = Config(
            token="x", guild_id=None, rcon_host="127.0.0.1", rcon_port=self.srcds.port,
            rcon_password=RCON_PASSWORD, rcon_enabled=True, connect_address="",
            steam_api_key="", lobby_size=2, pick_timeout=None, store_path="s.json",
            default_elo=1000, default_mode="5v5",
            get5_config_dir="/csgo/addons/sourcemod/configs/get5",
        )
        controller = ServerController(self.rcon())
        manager = Get5Manager(config, controller, files=SftpFiles(self.link))

        match = Match(host_id=1, size=2)
        match.players = [Player(1, "a", 1000), Player(2, "b", 1000)]
        match.teams = [[match.players[0]], [match.players[1]]]
        match.selected_map = MAP_POOL[2]
        entry = manager.prepare(
            match, {1: "76561198000000001", 2: "76561198000000002"}, matchid="m7"
        )
        sent = await manager.load(entry)

        written = self.root / "csgo/addons/sourcemod/configs/get5" / entry.filename
        self.assertTrue(written.exists())
        self.assertIn("76561198000000001", written.read_text())
        self.assertEqual(
            self.srcds.commands[-1],
            f'get5_loadmatch "addons/sourcemod/configs/get5/{entry.filename}"',
        )
        self.assertIn(entry.filename, sent)

        # get5 writes the stats file into csgo/ -- the bot reads it back.
        (self.root / "csgo" / "get5_matchstats_m7.cfg").write_text(
            '"Stats" { "winner" "team2" "map0" { "winner" "team2" '
            '"team1" { "score" "7" } "team2" { "score" "16" } } }'
        )
        self.assertEqual(manager.stats_path("m7"), "/csgo/get5_matchstats_m7.cfg")
        self.assertEqual(await manager.fetch_stats_result(entry), (1, (7, 16)))
        await controller.rcon.close()

    # -------------------------------------------------------------- security

    async def test_an_unknown_host_key_is_refused(self):
        stranger = asyncssh.generate_private_key("ssh-ed25519")
        other = pathlib.Path(self._dir.name) / "wrong_known_hosts"
        other.write_text(
            f"[127.0.0.1]:{self.ssh_port} "
            f"{stranger.export_public_key().decode().strip()}\n"
        )
        link = self.make_link(known_hosts=str(other))
        with self.assertRaises(RemoteError):
            await SftpFiles(link).write("/x.cfg", "secret")
        self.assertFalse((self.root / "x.cfg").exists())
        client = self.rcon(link=link)
        with self.assertRaises(RconError):
            await client.execute("status")
        self.assertEqual(self.srcds.commands, [], "nothing reached RCON")
        await link.close()

    async def test_an_unreachable_host_fails_cleanly(self):
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        dead_port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()
        link = self.make_link(port=dead_port)
        with self.assertRaises(RemoteError):
            await SftpFiles(link).write("/x.cfg", "body")
        self.assertIsNone(await SftpFiles(link).read("/x.cfg"))
        await link.close()


if __name__ == "__main__":
    unittest.main()
