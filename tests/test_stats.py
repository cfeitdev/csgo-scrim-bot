"""Per-player stats: reading get5's stats file and rendering the scoreboard."""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scrimbot import embeds  # noqa: E402
from scrimbot.get5 import Get5Manager, Get5Match  # noqa: E402

from test_get5 import FakeServer, make_config  # noqa: E402

BASE = 76561197960265728
T1, T2, T3, T4 = "	", "		", "			", "				"


def player_block(steam_id, name, kills, deaths, assists, damage, headshots, rounds=27):
    return "\n".join(
        [
            f'\t\t\t"{steam_id}"',
            "\t\t\t{",
            f'\t\t\t\t"name"\t\t"{name}"',
            f'\t\t\t\t"kills"\t\t"{kills}"',
            f'\t\t\t\t"deaths"\t\t"{deaths}"',
            f'\t\t\t\t"assists"\t\t"{assists}"',
            f'\t\t\t\t"damage"\t\t"{damage}"',
            f'\t\t\t\t"headshot_kills"\t\t"{headshots}"',
            f'\t\t\t\t"roundsplayed"\t\t"{rounds}"',
            "\t\t\t}",
        ]
    )


TEAM1 = [
    (BASE + 1, "nitro", 28, 17, 4, 2611, 12),
    (BASE + 2, "hazed", 22, 19, 7, 2050, 6),
    (BASE + 3, "b0ngo", 19, 20, 3, 1788, 9),
]
TEAM2 = [
    (BASE + 6, "zeus", 25, 18, 5, 2333, 11),
    (BASE + 7, "f0rest", 21, 19, 4, 1975, 8),
    (BASE + 8, "a-very-long-player-name", 18, 20, 8, 1690, 7),
]


def stats_file(team1=TEAM1, team2=TEAM2, mapname="de_season", winner="team1"):
    def team(rows, score):
        body = [f'\t\t\t"score"\t\t"{score}"']
        body += [player_block(*row) for row in rows]
        return "\n".join(body)

    return "\n".join(
        [
            '"Stats"',
            "{",
            f'\t"winner"\t\t"{winner}"',
            '\t"map0"',
            "\t{",
            f'\t\t"mapname"\t\t"{mapname}"',
            f'\t\t"winner"\t\t"{winner}"',
            '\t\t"team1"',
            "\t\t{",
            team(team1, 16),
            "\t\t}",
            '\t\t"team2"',
            "\t\t{",
            team(team2, 11),
            "\t\t}",
            "\t}",
            "}",
        ]
    )


class ReadPlayerStatsTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        prefix = "addons/sourcemod/configs/get5"
        csgo = os.path.join(self._dir.name, "csgo")
        config_dir = os.path.join(csgo, *prefix.split("/"))
        os.makedirs(config_dir)
        self.manager = Get5Manager(
            make_config(get5_config_dir=config_dir, get5_config_prefix=prefix),
            FakeServer(),
        )
        self.entry = Get5Match(
            matchid="scrim-1", token="t", payload={}, steam_ids=set(), fmt="kv"
        )

    def tearDown(self):
        self._dir.cleanup()

    def write(self, body):
        pathlib.Path(self.manager.stats_path("scrim-1")).write_text(
            body, encoding="utf-8"
        )

    def test_both_teams_are_returned(self):
        self.write(stats_file())
        teams = self.manager.read_player_stats(self.entry)
        self.assertEqual([len(t) for t in teams], [3, 3])

    def test_fields_are_parsed_as_numbers(self):
        self.write(stats_file())
        top = self.manager.read_player_stats(self.entry)[0][0]
        self.assertEqual(top["name"], "nitro")
        self.assertEqual(top["steam_id"], str(BASE + 1))
        self.assertEqual(top["kills"], 28)
        self.assertEqual(top["deaths"], 17)
        self.assertEqual(top["assists"], 4)
        self.assertEqual(top["damage"], 2611)
        self.assertEqual(top["headshots"], 12)
        self.assertEqual(top["rounds"], 27)

    def test_rows_are_sorted_by_kills(self):
        shuffled = [TEAM1[2], TEAM1[0], TEAM1[1]]
        self.write(stats_file(team1=shuffled))
        kills = [p["kills"] for p in self.manager.read_player_stats(self.entry)[0]]
        self.assertEqual(kills, sorted(kills, reverse=True))

    def test_the_score_key_is_not_read_as_a_player(self):
        self.write(stats_file())
        names = [p["name"] for team in self.manager.read_player_stats(self.entry) for p in team]
        self.assertNotIn("score", names)
        self.assertNotIn("16", names)

    def test_a_missing_file_returns_none(self):
        self.assertIsNone(self.manager.read_player_stats(self.entry))

    def test_a_file_without_players_yields_empty_teams(self):
        self.write(stats_file(team1=[], team2=[]))
        self.assertEqual(self.manager.read_player_stats(self.entry), [[], []])

    def test_missing_fields_default_to_zero(self):
        body = stats_file()
        body = "\n".join(
            line for line in body.splitlines() if '"headshot_kills"' not in line
        )
        self.write(body)
        top = self.manager.read_player_stats(self.entry)[0][0]
        self.assertEqual(top["headshots"], 0)

    def test_a_player_with_no_name_falls_back_to_the_steam_id(self):
        body = stats_file()
        body = "\n".join(
            line for line in body.splitlines() if '"name"\t\t"nitro"' not in line
        )
        self.write(body)
        top = self.manager.read_player_stats(self.entry)[0][0]
        self.assertEqual(top["name"], str(BASE + 1))


