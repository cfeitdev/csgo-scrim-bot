"""Multi-server routing: a match's chosen region gets every server call.

The EU server is local (files on the bot's disk, direct RCON). NA is remote
(files over SFTP, RCON through the SSH link). These tests swap both servers'
RCON and NA's files for recorders and drive whole matches through them.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import discord

    from scrimbot import views
    from scrimbot.core import ScrimCore
    from scrimbot.session import ScrimSession

    HAS_DISCORD = True
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    HAS_DISCORD = False

from scrimbot.config import Config  # noqa: E402
from scrimbot.get5 import Get5Manager  # noqa: E402
from scrimbot.match import Match, Phase, Player  # noqa: E402
from scrimbot.rcon import RconClient, RconError  # noqa: E402
from scrimbot.regions import EU, NA, build_servers  # noqa: E402
from scrimbot.remote import LocalFiles, RemoteError, SftpFiles  # noqa: E402
from scrimbot.server import ServerController  # noqa: E402

NA_HOST = "203.0.113.20"
NA_CONNECT = "203.0.113.20:27045"
NA_DIR = "/home/csgo/server/csgo/addons/sourcemod/configs/get5"
STEAM = {101: "76561198000000101", 102: "76561198000000102"}


def make_config(store_path: str, **over) -> Config:
    base = dict(
        token="x", guild_id=None, rcon_host="127.0.1.1", rcon_port=27045,
        rcon_password="eu-pass", rcon_enabled=True,
        connect_address="198.51.100.10:27045", steam_api_key="", lobby_size=10,
        pick_timeout=None, store_path=store_path, default_elo=1000,
        default_mode="5v5", test_bot_delay=0.001,
        get5_load_delay=0.0, get5_load_timeout=1.0, get5_poll_interval=0.01,
        get5_autostart=False,
        na_ssh_host=NA_HOST, na_ssh_user="scrimbot", na_ssh_key="/k",
        na_ssh_known_hosts="/kh", na_rcon_port=27045, na_rcon_password="na-pass",
        na_connect_address=NA_CONNECT,
    )
    base.update(over)
    return Config(**base)


class RecordingRcon:
    """Answers like a CS:GO server with get5 loaded; remembers every command."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.commands: list[str] = []
        self.loaded = False

    async def execute(self, command: str, *, expect_response: bool = True) -> str:
        self.commands.append(command)
        if command.startswith("get5_loadmatch"):
            self.loaded = True
        if command.startswith("get5_endmatch"):
            self.loaded = False
        if command == "get5_game_state":
            return f'"get5_game_state" = "{1 if self.loaded else 0}" ( def. "0" )'
        if command == "status":
            return '# 7 1 "someone" STEAM_1:0:12345 05:00 40 0 active 786432\n'
        if command == "echo scrimbot_ping":
            return "scrimbot_ping"
        return ""

    async def close(self) -> None:
        return None


class MemoryFiles:
    """A remote filesystem held in a dict -- stands in for SFTP."""

    remote = True

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.discarded: list[str] = []

    @staticmethod
    def join(*parts):
        return posixpath.join(*parts)

    async def write(self, path, body):
        self.files[path] = body

    async def read(self, path):
        return self.files.get(path)

    def discard(self, path):
        self.discarded.append(path)
        self.files.pop(path, None)


STATS_BODY = """
"Stats"
{
    "winner"    "team1"
    "map0"
    {
        "winner"    "team1"
        "team1"
        {
            "score" "16"
            "76561198000000101" { "name" "alpha" "kills" "21" "deaths" "9" "roundsplayed" "27" "damage" "2400" }
        }
        "team2"
        {
            "score" "9"
            "76561198000000102" { "name" "bravo" "kills" "12" "deaths" "18" "roundsplayed" "27" "damage" "1500" }
        }
    }
}
"""


# ----------------------------------------------------------------- config


