"""get5 0.6.0 match config generation and the auto-start watcher."""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import pathlib
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrimbot.config import Config  # noqa: E402
from scrimbot.get5 import (  # noqa: E402
    Get5Error,
    Get5Manager,
    Get5Match,
    build_match_config,
    parse_keyvalues,
    to_keyvalues,
)
from scrimbot.match import Match, Phase, Player  # noqa: E402
from scrimbot.steam import to_steam2  # noqa: E402

BASE_ID = 76561197960265728


def steam64(n: int) -> str:
    return str(BASE_ID + n)


def make_config(**over) -> Config:
    base = dict(
        token="x", guild_id=None, rcon_host="h", rcon_port=27015, rcon_password="p",
        rcon_enabled=True, connect_address="", steam_api_key="", lobby_size=10,
        pick_timeout=120.0, store_path="", default_elo=1000, default_mode="5v5",
    )
    base.update(over)
    return Config(**base)


def finished_match(size: int = 10) -> Match:
    match = Match(host_id=1, size=size)
    for i in range(1, size + 1):
        match.add_player(Player(id=i, name=f"player{i}", elo=1000))
    match.start_teams(random.Random(4))
    while match.phase is Phase.DRAFT:
        match.pick(match.current_captain.id, match.available_players[0].id)
    while match.phase is Phase.MAP_BAN:
        match.ban(match.current_banner.id, match.remaining_maps[0].name)
    return match


def roster_ids(match: Match) -> dict[int, str]:
    return {player.id: steam64(player.id) for player in match.players}


class FakeServer:
    """Stands in for ServerController."""

    def __init__(self, connected=()) -> None:
        self.commands: list[str] = []
        self._connected = list(connected)

    async def execute(self, command: str, *, expect_response: bool = True) -> str:
        self.commands.append(command)
        return ""

    async def connected_players(self):
        from scrimbot.server import ConnectedPlayer

        return [
            ConnectedPlayer(i, f"p{i}", to_steam2(sid))
            for i, sid in enumerate(self._connected, start=1)
        ]

    def connect(self, steam_id: str) -> None:
        self._connected.append(steam_id)


class StatusServer(FakeServer):
    """Replays a scripted sequence of get5_status payloads."""

    def __init__(self, payloads, connected=()):
        super().__init__(connected)
        self.payloads = list(payloads)

    async def execute(self, command: str, *, expect_response: bool = True) -> str:
        self.commands.append(command)
        if command != "get5_status":
            return ""
        payload = self.payloads.pop(0) if self.payloads else self.payloads
        if isinstance(payload, str):
            return payload
        return "L 01/01/2020 - 00:00:00: " + json.dumps(payload)


def state(gamestate, s1=0, s2=0, series1=0, series2=0):
    return {
        "gamestate": gamestate,
        "team1": {"current_map_score": s1, "series_score": series1},
        "team2": {"current_map_score": s2, "series_score": series2},
    }


class MatchConfigTests(unittest.TestCase):
    def setUp(self):
        self.match = finished_match()

    def test_schema_matches_get5_0_6_0(self):
        config = build_match_config(
            self.match, roster_ids(self.match), matchid="scrim-1"
        )
        self.assertEqual(config["matchid"], "scrim-1")
        self.assertEqual(config["num_maps"], 1)
        self.assertEqual(config["players_per_team"], 5)
        self.assertEqual(config["min_players_to_ready"], 5)
        self.assertTrue(config["skip_veto"])
        self.assertEqual(config["maplist"], [self.match.selected_map.name])
        self.assertIn("team1", config)
        self.assertIn("team2", config)

    def test_veto_result_is_the_only_map(self):
        config = build_match_config(self.match, roster_ids(self.match), matchid="m")
        self.assertEqual(len(config["maplist"]), 1)
        self.assertTrue(config["skip_veto"], "the veto already happened in Discord")

    def test_players_are_keyed_by_steam_id(self):
        config = build_match_config(self.match, roster_ids(self.match), matchid="m")
        for team_key, roster in (("team1", self.match.teams[0]), ("team2", self.match.teams[1])):
            players = config[team_key]["players"]
            self.assertEqual(len(players), 5)
            for player in roster:
                self.assertIn(steam64(player.id), players)
                self.assertEqual(players[steam64(player.id)], player.name)

    def test_no_player_is_on_both_teams(self):
        config = build_match_config(self.match, roster_ids(self.match), matchid="m")
        first = set(config["team1"]["players"])
        second = set(config["team2"]["players"])
        self.assertEqual(first & second, set())
        self.assertEqual(len(first | second), 10)

    def test_check_auths_is_forced_on(self):
        # This is what pins players to their drafted team.
        config = build_match_config(self.match, roster_ids(self.match), matchid="m")
        self.assertEqual(config["cvars"]["get5_check_auths"], "1")

    def test_extra_cvars_are_merged(self):
        config = build_match_config(
            self.match, roster_ids(self.match), matchid="m", cvars={"sv_password": "abc"}
        )
        self.assertEqual(config["cvars"]["sv_password"], "abc")
        self.assertEqual(config["cvars"]["get5_check_auths"], "1")

    def test_unregistered_player_is_refused(self):
        ids = roster_ids(self.match)
        del ids[self.match.teams[1][2].id]
        with self.assertRaises(Get5Error):
            build_match_config(self.match, ids, matchid="m")

    def test_no_map_is_refused(self):
        match = Match(host_id=1)
        with self.assertRaises(Get5Error):
            build_match_config(match, {}, matchid="m")

    def test_config_is_json_serialisable(self):
        config = build_match_config(self.match, roster_ids(self.match), matchid="m")
        json.loads(json.dumps(config))