def stub_map(index, team1=TEAM1, team2=TEAM2):
    """The block get5 opens on the next level change: names and roundsplayed,
    no score and no winner."""

    def team(rows):
        out = []
        for row in rows:
            out += [
                f'{T3}"{row[0]}"',
                T3 + "{",
                f'{T4}"roundsplayed"{T2}"1"',
                f'{T4}"name"{T2}"{row[1]}"',
                T3 + "}",
            ]
        return "\n".join(out)

    return "\n".join([
        f'{T1}"map{index}"',
        T1 + "{",
        f'{T2}"team1"', T2 + "{", team(team1), T2 + "}",
        f'{T2}"team2"', T2 + "{", team(team2), T2 + "}",
        T1 + "}",
    ])


class TrailingStubMapTests(ReadPlayerStatsTests):
    """A real 2026-08-30 stats file had a finished map0 and an empty map1.

    Reading the last block instead of the last *finished* one produced a
    scoreboard of zeroes and a 0:0 score under a correct winner.
    """

    def write_with_stub(self):
        body = stats_file()
        closing = body.rindex("}")
        self.write(body[:closing] + stub_map(1) + "\n" + body[closing:])

    def test_the_result_comes_from_the_finished_map(self):
        self.write_with_stub()
        self.assertEqual(self.manager.read_stats_result(self.entry), (0, (16, 11)))

    def test_the_scoreboard_comes_from_the_finished_map(self):
        self.write_with_stub()
        top = self.manager.read_player_stats(self.entry)[0][0]
        self.assertEqual(top["name"], "nitro")
        self.assertEqual(top["kills"], 28)

    def test_map10_sorts_after_map9(self):
        body = stats_file()
        closing = body.rindex("}")
        extra = "".join(stub_map(i) + "\n" for i in range(1, 11))
        self.write(body[:closing] + extra + body[closing:])
        self.assertEqual(self.manager.read_stats_result(self.entry), (0, (16, 11)))

    def test_a_stub_only_file_is_not_a_draw(self):
        body = "\n".join(['"Stats"', "{", f'{T1}"series_type"{T2}"bo1"',
                          stub_map(0), "}"])
        self.write(body)
        self.assertIsNone(self.manager.read_stats_result(self.entry))
        self.assertIsNone(self.manager.read_player_stats(self.entry))

    def test_an_empty_stats_file_is_not_a_draw(self):
        self.write("\n".join(['"Stats"', "{", f'{T1}"series_type"{T2}"bo1"',
                              f'{T1}"team1_name"{T2}"Team A"', "}"]))
        self.assertIsNone(self.manager.read_stats_result(self.entry))

    def test_a_map_with_a_score_but_no_winner_still_counts(self):
        body = stats_file(winner="")
        body = "\n".join(l for l in body.splitlines() if '"winner"' not in l)
        self.write(body)
        self.assertEqual(self.manager.read_stats_result(self.entry), (0, (16, 11)))


class ZeroedScoreboardTests(unittest.TestCase):
    def row(self, **over):
        base = {"steam_id": "1", "name": "x", "kills": 0, "deaths": 0,
                "assists": 0, "damage": 0, "headshots": 0, "rounds": 1}
        base.update(over)
        return base

    def test_an_all_zero_team_renders_nothing(self):
        self.assertEqual(embeds.scoreboard([self.row() for _ in range(5)]), "")

    def test_one_real_stat_is_enough_to_render(self):
        roster = [self.row() for _ in range(4)] + [self.row(kills=1)]
        self.assertIn("PLAYER", embeds.scoreboard(roster))

    def test_a_player_who_only_died_still_renders(self):
        self.assertIn("PLAYER", embeds.scoreboard([self.row(deaths=3)]))


class ScoreboardTests(unittest.TestCase):
    def rows(self, roster):
        block = embeds.scoreboard(roster)
        self.assertTrue(block.startswith("```"))
        return block.strip("`").strip("\n").splitlines()

    def roster(self, **over):
        row = {
            "steam_id": str(BASE + 1),
            "name": "nitro",
            "kills": 20,
            "deaths": 10,
            "assists": 5,
            "damage": 2160,
            "headshots": 10,
            "rounds": 27,
        }
        row.update(over)
        return [row]

    def test_empty_roster_renders_nothing(self):
        self.assertEqual(embeds.scoreboard([]), "")

    def test_columns_line_up_with_the_header(self):
        rows = self.rows(
            [
                dict(self.roster()[0], name="kp"),
                dict(self.roster()[0], name="a-name-that-is-really-quite-long"),
            ]
        )
        self.assertEqual(len({len(line) for line in rows}), 1, rows)

    def test_short_names_do_not_shift_the_columns(self):
        rows = self.rows(self.roster(name="kp"))
        self.assertEqual(len(rows[0]), len(rows[1]))

    def test_long_names_are_truncated(self):
        rows = self.rows(self.roster(name="x" * 40))
        self.assertLessEqual(len(rows[1]), len(rows[0]))
        self.assertIn("x" * 16, rows[1])

    def test_adr_is_damage_over_rounds(self):
        rows = self.rows(self.roster(damage=2160, rounds=27))
        self.assertIn("80.0", rows[1])

    def test_headshot_percentage(self):
        rows = self.rows(self.roster(kills=20, headshots=10))
        self.assertIn("50%", rows[1])

    def test_zero_kills_does_not_divide_by_zero(self):
        rows = self.rows(self.roster(kills=0, headshots=0))
        self.assertIn("0%", rows[1])

    def test_zero_rounds_does_not_divide_by_zero(self):
        rows = self.rows(self.roster(rounds=0, damage=0))
        self.assertIn("0.0", rows[1])


if __name__ == "__main__":
    unittest.main()