class ConfigTests(unittest.TestCase):
    def test_na_is_off_unless_its_ssh_host_is_set(self):
        config = make_config("s.json", na_ssh_host="")
        self.assertFalse(config.na_enabled)
        self.assertEqual(list(build_servers(config)), [EU])

    def test_for_na_swaps_every_server_facing_setting(self):
        config = make_config(
            "s.json",
            get5_base_url="http://bot:8080",
            get5_config_dir="/eu/get5",
            na_get5_config_dir=NA_DIR,
            na_get5_stats_dir="/home/csgo/server/csgo",
        )
        na = config.for_na()
        self.assertEqual(na.rcon_password, "na-pass")
        self.assertEqual(na.rcon_port, 27045)
        self.assertEqual(na.rcon_host, "127.0.0.1")
        self.assertEqual(na.connect_target, NA_CONNECT)
        self.assertEqual(na.get5_base_url, "", "NA is always file mode over SFTP")
        self.assertEqual(na.get5_config_dir, NA_DIR)
        self.assertEqual(na.get5_stats_dir, "/home/csgo/server/csgo")
        # ...and EU's own settings are untouched
        self.assertEqual(config.rcon_password, "eu-pass")
        self.assertEqual(config.get5_config_dir, "/eu/get5")

    def test_na_connect_address_defaults_to_the_public_host(self):
        na = make_config("s.json", na_connect_address="").for_na()
        self.assertEqual(na.connect_target, f"{NA_HOST}:27045")

    def test_validation_names_what_is_missing(self):
        config = make_config("s.json", na_ssh_key="", na_rcon_password="")
        with self.assertRaises(RuntimeError) as ctx:
            config.validate()
        self.assertIn("NA_SSH_KEY", str(ctx.exception))
        self.assertIn("NA_RCON_PASSWORD", str(ctx.exception))

    def test_get5_on_eu_requires_an_na_config_dir(self):
        config = make_config("s.json", get5_config_dir="/eu/get5")
        with self.assertRaises(RuntimeError) as ctx:
            config.validate()
        self.assertIn("NA_GET5_CONFIG_DIR", str(ctx.exception))
        make_config("s.json", get5_config_dir="/eu/get5", na_get5_config_dir=NA_DIR).validate()

    def test_eu_only_needs_nothing_new(self):
        make_config("s.json", na_ssh_host="", na_ssh_key="", na_rcon_password="").validate()


class BuildTests(unittest.TestCase):
    def test_na_gets_sftp_files_and_eu_keeps_local_ones(self):
        servers = build_servers(
            make_config("s.json", get5_config_dir="/eu", na_get5_config_dir=NA_DIR)
        )
        self.assertIsInstance(servers[EU].get5.files, LocalFiles)
        self.assertIsInstance(servers[NA].get5.files, SftpFiles)
        self.assertEqual(servers[NA].get5.mode, "file")
        self.assertIsNone(servers[EU].link)
        self.assertEqual(servers[NA].link.host, NA_HOST)

    def test_dry_run_reaches_neither_server(self):
        servers = build_servers(make_config("s.json", rcon_enabled=False))
        for server in servers.values():
            self.assertEqual(
                asyncio.run(server.server.execute("status")), "[dry run] status"
            )

    def test_remote_stats_paths_are_posix(self):
        servers = build_servers(
            make_config(
                "s.json", get5_config_dir="/eu", na_get5_config_dir=NA_DIR,
            )
        )
        path = servers[NA].get5.stats_path("m1")
        self.assertEqual(path, "/home/csgo/server/csgo/get5_matchstats_m1.cfg")


class RconConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unreachable_tunnel_reads_as_an_unreachable_server(self):
        async def refuse():
            raise RemoteError("SSH to the NA server failed")

        client = RconClient("127.0.0.1", 27045, "pw", timeout=1.0, connector=refuse)
        with self.assertRaises(RconError) as ctx:
            await client.execute("status")
        self.assertIn("SSH to the NA server failed", str(ctx.exception))

    async def test_the_connector_replaces_the_direct_connection(self):
        calls = []

        async def connector():
            calls.append(1)
            raise RemoteError("down")

        client = RconClient("never-dialled.invalid", 1, "pw", timeout=1.0, connector=connector)
        with self.assertRaises(RconError):
            await client.execute("status")
        self.assertTrue(calls)