class KeyValuesTests(unittest.TestCase):
    """get5 0.6.0 reads JSON only with smjansson; KeyValues always works."""

    def setUp(self):
        self.match = finished_match()
        self.config = build_match_config(
            self.match, roster_ids(self.match), matchid="kv-1",
            cvars={"sv_password": "hunter2"},
        )
        self.text = to_keyvalues(self.config)

    def test_root_and_braces(self):
        self.assertTrue(self.text.startswith('"Match"'))
        self.assertEqual(self.text.count("{"), self.text.count("}"))

    def test_booleans_become_one_and_zero(self):
        self.assertIn('"skip_veto"		"1"', self.text)
        self.assertNotIn("True", self.text)
        self.assertNotIn("true", self.text)

    def test_maplist_is_a_block_of_empty_values(self):
        self.assertIn('"maplist"', self.text)
        self.assertIn(f'"{self.match.selected_map.name}"		""', self.text)

    def test_every_player_appears_under_their_team(self):
        for team_key, roster in (("team1", self.match.teams[0]), ("team2", self.match.teams[1])):
            self.assertIn(f'"{team_key}"', self.text)
            for player in roster:
                self.assertIn(steam64(player.id), self.text)

    def test_cvars_survive(self):
        self.assertIn('"get5_check_auths"		"1"', self.text)
        self.assertIn('"sv_password"		"hunter2"', self.text)

    def test_quotes_in_names_are_escaped(self):
        text = to_keyvalues({"team1": {"players": {"STEAM_1:0:1": 'a "b" c'}}})
        self.assertIn(chr(92) + chr(34) + "b" + chr(92) + chr(34), text)

    def test_format_defaults_per_mode(self):
        self.assertEqual(
            Get5Manager(make_config(get5_config_dir="/srv"), FakeServer()).config_format, "kv"
        )
        self.assertEqual(
            Get5Manager(make_config(get5_base_url="http://b"), FakeServer()).config_format, "json"
        )

    def test_format_can_be_forced(self):
        manager = Get5Manager(
            make_config(get5_config_dir="/srv", get5_config_format="json"), FakeServer()
        )
        self.assertEqual(manager.config_format, "json")
        entry = manager.prepare(self.match, roster_ids(self.match), matchid="x")
        self.assertTrue(entry.filename.endswith(".json"))
        json.loads(entry.dumps())

    def test_kv_entry_serialises_as_keyvalues(self):
        manager = Get5Manager(make_config(get5_config_dir="/srv"), FakeServer())
        entry = manager.prepare(self.match, roster_ids(self.match), matchid="x")
        self.assertEqual(entry.fmt, "kv")
        self.assertTrue(entry.dumps().startswith('"Match"'))


class ManagerModeTests(unittest.TestCase):
    def test_off_without_configuration(self):
        manager = Get5Manager(make_config(), FakeServer())
        self.assertFalse(manager.enabled)
        self.assertEqual(manager.mode, "off")

    def test_url_mode(self):
        manager = Get5Manager(make_config(get5_base_url="http://bot:8080"), FakeServer())
        self.assertEqual(manager.mode, "url")
        self.assertTrue(manager.enabled)

    def test_file_mode(self):
        manager = Get5Manager(make_config(get5_config_dir="/srv/get5"), FakeServer())
        self.assertEqual(manager.mode, "file")

    def test_url_wins_when_both_are_set(self):
        manager = Get5Manager(
            make_config(get5_base_url="http://bot:8080", get5_config_dir="/srv"), FakeServer()
        )
        self.assertEqual(manager.mode, "url")


class LoadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.match = finished_match()

    async def test_url_mode_sends_loadmatch_url(self):
        server = FakeServer()
        manager = Get5Manager(make_config(get5_base_url="http://bot:8080/"), server)
        entry = manager.prepare(self.match, roster_ids(self.match), matchid="abc")
        await manager.load(entry)
        self.assertEqual(len(server.commands), 1)
        command = server.commands[0]
        self.assertTrue(command.startswith("get5_loadmatch_url "))
        self.assertIn(f"http://bot:8080/match/abc-{entry.token}.json", command)

    async def test_url_mode_passes_the_auth_header(self):
        server = FakeServer()
        manager = Get5Manager(
            make_config(
                get5_base_url="http://bot:8080",
                get5_auth_header="Authorization",
                get5_auth_value="Bearer hunter2",
            ),
            server,
        )
        entry = manager.prepare(self.match, roster_ids(self.match), matchid="abc")
        await manager.load(entry)
        self.assertIn('"Authorization" "Bearer hunter2"', server.commands[0])

    async def test_file_mode_writes_the_config_and_uses_a_server_relative_path(self):
        with tempfile.TemporaryDirectory() as directory:
            server = FakeServer()
            manager = Get5Manager(
                make_config(get5_config_dir=directory, get5_config_prefix="cfg/get5"), server
            )
            entry = manager.prepare(self.match, roster_ids(self.match), matchid="abc")
            await manager.load(entry)

            written = os.path.join(directory, entry.filename)
            self.assertTrue(os.path.exists(written))
            # File mode defaults to KeyValues, which needs no SourceMod extension.
            self.assertTrue(entry.filename.endswith(".cfg"))
            body = pathlib.Path(written).read_text(encoding="utf-8")
            self.assertTrue(body.lstrip().startswith('"Match"'))
            self.assertEqual(
                server.commands[0], f'get5_loadmatch "cfg/get5/{entry.filename}"'
            )

    async def test_forget_removes_the_written_file(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = Get5Manager(make_config(get5_config_dir=directory), FakeServer())
            entry = manager.prepare(self.match, roster_ids(self.match), matchid="abc")
            await manager.load(entry)
            manager.forget("abc")
            self.assertFalse(os.path.exists(os.path.join(directory, entry.filename)))

    async def test_force_start_and_end_match(self):
        server = FakeServer()
        manager = Get5Manager(make_config(get5_base_url="http://bot"), server)
        await manager.force_start()
        await manager.end_match(0)
        await manager.end_match(1)
        await manager.end_match(None)
        self.assertEqual(
            server.commands,
            ["get5_forceready", "get5_endmatch team1", "get5_endmatch team2", "get5_endmatch"],
        )


REAL_CVAR_REPLY = (
    '"get5_game_state" = "3" ( def. "0" )      - Current game state (see get5.inc)\n'
    'L 08/28/2026 - 20:45:33: rcon from "127.0.0.1:35334": command "get5_game_state"'
)


class CvarServer(FakeServer):
    """Answers get5_game_state like a real server; get5_status stays unparseable."""

    def __init__(self, reply):
        super().__init__()
        self.reply = reply

    async def execute(self, command, *, expect_response=True):
        self.commands.append(command)
        if command == "get5_game_state":
            return self.reply
        if command == "get5_status":
            return "get5_status requires the smjansson extension to be loaded"
        return ""


class GameStateCvarTests(unittest.IsolatedAsyncioTestCase):
    """get5_game_state is a plain cvar, so it works without smjansson."""

    def setUp(self):
        self.match = finished_match()
        self.entry = Get5Match(
            matchid="probe-1", token="t", payload={}, steam_ids=set(), fmt="kv"
        )

    def manager(self, reply):
        return Get5Manager(make_config(get5_config_dir="/srv"), CvarServer(reply))

    async def test_parses_a_real_rcon_cvar_reply(self):
        self.assertEqual(await self.manager(REAL_CVAR_REPLY).game_state(), 3)

    async def test_parses_a_bare_number(self):
        self.assertEqual(await self.manager("3").game_state(), 3)

    async def test_unparseable_reply_is_none(self):
        self.assertIsNone(await self.manager("Unknown command").game_state())

    async def test_verification_works_without_smjansson(self):
        # This is the whole point: get5_status is unavailable here.
        manager = self.manager(REAL_CVAR_REPLY)
        self.assertTrue(await manager.verify_loaded(self.entry, attempts=1))

    async def test_idle_server_fails_verification(self):
        manager = self.manager('"get5_game_state" = "0" ( def. "0" )')
        self.assertFalse(await manager.verify_loaded(self.entry, attempts=2))

    async def test_undecided_when_neither_source_answers(self):
        manager = self.manager("Unknown command")
        self.assertIsNone(await manager.verify_loaded(self.entry, attempts=1))

    async def test_ensure_idle_ends_a_live_match_without_smjansson(self):
        manager = self.manager('"get5_game_state" = "7" ( def. "0" )')
        await manager.ensure_idle()
        self.assertIn("get5_endmatch", manager.server.commands)

    async def test_ensure_idle_leaves_an_idle_server_alone(self):
        manager = self.manager('"get5_game_state" = "0" ( def. "0" )')
        await manager.ensure_idle()
        self.assertNotIn("get5_endmatch", manager.server.commands)


class RosterSwapTests(unittest.IsolatedAsyncioTestCase):
    """/sub must be pushed to a match get5 has already loaded."""

    def setUp(self):
        self.match = finished_match()
        self.ids = roster_ids(self.match)
        self.server = FakeServer()
        self.manager = Get5Manager(make_config(get5_config_dir="/srv"), self.server)
        self.entry = self.manager.prepare(self.match, self.ids, matchid="abc")
        self.victim = self.match.teams[1][2]
        self.out_id = self.ids[self.victim.id]
        self.in_id = steam64(9999)

    async def swap(self, team=1, name="subguy"):
        await self.manager.swap_player(
            self.entry, out_steam_id=self.out_id, in_steam_id=self.in_id,
            team=team, name=name,
        )

    async def test_sends_remove_then_add(self):
        await self.swap()
        self.assertEqual(
            self.server.commands,
            [f'get5_removeplayer "{self.out_id}"',
             f'get5_addplayer "{self.in_id}" team2 "subguy"'],
        )

    async def test_team_index_maps_to_get5_team_number(self):
        await self.swap(team=0)
        self.assertIn(f'get5_addplayer "{self.in_id}" team1 "subguy"', self.server.commands)

    async def test_stored_config_is_updated(self):
        await self.swap()
        players = self.entry.payload["team2"]["players"]
        self.assertNotIn(self.out_id, players)
        self.assertEqual(players[self.in_id], "subguy")
        self.assertNotIn(self.in_id, self.entry.payload["team1"]["players"])

    async def test_roster_watch_follows_the_swap(self):
        # Otherwise auto-start waits forever for the player who left.
        before = len(self.entry.steam_ids)
        await self.swap()
        self.assertIn(self.in_id, self.entry.steam_ids)
        self.assertNotIn(self.out_id, self.entry.steam_ids)
        self.assertEqual(len(self.entry.steam_ids), before)

    async def test_adding_without_an_outgoing_player(self):
        await self.manager.swap_player(
            self.entry, out_steam_id=None, in_steam_id=self.in_id, team=1, name=None
        )
        self.assertEqual(self.server.commands, [f'get5_addplayer "{self.in_id}" team2'])
        self.assertIn(self.in_id, self.entry.steam_ids)

    async def test_long_names_are_truncated(self):
        await self.swap(name="x" * 80)
        added = [c for c in self.server.commands if "addplayer" in c][0]
        self.assertIn('"' + "x" * 32 + '"', added)


STATS_FILE = """"Stats"
{
	"series_type"		"bo1"
	"team1_name"		"Team A"
	"team2_name"		"Team B"
	"winner"		"team2"
	"map0"
	{
		"mapname"		"de_season"
		"winner"		"team2"
		"demo_filename"	"scrim_map0_de_season.dem"
		"team1"
		{
			"score"		"11"
		}
		"team2"
		{
			"score"		"16"
		}
	}
}"""


class KeyValuesParserTests(unittest.TestCase):
    def test_nested_structure(self):
        kv = to_keyvalues({"a": "1", "b": {"c": "2"}})
        back = parse_keyvalues(kv)
        self.assertEqual(back, {"a": "1", "b": {"c": "2"}})

    def test_reads_a_real_get5_stats_file(self):
        stats = parse_keyvalues(STATS_FILE)
        self.assertEqual(stats["winner"], "team2")
        self.assertEqual(stats["map0"]["team1"]["score"], "11")
        self.assertEqual(stats["map0"]["team2"]["score"], "16")
        self.assertEqual(stats["map0"]["mapname"], "de_season")

    def test_comments_are_skipped(self):
        text = '"R"\n{\n"a" "1" // note\n}'
        self.assertEqual(parse_keyvalues(text), {"a": "1"})

    def test_escaped_quotes_round_trip(self):
        text = to_keyvalues({"team1": {"players": {"STEAM_1:0:1": 'a "b" c'}}})
        self.assertEqual(
            parse_keyvalues(text)["team1"]["players"]["STEAM_1:0:1"], 'a "b" c'
        )


class StatsResultTests(unittest.TestCase):
    """The final score is read off disk when smjansson is absent."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.csgo = os.path.join(self._dir.name, "csgo")
        self.prefix = "addons/sourcemod/configs/get5"
        os.makedirs(os.path.join(self.csgo, *self.prefix.split("/")))
        self.manager = Get5Manager(
            make_config(
                get5_config_dir=os.path.join(self.csgo, *self.prefix.split("/")),
                get5_config_prefix=self.prefix,
            ),
            FakeServer(),
        )
        self.entry = Get5Match(
            matchid="scrim-1", token="t", payload={}, steam_ids=set(), fmt="kv"
        )

    def tearDown(self):
        self._dir.cleanup()

    def write(self, body, matchid="scrim-1"):
        pathlib.Path(self.manager.stats_path(matchid)).write_text(body, encoding="utf-8")

    def test_stats_dir_is_derived_from_the_config_dir(self):
        self.assertEqual(
            self.manager.stats_dir.rstrip("/"), self.csgo.replace(os.sep, "/").rstrip("/")
        )

    def test_stats_path_uses_the_matchid(self):
        self.assertTrue(
            self.manager.stats_path("abc").endswith("get5_matchstats_abc.cfg")
        )

    def test_reads_winner_and_score(self):
        self.write(STATS_FILE)
        self.assertEqual(self.manager.read_stats_result(self.entry), (1, (11, 16)))

    def test_team1_win(self):
        self.write(STATS_FILE.replace('"winner"		"team2"', '"winner"		"team1"'))
        winner, scores = self.manager.read_stats_result(self.entry)
        self.assertEqual(winner, 0)
        self.assertEqual(scores, (11, 16))

    def test_none_winner_is_a_draw(self):
        self.write(STATS_FILE.replace('"winner"		"team2"', '"winner"		"none"'))
        self.assertEqual(self.manager.read_stats_result(self.entry), (None, (11, 16)))

    def test_missing_file_returns_none(self):
        self.assertIsNone(self.manager.read_stats_result(self.entry))

    def test_falls_back_to_the_score_when_no_winner_key(self):
        body = "\n".join(
            line for line in STATS_FILE.splitlines() if '"winner"' not in line
        )
        self.write(body)
        winner, scores = self.manager.read_stats_result(self.entry)
        self.assertEqual((winner, scores), (1, (11, 16)))

    def test_an_explicit_stats_dir_wins(self):
        manager = Get5Manager(
            make_config(get5_config_dir="/a/b", get5_stats_dir="/somewhere/else"),
            FakeServer(),
        )
        self.assertEqual(manager.stats_dir, "/somewhere/else")


class LoadVerificationTests(unittest.IsolatedAsyncioTestCase):
    """A refused load must never be reported as success."""

    def setUp(self):
        self.match = finished_match()
        self.ids = roster_ids(self.match)

    def manager(self, server=None, **over):
        return Get5Manager(
            make_config(get5_base_url="http://bot:8080", **over), server or FakeServer()
        )

    def entry(self, manager):
        return manager.prepare(self.match, self.ids, matchid="abc")

    async def test_refusals_raise(self):
        manager = self.manager()
        for reply in (
            "Cannot load a match when a match is already loaded",
            "Cannot load a match config with another match already loaded",
            "Cannot load matches from a url without the SteamWorks extension running",
            "Failed to load match config.",
            "Usage: get5_loadmatch <filename>",
        ):
            with self.subTest(reply=reply):
                with self.assertRaises(Get5Error):
                    manager._check_reply(reply)

    async def test_quiet_reply_is_success(self):
        manager = self.manager()
        manager._check_reply("")
        manager._check_reply("Loaded match config.")

    async def test_load_raises_when_the_server_refuses(self):
        class Refusing(FakeServer):
            async def execute(self, command, *, expect_response=True):
                self.commands.append(command)
                return "Failed to load match config."

        manager = self.manager(Refusing())
        with self.assertRaises(Get5Error):
            await manager.load(self.entry(manager))

    async def test_verify_confirms_a_loaded_match(self):
        server = StatusServer([state(3)])
        server.payloads[0]["matchid"] = "abc"
        manager = self.manager(server)
        self.assertTrue(await manager.verify_loaded(self.entry(manager), attempts=1))

    async def test_verify_rejects_a_server_still_idle(self):
        manager = self.manager(StatusServer([state(0), state(0)]))
        self.assertFalse(await manager.verify_loaded(self.entry(manager), attempts=2))

    async def test_verify_is_undecided_without_smjansson(self):
        manager = self.manager(StatusServer(["get5_status requires the smjansson extension"]))
        self.assertIsNone(await manager.verify_loaded(self.entry(manager), attempts=1))

    async def test_verify_notices_a_different_match(self):
        server = StatusServer([state(3)])
        server.payloads[0]["matchid"] = "somebody-elses-match"
        manager = self.manager(server)
        with self.assertRaises(Get5Error):
            await manager.verify_loaded(self.entry(manager), attempts=1)

    async def test_ensure_idle_ends_a_stale_match(self):
        server = StatusServer([state(7)])
        manager = self.manager(server)
        await manager.ensure_idle()
        self.assertIn("get5_endmatch", server.commands)

    async def test_ensure_idle_leaves_a_clean_server_alone(self):
        server = StatusServer([state(0)])
        manager = self.manager(server)
        await manager.ensure_idle()
        self.assertNotIn("get5_endmatch", server.commands)


class GameStateCvarTests(unittest.IsolatedAsyncioTestCase):
    """get5_game_state is a plain cvar, readable without smjansson."""

    CVAR_REPLY = (
        '"get5_game_state" = "3" ( def. "0" )'
        "                    - Current game state (see get5.inc)"
    )

    def setUp(self):
        self.match = finished_match()
        self.ids = roster_ids(self.match)

    def server(self, reply):
        class Cvar(FakeServer):
            async def execute(inner, command, *, expect_response=True):
                inner.commands.append(command)
                if command == "get5_game_state":
                    return reply
                if command == "get5_status":
                    return ""  # smjansson missing
                return ""

        return Cvar()

    def manager(self, server):
        return Get5Manager(make_config(get5_config_dir="/srv"), server)

    async def test_parses_the_real_rcon_reply(self):
        manager = self.manager(self.server(self.CVAR_REPLY))
        self.assertEqual(await manager.game_state(), 3)

    async def test_parses_an_idle_server(self):
        manager = self.manager(self.server('"get5_game_state" = "0" ( def. "0" )'))
        self.assertEqual(await manager.game_state(), 0)

    async def test_unparseable_reply_is_none(self):
        manager = self.manager(self.server("Unknown command"))
        self.assertIsNone(await manager.game_state())

    async def test_verify_uses_the_cvar_when_status_is_unavailable(self):
        # This is the case on a stock SourceMod: no smjansson, so get5_status
        # returns nothing, but the cvar still proves the match loaded.
        manager = self.manager(self.server(self.CVAR_REPLY))
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        self.assertTrue(await manager.verify_loaded(entry, attempts=1))

    async def test_verify_fails_when_the_server_stays_idle(self):
        manager = self.manager(self.server('"get5_game_state" = "0" ( def. "0" )'))
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        self.assertFalse(await manager.verify_loaded(entry, attempts=2))

    async def test_verify_is_undecided_with_no_cvar_and_no_status(self):
        manager = self.manager(self.server("Unknown command"))
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        self.assertIsNone(await manager.verify_loaded(entry, attempts=2))

    async def test_ensure_idle_ends_a_match_found_via_the_cvar(self):
        server = self.server(self.CVAR_REPLY)
        await self.manager(server).ensure_idle()
        self.assertIn("get5_endmatch", server.commands)

    async def test_ensure_idle_leaves_an_idle_server_alone(self):
        server = self.server('"get5_game_state" = "0" ( def. "0" )')
        await self.manager(server).ensure_idle()
        self.assertNotIn("get5_endmatch", server.commands)


class RosterWatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.match = finished_match()
        self.ids = roster_ids(self.match)

    def manager(self, server, **over):
        config = make_config(
            get5_base_url="http://bot:8080",
            get5_poll_interval=0.01,
            get5_ready_timeout=0.5,
            **over,
        )
        return Get5Manager(config, server)

    async def test_status_ids_are_normalised_to_steam64(self):
        server = FakeServer(connected=[steam64(1), steam64(2)])
        manager = self.manager(server)
        self.assertEqual(
            await manager.connected_steam_ids(), {steam64(1), steam64(2)}
        )

    async def test_waits_until_the_full_roster_is_present(self):
        server = FakeServer(connected=[steam64(i) for i in range(1, 10)])
        manager = self.manager(server)
        entry = manager.prepare(self.match, self.ids, matchid="abc")

        progress: list[tuple[int, int]] = []

        async def on_progress(present, missing, connected, empty_polls):
            progress.append((len(present), len(present) + len(missing)))

        async def late_joiner():
            await asyncio.sleep(0.05)
            server.connect(steam64(10))

        asyncio.ensure_future(late_joiner())
        complete = await manager.wait_for_roster(entry, on_progress=on_progress)

        self.assertTrue(complete)
        self.assertEqual(progress[0], (9, 10))
        self.assertEqual(progress[-1], (10, 10))

    async def test_times_out_when_players_never_show(self):
        server = FakeServer(connected=[steam64(1)])
        manager = self.manager(server)
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        self.assertFalse(await manager.wait_for_roster(entry))

    async def test_strangers_do_not_count_toward_the_roster(self):
        outsiders = [steam64(i) for i in range(1, 10)] + [steam64(9999)]
        manager = self.manager(FakeServer(connected=outsiders))
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        self.assertFalse(await manager.wait_for_roster(entry))

    async def test_progress_names_who_is_missing(self):
        server = FakeServer(connected=[steam64(i) for i in range(1, 10)])
        manager = self.manager(server)
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        seen = {}

        async def on_progress(present, missing, connected, empty_polls):
            seen["missing"] = {entry.name_for(sid) for sid in missing}
            seen["strangers"] = connected - entry.steam_ids

        await manager.wait_for_roster(entry, on_progress=on_progress)
        # player10 is the one who never joined
        self.assertEqual(seen["missing"], {"player10"})
        self.assertEqual(seen["strangers"], set())

    async def test_progress_flags_players_who_are_not_on_the_roster(self):
        # Somebody connected on a Steam account that was never registered.
        outsider = steam64(4242)
        server = FakeServer(connected=[steam64(i) for i in range(1, 10)] + [outsider])
        manager = self.manager(server)
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        seen = {}

        async def on_progress(present, missing, connected, empty_polls):
            seen["strangers"] = connected - entry.steam_ids

        await manager.wait_for_roster(entry, on_progress=on_progress)
        self.assertEqual(seen["strangers"], {outsider})

    async def test_an_empty_server_is_reported_after_three_polls(self):
        manager = self.manager(FakeServer(connected=[]))
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        empties = []

        async def on_progress(present, missing, connected, empty_polls):
            empties.append(empty_polls)

        await manager.wait_for_roster(entry, on_progress=on_progress)
        self.assertIn(3, empties)

    async def test_a_failing_poll_does_not_end_the_wait(self):
        class Flaky(FakeServer):
            def __init__(self):
                super().__init__(connected=[steam64(i) for i in range(1, 11)])
                self.calls = 0

            async def connected_players(self):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("rcon hiccup")
                return await super().connected_players()

        server = Flaky()
        manager = self.manager(server)
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        self.assertTrue(await manager.wait_for_roster(entry))
        self.assertGreater(server.calls, 1)


class ResultWatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.match = finished_match()
        self.ids = roster_ids(self.match)

    def manager(self, server):
        return Get5Manager(
            make_config(
                get5_base_url="http://bot",
                get5_poll_interval=0.01,
                get5_match_timeout=1.0,
            ),
            server,
        )

    def entry(self, manager):
        return manager.prepare(self.match, self.ids, matchid="abc")

    async def test_team1_win_is_detected(self):
        server = StatusServer([
            state(7, 5, 3), state(7, 15, 9), state(8, 16, 9, series1=1)
        ])
        manager = self.manager(server)
        winner, scores = await manager.wait_for_result(self.entry(manager))
        self.assertEqual(winner, 0)
        self.assertEqual(scores, (16, 9))

    async def test_team2_win_is_detected(self):
        server = StatusServer([state(7, 4, 9), state(8, 11, 16, series2=1)])
        manager = self.manager(server)
        winner, scores = await manager.wait_for_result(self.entry(manager))
        self.assertEqual(winner, 1)
        self.assertEqual(scores, (11, 16))

    async def test_draw_is_detected(self):
        server = StatusServer([state(7, 10, 10), state(8, 15, 15)])
        manager = self.manager(server)
        winner, scores = await manager.wait_for_result(self.entry(manager))
        self.assertIsNone(winner)
        self.assertEqual(scores, (15, 15))

    async def test_score_updates_are_reported(self):
        server = StatusServer([state(7, 1, 0), state(7, 1, 0), state(7, 5, 2), state(8, 16, 2, series1=1)])
        manager = self.manager(server)
        seen = []

        async def on_update(scores):
            seen.append(scores)

        await manager.wait_for_result(self.entry(manager), on_update=on_update)
        self.assertEqual(seen[0], (1, 0))
        self.assertIn((5, 2), seen)
        # unchanged score does not re-report
        self.assertEqual(len(seen), len(set(seen)))

    async def test_reset_to_none_after_live_still_resolves(self):
        # get5 wipes state back to None once the post-game window closes.
        server = StatusServer([state(7, 16, 12), state(0)])
        manager = self.manager(server)
        winner, scores = await manager.wait_for_result(self.entry(manager))
        self.assertEqual(winner, 0)
        self.assertEqual(scores, (16, 12))

    async def test_warmup_alone_never_resolves(self):
        server = StatusServer([state(3) for _ in range(200)])
        manager = self.manager(server)
        self.assertIsNone(await manager.wait_for_result(self.entry(manager)))

    async def test_non_json_reply_bails_out(self):
        server = StatusServer(["get5_status requires the smjansson extension to be loaded"])
        manager = self.manager(server)
        self.assertIsNone(await manager.wait_for_result(self.entry(manager)))

    async def test_the_cvar_decides_when_status_has_no_gamestate(self):
        """A status payload we cannot read must not stall the watcher.

        Loading smjansson makes get5_status answer, which would otherwise
        take the cvar out of the loop entirely.
        """

        class Mixed(StatusServer):
            def __init__(self, payloads, states):
                super().__init__(payloads)
                self.states = list(states)

            async def execute(self, command, *, expect_response=True):
                if command == "get5_game_state":
                    self.commands.append(command)
                    value = self.states.pop(0) if self.states else 8
                    return f'"get5_game_state" = "{value}" ( def. "0" )'
                return await super().execute(command, expect_response=expect_response)

        scores = {
            "team1": {"current_map_score": 16, "series_score": 1},
            "team2": {"current_map_score": 12, "series_score": 0},
        }
        server = Mixed([dict(scores), dict(scores)], [7, 8])
        manager = self.manager(server)
        winner, final = await manager.wait_for_result(self.entry(manager))
        self.assertEqual((winner, final), (0, (16, 12)))
        self.assertIn("get5_game_state", server.commands)

    async def test_live_scores_still_tick_when_the_cvar_supplies_the_state(self):
        class Mixed(StatusServer):
            async def execute(self, command, *, expect_response=True):
                if command == "get5_game_state":
                    self.commands.append(command)
                    return '"get5_game_state" = "7" ( def. "0" )'
                return await super().execute(command, expect_response=expect_response)

        def payload(s1, s2):
            return {
                "team1": {"current_map_score": s1, "series_score": 0},
                "team2": {"current_map_score": s2, "series_score": 0},
            }

        server = Mixed([payload(1, 0), payload(4, 2)])
        manager = self.manager(server)
        seen = []

        async def on_update(value):
            seen.append(value)

        await manager.wait_for_result(self.entry(manager), on_update=on_update)
        self.assertIn((1, 0), seen)
        self.assertIn((4, 2), seen)

    async def test_status_parses_json_out_of_a_noisy_reply(self):
        server = StatusServer([state(7, 3, 1)])
        manager = self.manager(server)
        payload = await manager.status()
        self.assertEqual(payload["gamestate"], 7)


if __name__ == "__main__":
    unittest.main()
