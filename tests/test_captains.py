"""Forced captains land in place, and the last pick makes itself."""

from __future__ import annotations

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrimbot.match import Match, Phase, Player  # noqa: E402


def lobby(size=10, seed=4):
    match = Match(host_id=1, size=size)
    for i in range(1, size + 1):
        match.add_player(Player(i, f"p{i}", 1000 + i))
    match.start_teams(random.Random(seed))
    return match


def drafting(picks=3, **over):
    match = lobby(**over)
    for _ in range(picks):
        match.pick(match.current_captain.id, match.available_players[0].id)
    return match


def names(match):
    return [[p.name for p in team] for team in match.teams]


class ForcedCaptainTests(unittest.TestCase):
    def test_a_forced_captain_does_not_reroll_the_draft(self):
        match = drafting(5)
        before, index = names(match), match.draft_index
        match.set_forced_captain(match.available_players[0].id, 1)
        self.assertEqual(match.draft_index, index)
        self.assertEqual([len(t) for t in match.teams], [len(t) for t in before])

    def test_pulling_a_captain_from_the_pool_swaps_the_old_one_out(self):
        match = drafting(3)
        old = match.teams[0][0]
        target = match.available_players[1]
        match.set_forced_captain(target.id, 0)
        self.assertIs(match.teams[0][0], target)
        self.assertIn(old, match.available_players)
        self.assertNotIn(target, match.available_players)

    def test_promoting_someone_already_on_the_team_is_a_swap(self):
        match = drafting(4)
        old, target = match.teams[0][0], match.teams[0][2]
        match.set_forced_captain(target.id, 0)
        self.assertIs(match.teams[0][0], target)
        self.assertIs(match.teams[0][2], old)

    def test_taking_a_captain_from_the_other_team_keeps_sizes(self):
        match = drafting(4)
        sizes = [len(t) for t in match.teams]
        old, target = match.teams[0][0], match.teams[1][1]
        match.set_forced_captain(target.id, 0)
        self.assertIs(match.teams[0][0], target)
        self.assertIs(match.teams[1][1], old)
        self.assertEqual([len(t) for t in match.teams], sizes)

    def test_forcing_the_current_captain_changes_nothing(self):
        match = drafting(4)
        before = names(match)
        match.set_forced_captain(match.teams[0][0].id, 0)
        self.assertEqual(names(match), before)

    def test_nobody_is_lost_or_duplicated(self):
        for target_of in ("pool", "same", "other"):
            with self.subTest(target=target_of):
                match = drafting(4)
                target = {
                    "pool": match.available_players[0],
                    "same": match.teams[0][1],
                    "other": match.teams[1][2],
                }[target_of]
                match.set_forced_captain(target.id, 0)
                placed = [p.id for team in match.teams for p in team]
                placed += [p.id for p in match.available_players]
                self.assertEqual(sorted(placed), sorted(p.id for p in match.players))

    def test_captains_list_tracks_the_change(self):
        match = drafting(3)
        target = match.available_players[0]
        match.set_forced_captain(target.id, 1)
        self.assertIs(match.captains[1], target)
        self.assertIs(match.captains[1], match.teams[1][0])

    def test_forcing_the_same_person_onto_both_sides_clears_the_first(self):
        match = lobby()
        match.set_forced_captain(5, 0)
        match.set_forced_captain(5, 1)
        self.assertEqual(match.forced_captains, [None, 5])

    def test_someone_not_in_the_lobby_is_refused(self):
        match = lobby()
        with self.assertRaises(Exception):
            match.set_forced_captain(9999, 0)


class RedraftTests(unittest.TestCase):
    def test_redraft_clears_forced_captains(self):
        match = lobby()
        match.set_forced_captain(7, 0)
        match.set_forced_captain(9, 1)
        match.redraft(random.Random(11))
        self.assertEqual(match.forced_captains, [None, None])

    def test_redraft_after_forcing_does_not_reuse_them(self):
        match = lobby()
        match.set_forced_captain(7, 0)
        match.redraft(random.Random(11))
        match.redraft(random.Random(12))
        self.assertEqual(match.forced_captains, [None, None])

    def test_redraft_still_produces_a_full_draft(self):
        match = drafting(4)
        match.redraft(random.Random(5))
        self.assertIs(match.phase, Phase.DRAFT)
        self.assertEqual([len(t) for t in match.teams], [1, 1])