class RemoteGet5Tests(unittest.IsolatedAsyncioTestCase):
    async def test_load_writes_through_the_files_layer(self):
        files = MemoryFiles()
        rcon = RecordingRcon("na")
        manager = Get5Manager(
            make_config("s.json", get5_config_dir=NA_DIR), ServerController(rcon), files=files
        )
        match = Match(host_id=1, size=2)
        match.players = [Player(101, "a", 1000), Player(102, "b", 1000)]
        match.teams = [[match.players[0]], [match.players[1]]]
        from scrimbot.maps import MAP_POOL

        match.selected_map = MAP_POOL[2]
        entry = manager.prepare(match, {101: STEAM[101], 102: STEAM[102]}, matchid="m1")
        sent = await manager.load(entry)
        self.assertIn(posixpath.join(NA_DIR, entry.filename), files.files)
        self.assertEqual(
            sent, f"get5_loadmatch addons/sourcemod/configs/get5/{entry.filename}"
        )
        manager.forget("m1")
        self.assertEqual(files.discarded, [posixpath.join(NA_DIR, entry.filename)])

    async def test_results_are_read_from_the_remote_stats_file(self):
        files = MemoryFiles()
        manager = Get5Manager(
            make_config("s.json", get5_config_dir=NA_DIR),
            ServerController(RecordingRcon("na")),
            files=files,
        )
        from scrimbot.get5 import Get5Match

        entry = Get5Match(matchid="m9", token="t", payload={})
        self.assertIsNone(await manager.fetch_stats_result(entry))
        files.files[manager.stats_path("m9")] = STATS_BODY
        self.assertEqual(await manager.fetch_stats_result(entry), (0, (16, 9)))
        teams = await manager.fetch_player_stats(entry)
        self.assertEqual(teams[0][0]["name"], "alpha")


# ---------------------------------------------------------------- sessions


class FakeMessage:
    jump_url = "https://discord.com/channels/1/2/3"

    def __init__(self, **sent) -> None:
        self.sent = sent
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeChannel:
    id = 55

    def __init__(self) -> None:
        self.guild = None
        self.lines: list[str] = []
        self.messages: list[FakeMessage] = []

    async def send(self, content=None, **kwargs):
        if content:
            self.lines.append(content)
        message = FakeMessage(content=content, **kwargs)
        self.messages.append(message)
        return message


class FakeUser:
    def __init__(self, user_id: int, admin: bool = False) -> None:
        self.id = user_id
        self.display_name = f"user{user_id}"
        self.mention = f"<@{user_id}>"
        self.guild_permissions = discord.Permissions(administrator=admin)
        self.roles = []


