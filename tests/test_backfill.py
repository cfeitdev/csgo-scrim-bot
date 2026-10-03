"""tools/fix_past_results.py -- repairing results the bot already posted."""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import discord

    from scrimbot.get5 import Get5Manager
    from tools.fix_past_results import (
        StatsIndex,
        is_result_embed,
        is_scoreboard_field,
        repair,
        team_of_field,
    )

    HAS_DISCORD = True
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    HAS_DISCORD = False

from test_get5 import make_config  # noqa: E402
from test_stats import TEAM1, TEAM2, stats_file, stub_map  # noqa: E402

GUILD, CHANNEL = 111, 222
ZWSP = "​"


def players():
    def row(name, kills):
        return {
            "steam_id": "1", "name": name, "kills": kills, "deaths": 15,
            "assists": 4, "damage": 2000, "headshots": 8, "rounds": 27,
        }

    return [[row("alpha", 20)], [row("bravo", 18)]]


def result_message(score="0 : 0", zeroed=True, game_map="de_season"):
    embed = discord.Embed(
        title="🟦 Team A wins",
        description=f"**Team A {score} Team B**  •  Map: `{game_map}`",
    )
    for team in (0, 1):
        embed.add_field(
            name=f"{'🟦🟥'[team]} Team {'AB'[team]}",
            value=f"<@{team + 1}> `1000 -> 1017`",
            inline=False,
        )
        if zeroed:
            embed.add_field(name=ZWSP, value="```\nx  0  0  0\n```", inline=False)
    return embed


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class FieldRecognitionTests(unittest.TestCase):
    def test_team_headings_map_to_teams(self):
        self.assertEqual(team_of_field("🟦 Team A"), 0)
        self.assertEqual(team_of_field("🟥 Team B"), 1)
        self.assertEqual(team_of_field("🟦 Team A (avg 1000)"), 0)
        self.assertIsNone(team_of_field("Queue"))

    def test_a_zero_width_heading_is_a_scoreboard_field(self):
        embed = result_message()
        self.assertTrue(is_scoreboard_field(embed.fields[1]))
        self.assertFalse(is_scoreboard_field(embed.fields[0]))

    def test_a_code_block_under_a_real_heading_is_not_a_scoreboard(self):
        embed = discord.Embed(title="x")
        embed.add_field(name="Players", value="```\nfoo\n```")
        self.assertFalse(is_scoreboard_field(embed.fields[0]))

    def test_result_embeds_need_both_teams(self):
        self.assertTrue(is_result_embed(result_message()))
        half = discord.Embed(title="x")
        half.add_field(name="🟦 Team A", value="a")
        self.assertFalse(is_result_embed(half))
        self.assertFalse(is_result_embed(discord.Embed(title="x")))

    def test_a_lobby_embed_is_not_a_result(self):
        lobby = discord.Embed(title="5v5 Scrim -- Queue")
        lobby.add_field(name="Queue", value="3 / 10")
        lobby.add_field(name="Players", value="```\n1 a\n```")
        self.assertFalse(is_result_embed(lobby))


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class RepairTests(unittest.TestCase):
    def test_the_score_is_rewritten_and_the_map_kept(self):
        fixed = repair(result_message(), (16, 12), players())
        self.assertIn("16 : 12", fixed.description)
        self.assertIn("`de_season`", fixed.description)

    def test_zeroed_tables_are_replaced_not_duplicated(self):
        fixed = repair(result_message(), (16, 12), players())
        self.assertEqual(len(fixed.fields), 4)
        tables = [f for f in fixed.fields if is_scoreboard_field(f)]
        self.assertEqual(len(tables), 2)
        self.assertIn("alpha", tables[0].value)
        self.assertIn("bravo", tables[1].value)

    def test_each_table_follows_its_own_team(self):
        fixed = repair(result_message(), (16, 12), players())
        names = [f.name for f in fixed.fields]
        self.assertEqual(team_of_field(names[0]), 0)
        self.assertIn("alpha", fixed.fields[1].value)
        self.assertEqual(team_of_field(names[2]), 1)
        self.assertIn("bravo", fixed.fields[3].value)

    def test_the_elo_lines_are_untouched(self):
        before = result_message()
        elo = [f.value for f in before.fields if team_of_field(f.name) is not None]
        fixed = repair(before, (16, 12), players())
        after = [f.value for f in fixed.fields if team_of_field(f.name) is not None]
        self.assertEqual(after, elo)

    def test_a_legacy_embed_with_no_table_gains_one(self):
        legacy = result_message(zeroed=False)
        legacy.description = "Map: `de_season`"
        fixed = repair(legacy, (16, 12), players())
        self.assertEqual(len(fixed.fields), 4)
        self.assertIn("16 : 12", fixed.description)

    def test_running_twice_changes_nothing(self):
        fixed = repair(result_message(), (16, 12), players())
        self.assertIsNone(repair(fixed, (16, 12), players()))

    def test_an_already_correct_message_is_left_alone(self):
        good = repair(result_message(), (16, 12), players())
        self.assertIsNone(repair(good, (16, 12), players()))

    def test_the_original_embed_is_not_mutated(self):
        before = result_message()
        count = len(before.fields)
        description = before.description
        repair(before, (16, 12), players())
        self.assertEqual(len(before.fields), count)
        self.assertEqual(before.description, description)


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class StatsIndexTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        prefix = "addons/sourcemod/configs/get5"
        self.csgo = os.path.join(self._dir.name, "csgo")
        config_dir = os.path.join(self.csgo, *prefix.split("/"))
        os.makedirs(config_dir)
        self.manager = Get5Manager(
            make_config(get5_config_dir=config_dir, get5_config_prefix=prefix), None
        )

    def tearDown(self):
        self._dir.cleanup()

    def write(self, channel, started, body):
        matchid = f"{GUILD}-{channel}-{started}"
        pathlib.Path(self.manager.stats_path(matchid)).write_text(body, encoding="utf-8")
        return matchid

    def index(self):
        return StatsIndex(self.manager)

    def test_files_are_grouped_by_channel(self):
        self.write(CHANNEL, 1000, stats_file())
        self.write(999, 1000, stats_file())
        index = self.index()
        self.assertEqual(index.total, 2)
        self.assertEqual(len(index.by_channel[CHANNEL]), 1)

    def test_candidates_are_nearest_first_and_inside_the_window(self):
        for started in (1000, 2000, 3000):
            self.write(CHANNEL, started, stats_file())
        found = self.index().candidates(CHANNEL, before=3500, window=2000)
        # 1000 is 2500 away, outside the window
        self.assertEqual(len(found), 2)
        self.assertIn("3000", found[0])
        self.assertIn("2000", found[1])

    def test_a_match_started_after_the_message_still_counts(self):
        """A host-channel result is an edit of the lobby message, so the match
        starts *after* the timestamp the message carries."""
        self.write(CHANNEL, 5000, stats_file())
        self.assertEqual(len(self.index().candidates(CHANNEL, before=4000, window=9999)), 1)

    def test_candidates_outside_the_window_are_dropped(self):
        self.write(CHANNEL, 50000, stats_file())
        self.assertEqual(self.index().candidates(CHANNEL, before=4000, window=100), [])

    def test_find_matches_on_the_map_name(self):
        self.write(CHANNEL, 1000, stats_file(mapname="de_season"))
        self.write(CHANNEL, 2000, stats_file(mapname="de_dust2"))
        hit = self.index().find(CHANNEL, message_time=3000, game_map="de_season", window=9999)
        self.assertIsNotNone(hit)
        path, result, _players = hit
        self.assertIn("1000", path)
        self.assertEqual(result, (0, (16, 11)))

    def test_find_skips_a_stub_only_file(self):
        body = "\n".join(['"Stats"', "{", stub_map(0), "}"])
        self.write(CHANNEL, 2000, body)
        self.write(CHANNEL, 1000, stats_file(mapname="de_season"))
        hit = self.index().find(CHANNEL, message_time=3000, game_map="de_season", window=9999)
        self.assertIsNotNone(hit)
        self.assertIn("1000", hit[0])

    def test_find_returns_none_when_nothing_matches(self):
        self.write(CHANNEL, 1000, stats_file(mapname="de_season"))
        self.assertIsNone(
            self.index().find(CHANNEL, message_time=3000, game_map="cs_office", window=9999)
        )

    def test_the_roster_beats_a_nearer_file_from_another_game(self):
        strangers = [(999_000_1, "x1", 1, 1, 1, 1, 1), (999_000_2, "x2", 1, 1, 1, 1, 1)]
        others = [(999_000_3, "x3", 1, 1, 1, 1, 1), (999_000_4, "x4", 1, 1, 1, 1, 1)]
        near = stats_file(mapname="de_season", team1=strangers, team2=others)
        self.write(CHANNEL, 3000, near)
        self.write(CHANNEL, 100000, stats_file(mapname="de_season"))
        roster = {str(row[0]) for row in TEAM1}
        hit = self.index().find(
            CHANNEL, message_time=3000, game_map="de_season", window=10,
            roster=roster, min_overlap=3,
        )
        self.assertIsNotNone(hit)
        self.assertIn("100000", hit[0])

    def test_a_file_from_another_channel_can_still_match_on_the_roster(self):
        self.write(999, 5000, stats_file(mapname="de_season"))
        roster = {str(row[0]) for row in TEAM1}
        hit = self.index().find(
            CHANNEL, message_time=5000, game_map="de_season", window=10,
            roster=roster, min_overlap=3,
        )
        self.assertIsNotNone(hit)

    def test_too_little_roster_overlap_is_refused(self):
        self.write(CHANNEL, 1000, stats_file(mapname="de_season"))
        hit = self.index().find(
            CHANNEL, message_time=1000, game_map="de_season", window=9999,
            roster={"76561190000000000"}, min_overlap=3,
        )
        self.assertIsNone(hit)

    def test_a_trailing_stub_map_does_not_win_over_the_played_one(self):
        body = stats_file(mapname="de_season")
        closing = body.rindex("}")
        self.write(CHANNEL, 1000, body[:closing] + stub_map(1) + "\n" + body[closing:])
        hit = self.index().find(CHANNEL, message_time=3000, game_map="de_season", window=9999)
        self.assertIsNotNone(hit)
        _path, result, roster = hit
        self.assertEqual(result, (0, (16, 11)))
        self.assertEqual(roster[0][0]["kills"], 28)


if __name__ == "__main__":
    unittest.main()
