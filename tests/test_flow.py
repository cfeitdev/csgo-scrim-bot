"""Flow tests for the scrim state machine. No Discord connection required.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrimbot.elo import balance_teams, team_deltas  # noqa: E402
from scrimbot.maps import MAP_POOL, resolve_pool  # noqa: E402
from scrimbot.match import (  # noqa: E402
    Match,
    Phase,
    Player,
    ScrimError,
    build_ban_order,
    build_draft_order,
)


def make_match(size: int = 10, **kwargs) -> Match:
    match = Match(host_id=1, size=size, **kwargs)
    for i in range(1, size + 1):
        match.add_player(Player(id=i, name=f"player{i}", elo=1000 + i * 10))
    return match


def run_draft(match: Match) -> None:
    while match.phase is Phase.DRAFT:
        match.pick(match.current_captain.id, match.available_players[0].id)


def run_veto(match: Match) -> None:
    while match.phase is Phase.MAP_BAN:
        match.ban(match.current_banner.id, match.remaining_maps[0].name)


class DraftOrderTests(unittest.TestCase):
    def test_ten_player_order(self):
        # Snake: a single opening pick, then pairs. A B B A A B B A
        self.assertEqual(build_draft_order(8), [0, 1, 1, 0, 0, 1, 1, 0])

    def test_teams_end_up_even(self):
        order = build_draft_order(8)
        self.assertEqual(order.count(0), order.count(1))

    def test_every_lobby_size_splits_evenly(self):
        # The opening single is what keeps a snake balanced; check it holds
        # rather than trusting it only at ten players.
        for size in range(4, 21, 2):
            with self.subTest(size=size):
                order = build_draft_order(size - 2)
                self.assertEqual(len(order), size - 2)
                self.assertEqual(order.count(0), order.count(1))

    def test_blocks_are_one_then_pairs(self):
        order = build_draft_order(8)
        blocks = []
        for team in order:
            if blocks and blocks[-1][0] == team:
                blocks[-1][1] += 1
            else:
                blocks.append([team, 1])
        self.assertEqual([count for _team, count in blocks], [1, 2, 2, 2, 1])
        self.assertEqual(blocks[0][0], 0, "team A opens the draft")

    def test_ban_order_starts_with_team_b(self):
        # Derived from the pool size so adding maps does not break the test.
        order = build_ban_order(len(MAP_POOL) - 1)
        self.assertEqual(len(order), len(MAP_POOL) - 1)
        self.assertEqual(order[0], 1)
        # B bans first, so it takes the extra ban when the count is odd.
        self.assertEqual(order.count(1) - order.count(0), len(order) % 2)

    def test_ban_order_for_the_original_fourteen_map_pool(self):
        order = build_ban_order(13)
        self.assertEqual((order.count(1), order.count(0)), (7, 6))


class LobbyTests(unittest.TestCase):
    def test_duplicate_join_rejected(self):
        match = Match(host_id=1)
        match.add_player(Player(1, "a", 1000))
        with self.assertRaises(ScrimError):
            match.add_player(Player(1, "a", 1000))

    def test_eleventh_player_rejected(self):
        match = make_match()
        with self.assertRaises(ScrimError):
            match.add_player(Player(99, "late", 1000))

    def test_leave_after_start_rejected(self):
        match = make_match()
        match.start_teams(random.Random(7))
        with self.assertRaises(ScrimError):
            match.remove_player(match.players[-1].id)

    def test_staff_can_force_drop_after_start(self):
        match = make_match()
        match.start_teams(random.Random(7))
        victim = match.players[-1]
        match.remove_player(victim.id, force=True)
        self.assertFalse(match.has_player(victim.id))
        self.assertNotIn(victim, [p for team in match.teams for p in team])


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.match = make_match()
        self.match.start_teams(random.Random(1))

    def test_captains_are_seeded_onto_teams(self):
        self.assertEqual(self.match.phase, Phase.DRAFT)
        self.assertEqual(len(self.match.captains), 2)
        self.assertEqual(self.match.teams[0][0], self.match.captains[0])
        self.assertEqual(self.match.teams[1][0], self.match.captains[1])
        self.assertEqual(len(self.match.available_players), 8)

    def test_wrong_captain_cannot_pick(self):
        wrong = self.match.captains[1].id
        target = self.match.available_players[0].id
        with self.assertRaises(ScrimError):
            self.match.pick(wrong, target)

    def test_non_captain_cannot_pick(self):
        outsider = self.match.available_players[0]
        with self.assertRaises(ScrimError):
            self.match.pick(outsider.id, self.match.available_players[1].id)

    def test_pick_counts_follow_the_snake(self):
        # A:1  B:2  A:2  B:2  A:1
        self.assertEqual(self.match.picks_left_for_current_captain(), 1)
        self.drive_picks(1)
        self.assertEqual(self.match.current_draft_team, 1)
        self.assertEqual(self.match.picks_left_for_current_captain(), 2)
        self.drive_picks(2)
        self.assertEqual(self.match.current_draft_team, 0)
        self.assertEqual(self.match.picks_left_for_current_captain(), 2)
        self.drive_picks(2)
        self.assertEqual(self.match.current_draft_team, 1)
        self.assertEqual(self.match.picks_left_for_current_captain(), 2)
        self.drive_picks(2)
        # The eighth slot belongs to A, but only one player is left by then, so
        # _autofill_forced_picks assigns them and the draft closes itself.
        self.assertIsNone(self.match.current_draft_team)
        self.assertEqual(len(self.match.teams[0]), 5)
        self.assertEqual(len(self.match.teams[1]), 5)

    def test_the_autofilled_last_player_goes_to_the_team_whose_slot_it_is(self):
        # Snake ends A, so A must receive the auto-assigned final player.
        last = None
        while self.match.current_draft_team is not None:
            last = self.match.available_players[-1]
            captain = self.match.current_captain
            self.match.pick(captain.id, self.match.available_players[0].id)
        self.assertIn(last, self.match.teams[0])

    def test_full_draft_lands_in_map_ban_with_five_a_side(self):
        # Seven picks, not eight -- the last player is assigned automatically.
        self.drive_picks(7)
        self.assertEqual(self.match.phase, Phase.MAP_BAN)
        self.assertEqual(len(self.match.teams[0]), 5)
        self.assertEqual(len(self.match.teams[1]), 5)
        self.assertEqual(self.match.available_players, [])
        drafted = [p.id for team in self.match.teams for p in team]
        self.assertEqual(len(set(drafted)), 10)

    def drive_picks(self, count: int) -> None:
        for _ in range(count):
            captain = self.match.current_captain
            self.match.pick(captain.id, self.match.available_players[0].id)


class CaptainSelectionTests(unittest.TestCase):
    def test_highest_elo_captains_when_random_is_off(self):
        match = make_match(random_captains=False)
        match.start_teams(random.Random(2))
        best = sorted(match.players, key=lambda p: p.elo, reverse=True)[:2]
        self.assertEqual({c.id for c in match.captains}, {p.id for p in best})

    def test_forced_captain_is_honoured(self):
        match = make_match()
        match.set_forced_captain(3, 1)
        match.start_teams(random.Random(5))
        self.assertEqual(match.captains[1].id, 3)
        self.assertEqual(match.teams[1][0].id, 3)

    def test_forcing_both_captains(self):
        match = make_match()
        match.set_forced_captain(4, 0)
        match.set_forced_captain(9, 1)
        match.start_teams(random.Random(5))
        self.assertEqual([c.id for c in match.captains], [4, 9])

    def test_forcing_the_same_player_moves_them(self):
        match = make_match()
        match.set_forced_captain(4, 0)
        match.set_forced_captain(4, 1)
        self.assertEqual(match.forced_captains, [None, 4])


class PickModeTests(unittest.TestCase):
    def test_random_mode_skips_the_draft(self):
        match = make_match(pick_mode="random")
        match.start_teams(random.Random(4))
        self.assertEqual(match.phase, Phase.MAP_BAN)
        self.assertEqual(len(match.teams[0]), 5)
        self.assertEqual(len(match.teams[1]), 5)

    def test_balanced_mode_splits_by_elo(self):
        match = make_match(pick_mode="balanced")
        match.start_teams(random.Random(4))
        self.assertEqual(match.phase, Phase.MAP_BAN)
        self.assertLessEqual(abs(match.team_elo(0) - match.team_elo(1)), 15)

    def test_balance_teams_is_even(self):
        players = [Player(i, f"p{i}", elo) for i, elo in enumerate([1500, 1400, 1000, 900])]
        first, second = balance_teams(players)
        self.assertEqual(len(first), len(second))
        self.assertEqual(
            sum(p.elo for p in first), sum(p.elo for p in second)
        )

    def test_unknown_pick_mode_is_rejected(self):
        match = make_match(pick_mode="nonsense")
        with self.assertRaises(ScrimError):
            match.start_teams(random.Random(1))


class MapBanTests(unittest.TestCase):
    def setUp(self):
        self.match = make_match()
        self.match.start_teams(random.Random(3))
        run_draft(self.match)

    def test_veto_starts_with_full_pool(self):
        self.assertEqual(self.match.phase, Phase.MAP_BAN)
        self.assertEqual(len(self.match.remaining_maps), len(MAP_POOL))
        self.assertEqual(self.match.current_ban_team, 1)

    def test_wrong_captain_cannot_ban(self):
        wrong = self.match.captains[0].id
        with self.assertRaises(ScrimError):
            self.match.ban(wrong, self.match.remaining_maps[0].name)

    def test_veto_resolves_to_one_map_and_goes_live(self):
        run_veto(self.match)
        self.assertEqual(self.match.phase, Phase.LIVE)
        self.assertIsNotNone(self.match.selected_map)
        self.assertEqual(len(self.match.banned), len(MAP_POOL) - 1)

    def test_cannot_ban_twice(self):
        banner = self.match.current_banner
        game_map = self.match.remaining_maps[0]
        self.match.ban(banner.id, game_map.name)
        with self.assertRaises(ScrimError):
            self.match.ban(self.match.current_banner.id, game_map.name)

    def test_vetoing_off_rolls_a_map_immediately(self):
        match = make_match(vetoing=False)
        match.start_teams(random.Random(3))
        run_draft(match)
        self.assertEqual(match.phase, Phase.LIVE)
        self.assertIn(match.selected_map, MAP_POOL)
        self.assertEqual(match.banned, [])


class SubAndRedraftTests(unittest.TestCase):
    def setUp(self):
        self.match = make_match()
        self.match.start_teams(random.Random(8))
        run_draft(self.match)

    def test_sub_keeps_the_team_slot(self):
        target = self.match.teams[1][3]
        replacement = Player(99, "sub", 1234)
        outgoing, team = self.match.substitute(target.id, replacement)
        self.assertEqual(outgoing, target)
        self.assertEqual(team, 1)
        self.assertIn(replacement, self.match.teams[1])
        self.assertNotIn(target, self.match.players)

    def test_sub_inherits_captaincy(self):
        captain = self.match.captains[0]
        replacement = Player(99, "sub", 1234)
        self.match.substitute(captain.id, replacement)
        self.assertEqual(self.match.captains[0], replacement)
        self.assertEqual(self.match.teams[0][0], replacement)

    def test_cannot_sub_in_someone_already_playing(self):
        existing = self.match.teams[0][1]
        with self.assertRaises(ScrimError):
            self.match.substitute(self.match.teams[1][1].id, existing)

    def test_redraft_rebuilds_teams(self):
        self.match.redraft(random.Random(99))
        self.assertEqual(self.match.phase, Phase.DRAFT)
        self.assertEqual(len(self.match.teams[0]), 1)
        self.assertEqual(len(self.match.available_players), 8)
        self.assertIsNone(self.match.selected_map)

    def test_redraft_rerolls_captains(self):
        # A reroll should hand the armband to different people.
        before = {c.id for c in self.match.captains}
        changed = 0
        for seed in range(12):
            match = make_match()
            match.start_teams(random.Random(seed))
            first = {c.id for c in match.captains}
            match.redraft(random.Random(seed))
            if {c.id for c in match.captains} != first:
                changed += 1
        self.assertEqual(changed, 12, "every redraft should produce new captains")
        self.assertTrue(before)

    def test_reroll_avoids_the_previous_pair_specifically(self):
        match = make_match()
        match.start_teams(random.Random(3))
        previous = {c.id for c in match.captains}
        for _ in range(5):
            match.redraft(random.Random(0))
            current = {c.id for c in match.captains}
            self.assertEqual(current & previous, set())
            previous = current

    def test_reroll_still_respects_highest_elo_mode(self):
        match = make_match(random_captains=False)
        match.start_teams(random.Random(3))
        best = {p.id for p in sorted(match.players, key=lambda p: p.elo, reverse=True)[:2]}
        match.redraft(random.Random(9))
        self.assertEqual({c.id for c in match.captains}, best)

    def test_redraft_clears_forced_captains(self):
        """A re-draft is a fresh roll -- forced picks do not carry over."""
        self.match.set_forced_captain(7, 0)
        self.match.redraft(random.Random(99))
        self.assertEqual(self.match.forced_captains, [None, None])


class FinishTests(unittest.TestCase):
    def setUp(self):
        self.match = make_match()
        self.match.start_teams(random.Random(6))
        run_draft(self.match)
        run_veto(self.match)

    def test_finish_records_the_winner(self):
        self.match.finish(0)
        self.assertEqual(self.match.phase, Phase.COMPLETE)
        self.assertEqual(self.match.winner, 0)
        self.assertTrue(self.match.finished)

    def test_cannot_finish_twice(self):
        self.match.finish(1)
        with self.assertRaises(ScrimError):
            self.match.finish(0)

    def test_cannot_finish_before_teams_exist(self):
        match = make_match()
        with self.assertRaises(ScrimError):
            match.finish(0)


class EloMathTests(unittest.TestCase):
    def test_even_teams_trade_half_of_k(self):
        win, lose = team_deltas([1000] * 5, [1000] * 5, k=32)
        self.assertEqual(win, 16)
        self.assertEqual(lose, -16)

    def test_beating_a_stronger_team_pays_more(self):
        underdog, _ = team_deltas([900] * 5, [1300] * 5)
        favourite, _ = team_deltas([1300] * 5, [900] * 5)
        self.assertGreater(underdog, favourite)

    def test_a_draw_moves_ratings_toward_each_other(self):
        win, lose = team_deltas([1300] * 5, [900] * 5, tie=True)
        self.assertLess(win, 0)
        self.assertGreater(lose, 0)

    def test_zero_sum(self):
        win, lose = team_deltas([1120] * 5, [980] * 5)
        self.assertEqual(win, -lose)


class AutoPickTests(unittest.TestCase):
    def test_timeouts_can_drive_the_whole_flow(self):
        match = make_match()
        rng = random.Random(11)
        match.start_teams(rng)
        while match.phase is Phase.DRAFT:
            match.auto_pick(rng)
        while match.phase is Phase.MAP_BAN:
            match.auto_ban(rng)
        self.assertEqual(match.phase, Phase.LIVE)
        self.assertEqual(len(match.teams[0]), 5)
        self.assertEqual(len(match.teams[1]), 5)


class MapPoolTests(unittest.TestCase):
    def test_pool_matches_spec(self):
        expected = [
            "de_dust2_old",
            "de_cache",
            "de_mirage",
            "de_train",
            "cs_italy",
            "cs_assault",
            "de_overpass",
            "cs_agency",
            "de_vertigo",
            "cs_militia",
            "de_aztec",
            "de_dust",
            "cs_office",
            "cs_workout",
            "de_tulip",
            "de_season",
        ]
        self.assertEqual([m.name for m in MAP_POOL], expected)

    def test_changelevel_command(self):
        self.assertEqual(MAP_POOL[2].changelevel_command(), "changelevel de_mirage")

    def test_custom_pool_with_workshop_id(self):
        pool = resolve_pool(["de_nuke", "de_cache"], {"de_nuke": "123456"})
        self.assertEqual(pool[0].changelevel_command(), "host_workshop_map 123456")
        self.assertEqual(pool[1].changelevel_command(), "changelevel de_cache")
        self.assertEqual(pool[1].display, "Cache")

    def test_empty_pool_falls_back_to_default(self):
        self.assertEqual(len(resolve_pool(None)), len(MAP_POOL))


class RconEncodingTests(unittest.TestCase):
    def test_packet_layout(self):
        import struct

        from scrimbot.rcon import SERVERDATA_EXECCOMMAND, _encode

        packet = _encode(7, SERVERDATA_EXECCOMMAND, "changelevel de_dust2")
        (length,) = struct.unpack("<i", packet[:4])
        self.assertEqual(length, len(packet) - 4)
        packet_id, packet_type = struct.unpack("<ii", packet[4:12])
        self.assertEqual(packet_id, 7)
        self.assertEqual(packet_type, SERVERDATA_EXECCOMMAND)
        self.assertTrue(packet.endswith(b"\x00\x00"))

    def test_status_parsing(self):
        from scrimbot.server import parse_status

        output = (
            "hostname: scrim server\n"
            '# 2 1 "nitro" STEAM_1:0:12345678 12:34 30 0 active 786432\n'
            '# 5 2 "hazed" [U:1:87654321] 03:11 44 0 active 196608\n'
        )
        players = parse_status(output)
        self.assertEqual([p.userid for p in players], [2, 5])
        self.assertEqual(players[0].name, "nitro")
        self.assertEqual(players[1].steam_id, "[U:1:87654321]")


if __name__ == "__main__":
    unittest.main()