class FakeResponse:
    def __init__(self) -> None:
        self.done = False
        self.edits: list[dict] = []
        self.sent: list[tuple] = []

    def is_done(self) -> bool:
        return self.done

    async def edit_message(self, **kwargs):
        self.done = True
        self.edits.append(kwargs)

    async def send_message(self, content=None, **kwargs):
        self.done = True
        self.sent.append((content, kwargs))

    async def defer(self, **kwargs):
        self.done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class FakeInteraction:
    def __init__(self, user_id: int) -> None:
        self.user = FakeUser(user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class RoutingTests(unittest.IsolatedAsyncioTestCase):
    """A real ScrimCore and ScrimSession, with each server's wire recorded."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.eu_dir = os.path.join(self._dir.name, "csgo", "addons", "sourcemod", "configs", "get5")
        config = make_config(
            os.path.join(self._dir.name, "s.json"),
            get5_config_dir=self.eu_dir,
            na_get5_config_dir=NA_DIR,
        )
        self.core = ScrimCore(config)
        await self.core.setup()
        self.rcon = {}
        for key, target in self.core.servers.items():
            rcon = RecordingRcon(key)
            self.rcon[key] = rcon
            target.server = ServerController(rcon)
            target.get5.server = target.server
        self.na_files = MemoryFiles()
        self.core.servers[NA].get5.files = self.na_files
        for uid, steam in STEAM.items():
            await self.core.store.register(0, uid, steam, None)
        await self.core.store.set_setting(0, "server_cleanup", True)

        self.channel = FakeChannel()
        match = Match(
            host_id=101, size=2, pick_mode="random", regions=self.core.regions
        )
        match.add_player(Player(101, "alpha", 1000))
        match.add_player(Player(102, "bravo", 1000))
        self.session = ScrimSession(self.core, self.channel, match)
        self.session.message = FakeMessage()

    async def asyncTearDown(self):
        await self.session._teardown_get5()
        await self.core.close()
        self._dir.cleanup()

    async def play_to_live(self, region: str) -> None:
        match = self.session.match
        match.start_teams(random.Random(3))
        self.assertTrue(match.vote_open)
        match.vote_region(101, region)
        match.vote_region(102, region)
        self.assertEqual(match.region, region)
        while match.phase is Phase.MAP_BAN:
            match.ban(match.current_banner.id, match.remaining_maps[0].name)
        await self.session.go_live()

    def other(self, region: str) -> str:
        return EU if region == NA else NA

    async def test_an_na_match_runs_entirely_on_na(self):
        await self.play_to_live(NA)
        na = self.rcon[NA].commands
        game_map = self.session.match.selected_map.name
        self.assertIn(f"changelevel {game_map}", na)
        loads = [c for c in na if c.startswith("get5_loadmatch")]
        self.assertEqual(len(loads), 1)
        self.assertIn("addons/sourcemod/configs/get5/", loads[0])
        # the config went to NA's disk, not the bot's
        self.assertEqual(len(self.na_files.files), 1)
        self.assertTrue(next(iter(self.na_files.files)).startswith(NA_DIR + "/"))
        self.assertFalse(os.path.exists(self.eu_dir) and os.listdir(self.eu_dir))
        self.assertIn("Match config loaded", self.session.get5_status)

        # connect details point at NA
        self.assertEqual(self.session.connect_target, NA_CONNECT)
        interaction = FakeInteraction(101)
        await self.session.handle_connect_info(interaction)
        self.assertIn(f"connect {NA_CONNECT}", interaction.response.sent[0][0])

        # the result is read from NA's stats file, and NA is cleaned up
        entry = self.session._get5
        self.na_files.files[self.core.servers[NA].get5.stats_path(entry.matchid)] = STATS_BODY
        await self.session.finish(0)
        self.assertEqual(self.session.player_stats[0][0]["name"], "alpha")
        self.assertIn("get5_endmatch team1", na)
        self.assertIn('kickid 7 "Scrim finished"', na)
        self.assertIn(posixpath.join(NA_DIR, entry.filename), self.na_files.discarded)

        self.assertEqual(self.rcon[EU].commands, [], "EU was never touched")

    async def test_an_eu_match_runs_entirely_on_eu(self):
        await self.play_to_live(EU)
        eu = self.rcon[EU].commands
        self.assertIn(f"changelevel {self.session.match.selected_map.name}", eu)
        self.assertTrue(any(c.startswith("get5_loadmatch") for c in eu))
        self.assertEqual(len(os.listdir(self.eu_dir)), 1, "config written locally")
        self.assertEqual(self.session.connect_target, "198.51.100.10:27045")
        await self.session.cancel()
        self.assertIn("get5_endmatch", eu)
        self.assertEqual(self.rcon[NA].commands, [], "NA was never touched")
        self.assertEqual(self.na_files.files, {})

    async def test_a_sub_updates_the_roster_on_the_match_server(self):
        await self.play_to_live(NA)
        await self.core.store.register(0, 103, "76561198000000103", None)
        await self.session.substitute(102, FakeUser(103))
        na = self.rcon[NA].commands
        self.assertIn('get5_removeplayer "76561198000000102"', na)
        self.assertTrue(any(c.startswith('get5_addplayer "76561198000000103"') for c in na))
        self.assertEqual(self.rcon[EU].commands, [])

    async def test_redraft_tears_down_on_the_match_server(self):
        await self.play_to_live(NA)
        before = len(self.rcon[NA].commands)
        await self.session.redraft()
        self.assertIn("get5_endmatch", self.rcon[NA].commands[before:])
        self.assertEqual(self.session.match.region, NA, "the region survives a redraft")
        self.assertEqual(self.rcon[EU].commands, [])

    async def test_the_change_map_retry_goes_to_the_match_server(self):
        await self.play_to_live(NA)
        self.rcon[NA].commands.clear()
        await self.session.apply_map()
        self.assertIn(f"changelevel {self.session.match.selected_map.name}", self.rcon[NA].commands)
        self.assertEqual(self.rcon[EU].commands, [])

    async def test_na_addresses_are_scrubbed_from_channel_text(self):
        text = self.session._safe(
            f"Could not reach {NA_HOST}:22 or 127.0.0.1:27045 or 127.0.1.1:27045"
        )
        self.assertNotIn(NA_HOST, text)
        self.assertNotIn("127.0.0.1", text)
        self.assertNotIn("127.0.1.1", text)

    async def test_the_live_message_names_the_server(self):
        await self.play_to_live(NA)
        embed, _view = self.session._build()
        self.assertIn("NA", embed.description)

    async def test_without_a_vote_the_live_message_does_not_mention_a_server(self):
        self.session.match.regions = (EU,)
        self.session.match.start_teams(random.Random(3))
        embed, _view = self.session._build()
        self.assertIsNone(self.session.region_label)


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class VoteMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        config = make_config(os.path.join(self._dir.name, "s.json"), rcon_enabled=False)
        self.core = ScrimCore(config)
        await self.core.setup()
        self.channel = FakeChannel()
        self.match = Match(host_id=1, size=10, regions=self.core.regions)
        self.session = ScrimSession(self.core, self.channel, self.match)
        self.session.message = FakeMessage()

    async def asyncTearDown(self):
        await self.session._teardown_get5()
        await self.core.close()
        self._dir.cleanup()

    async def fill(self) -> None:
        for uid in range(1, 11):
            await self.session.force_join(FakeUser(uid))

    def vote_message(self) -> FakeMessage:
        return self.session.vote_message

    async def test_the_vote_is_posted_when_the_queue_fills(self):
        await self.fill()
        self.assertEqual(self.match.phase, Phase.DRAFT)
        message = self.vote_message()
        self.assertIsNotNone(message)
        self.assertIn("Server vote", message.sent["embed"].title)
        self.assertIsInstance(message.sent["view"], views.RegionVoteView)
        labels = [item.label for item in message.sent["view"].children]
        self.assertEqual(labels, ["EU", "NA"])

    async def test_it_is_not_posted_before_the_queue_fills(self):
        for uid in range(1, 10):
            await self.session.force_join(FakeUser(uid))
        self.assertIsNone(self.vote_message())

    async def test_the_vote_lives_apart_from_the_pick_clock(self):
        await self.fill()
        self.assertIsNot(self.session.view, self.session.vote_view)
        self.assertIsNone(self.session.vote_view.timeout)
        self.assertNotIn(
            views.RegionButton, [type(item) for item in self.session.view.children]
        )

    async def test_a_click_records_the_vote_and_confirms_privately(self):
        await self.fill()
        interaction = FakeInteraction(3)
        await self.session.handle_region_vote(interaction, NA)
        self.assertEqual(self.match.region_votes, {3: NA})
        embed = interaction.response.edits[0]["embed"]
        self.assertIn("1/10", embed.footer.text)
        confirmation, kwargs = interaction.followup.sent[0]
        self.assertIn("You voted", confirmation)
        self.assertTrue(kwargs.get("ephemeral"))

    async def test_outsiders_are_turned_away(self):
        await self.fill()
        interaction = FakeInteraction(999)
        await self.session.handle_region_vote(interaction, NA)
        self.assertEqual(self.match.region_votes, {})
        text, kwargs = interaction.response.sent[0]
        self.assertIn("Only players", text)
        self.assertTrue(kwargs.get("ephemeral"))

    async def test_the_tenth_vote_locks_it_and_says_so(self):
        await self.fill()
        for uid in range(1, 11):
            interaction = FakeInteraction(uid)
            await self.session.handle_region_vote(interaction, NA if uid <= 6 else EU)
        self.assertEqual(self.match.region, NA)
        self.assertIsNone(interaction.response.edits[-1]["view"], "buttons removed")
        self.assertIsNone(self.session.vote_view)
        self.assertTrue(any("playing on" in line and "NA" in line for line in self.channel.lines))

    async def test_an_admin_override_closes_it(self):
        await self.fill()
        await self.session.set_region(EU)
        self.assertEqual(self.match.region, EU)
        self.assertTrue(any("administrator" in line for line in self.channel.lines))
        last = self.vote_message().edits[-1]
        self.assertIsNone(last["view"])
        self.assertIn("EU", last["embed"].title)

    async def test_cancelling_mid_vote_closes_the_message(self):
        await self.fill()
        view = self.session.vote_view
        await self.session.cancel()
        self.assertTrue(view.is_finished())
        last = self.vote_message().edits[-1]
        self.assertIn("closed", last["embed"].title)
        self.assertIsNone(last["view"])

    async def test_testfill_bots_vote_by_themselves(self):
        added = await self.session.add_test_players(10)
        self.assertEqual(added, 10)
        self.assertFalse(self.match.vote_open)
        self.assertIn(self.match.region, (EU, NA))
        self.assertEqual(len(self.match.region_votes), 10)

    async def test_a_forcedrop_updates_the_tally(self):
        await self.fill()
        self.match.vote_region(4, NA)
        await self.session.force_drop(4)
        last = self.vote_message().edits[-1]
        self.assertIn("0/10", last["embed"].footer.text)

    async def test_the_veto_ending_closes_it_and_announces_the_server(self):
        await self.fill()
        self.match.vote_region(1, NA)
        while self.match.phase is Phase.DRAFT:
            self.match.pick(self.match.current_captain.id, self.match.available_players[0].id)
        while self.match.phase is Phase.MAP_BAN:
            self.match.ban(self.match.current_banner.id, self.match.remaining_maps[0].name)
        await self.session.go_live()
        self.assertEqual(self.match.region, NA)
        self.assertTrue(any("playing on" in line for line in self.channel.lines))
        self.assertIsNone(self.vote_message().edits[-1]["view"])


if __name__ == "__main__":
    unittest.main()