class LastPickTests(unittest.TestCase):
    def play_out(self, size=10):
        match = lobby(size=size)
        picks = 0
        while match.phase is Phase.DRAFT:
            match.pick(match.current_captain.id, match.available_players[0].id)
            picks += 1
        return match, picks

    def test_the_final_player_is_assigned_automatically(self):
        match, picks = self.play_out()
        self.assertEqual(picks, 7)  # eight slots, the last fills itself

    def test_everyone_ends_up_on_a_team(self):
        match, _ = self.play_out()
        self.assertEqual([len(t) for t in match.teams], [5, 5])
        self.assertEqual(match.available_players, [])

    def test_the_draft_still_advances_to_the_map_ban(self):
        match, _ = self.play_out()
        self.assertIs(match.phase, Phase.MAP_BAN)

    def test_a_captain_is_never_left_on_the_clock_with_one_option(self):
        match = lobby()
        while match.phase is Phase.DRAFT:
            self.assertGreater(len(match.available_players), 1)
            match.pick(match.current_captain.id, match.available_players[0].id)

    def test_it_works_for_a_smaller_lobby(self):
        match, picks = self.play_out(size=4)
        self.assertEqual([len(t) for t in match.teams], [2, 2])
        self.assertEqual(picks, 1)

    def test_the_autofilled_player_goes_to_the_side_whose_turn_it_was(self):
        match = lobby()
        while len(match.available_players) > 1:
            expected = match.draft_order[match.draft_index]
            last = None
            if len(match.available_players) == 2:
                # after this pick one remains, and it is the next side's turn
                expected_next = match.draft_order[match.draft_index + 1]
                last = [p for p in match.available_players
                        if p.id != match.available_players[0].id][0]
            match.pick(match.current_captain.id, match.available_players[0].id)
            del expected
        self.assertIn(last, match.teams[expected_next])


if __name__ == "__main__":
    unittest.main()


def full_lobby(size=10, blacklist=(), **over):
    match = Match(host_id=1, size=size, **over)
    for i in range(1, size + 1):
        match.add_player(Player(i, f"p{i}", 1000 + i))
    match.captain_blacklist = set(blacklist)
    return match


class CaptainBlacklistTests(unittest.TestCase):
    def test_blacklisted_players_are_never_random_captains(self):
        for seed in range(50):
            match = full_lobby(blacklist={1, 2, 3, 4, 5, 6, 7, 8})
            match.start_teams(random.Random(seed))
            self.assertEqual({c.id for c in match.captains}, {9, 10})

    def test_highest_elo_mode_skips_blacklisted(self):
        # p10 and p9 have the highest Elo but are barred.
        match = full_lobby(blacklist={10, 9}, random_captains=False)
        match.start_teams(random.Random(1))
        self.assertEqual([c.id for c in match.captains], [8, 7])

    def test_falls_back_when_everyone_is_blacklisted(self):
        match = full_lobby(size=2, blacklist={1, 2})
        match.start_teams(random.Random(1))
        self.assertEqual({c.id for c in match.captains}, {1, 2})

    def test_balanced_mode_moves_an_eligible_player_to_captain(self):
        match = full_lobby(blacklist={10, 9, 1, 2}, pick_mode="balanced")
        match.start_teams(random.Random(1))
        for team, captain in zip(match.teams, match.captains):
            self.assertIs(team[0], captain)
            self.assertNotIn(captain.id, match.captain_blacklist)
        self.assertEqual(sum(len(t) for t in match.teams), 10)

    def test_random_mode_skips_blacklisted(self):
        match = full_lobby(blacklist=set(range(1, 9)), pick_mode="random")
        match.start_teams(random.Random(3))
        self.assertEqual({c.id for c in match.captains}, {9, 10})

    def test_redraft_respects_the_blacklist(self):
        match = full_lobby(blacklist=set(range(1, 9)))
        match.start_teams(random.Random(2))
        match.redraft(random.Random(5))
        self.assertEqual({c.id for c in match.captains}, {9, 10})

    def test_forcing_a_blacklisted_captain_is_refused(self):
        from scrimbot.match import ScrimError

        match = full_lobby(blacklist={4})
        with self.assertRaises(ScrimError):
            match.set_forced_captain(4, 0)
        self.assertEqual(match.forced_captains, [None, None])
