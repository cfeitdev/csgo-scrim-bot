"""The EU/NA server vote, in the pure match model. No Discord required."""

from __future__ import annotations

import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrimbot.match import Match, Phase, Player, ScrimError  # noqa: E402

REGIONS = ("eu", "na")


def make_match(size: int = 10, **kwargs) -> Match:
    kwargs.setdefault("regions", REGIONS)
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


def vote(match: Match, eu: int, na: int) -> None:
    """Players 1..eu vote EU, the next ``na`` vote NA."""
    for uid in range(1, eu + 1):
        match.vote_region(uid, "eu")
    for uid in range(eu + 1, eu + na + 1):
        match.vote_region(uid, "na")


class NoVoteTests(unittest.TestCase):
    def test_a_single_region_never_votes(self):
        match = make_match(regions=("eu",))
        match.start_teams(random.Random(1))
        self.assertFalse(match.has_region_vote)
        self.assertFalse(match.vote_open)
        run_draft(match)
        run_veto(match)
        self.assertEqual(match.phase, Phase.LIVE)
        self.assertIsNone(match.region)

    def test_no_regions_at_all_is_todays_behaviour(self):
        match = make_match(regions=())
        match.start_teams(random.Random(1))
        run_draft(match)
        run_veto(match)
        self.assertIsNone(match.region)
        with self.assertRaises(ScrimError):
            match.vote_region(1, "eu")

    def test_no_draft_and_no_veto_defaults_to_eu_without_a_vote(self):
        for mode in ("random", "balanced"):
            with self.subTest(mode=mode):
                match = make_match(pick_mode=mode, vetoing=False)
                match.start_teams(random.Random(2))
                self.assertEqual(match.phase, Phase.LIVE)
                self.assertFalse(match.vote_open)
                self.assertEqual(match.region, "eu")
                self.assertEqual(match.region_decided_by, "default")

    def test_the_default_follows_the_configured_home_region(self):
        match = make_match(pick_mode="random", vetoing=False, default_region="na")
        match.start_teams(random.Random(2))
        self.assertEqual(match.region, "na")


class OpeningTests(unittest.TestCase):
    def test_the_lobby_does_not_vote(self):
        match = Match(host_id=1, size=10, regions=REGIONS)
        match.add_player(Player(1, "a", 1000))
        self.assertFalse(match.vote_open)
        with self.assertRaises(ScrimError):
            match.vote_region(1, "na")

    def test_it_opens_with_the_captain_draft(self):
        match = make_match()
        match.start_teams(random.Random(1))
        self.assertEqual(match.phase, Phase.DRAFT)
        self.assertTrue(match.vote_open)
        self.assertIsNone(match.region)

    def test_it_opens_with_the_veto_when_there_is_no_draft(self):
        match = make_match(pick_mode="random")
        match.start_teams(random.Random(1))
        self.assertEqual(match.phase, Phase.MAP_BAN)
        self.assertTrue(match.vote_open)

    def test_it_stays_open_from_the_draft_into_the_veto(self):
        match = make_match()
        match.start_teams(random.Random(1))
        run_draft(match)
        self.assertEqual(match.phase, Phase.MAP_BAN)
        self.assertTrue(match.vote_open)


class VotingTests(unittest.TestCase):
    def setUp(self):
        self.match = make_match()
        self.match.start_teams(random.Random(1))

    def test_votes_are_counted(self):
        vote(self.match, 3, 4)
        self.assertEqual(self.match.region_tally(), {"eu": 3, "na": 4})
        self.assertTrue(self.match.vote_open)

    def test_a_player_can_change_their_vote(self):
        self.match.vote_region(1, "eu")
        self.match.vote_region(1, "na")
        self.assertEqual(self.match.region_tally(), {"eu": 0, "na": 1})

    def test_changing_a_vote_does_not_count_twice_toward_closing(self):
        for _ in range(12):
            self.match.vote_region(1, "na")
        self.assertTrue(self.match.vote_open)

    def test_only_players_in_the_game_can_vote(self):
        with self.assertRaises(ScrimError):
            self.match.vote_region(999, "na")

    def test_an_unknown_region_is_refused(self):
        with self.assertRaises(ScrimError):
            self.match.vote_region(1, "asia")

    def test_the_tenth_vote_closes_it_early(self):
        vote(self.match, 4, 5)
        self.assertTrue(self.match.vote_open)
        closed = self.match.vote_region(10, "na")
        self.assertTrue(closed)
        self.assertFalse(self.match.vote_open)
        self.assertEqual(self.match.region, "na")
        self.assertEqual(self.match.region_decided_by, "vote")
        # ...while the draft carries on underneath it
        self.assertEqual(self.match.phase, Phase.DRAFT)

    def test_no_voting_once_it_has_closed(self):
        vote(self.match, 10, 0)
        with self.assertRaises(ScrimError):
            self.match.vote_region(1, "na")

    def test_a_lobby_of_two_closes_at_two_votes(self):
        match = make_match(size=2)
        match.start_teams(random.Random(1))
        self.assertFalse(match.vote_region(1, "eu"))
        self.assertTrue(match.vote_region(2, "eu"))
        self.assertEqual(match.region, "eu")


class ClosingAtVetoEndTests(unittest.TestCase):
    def finish(self, eu: int, na: int, **kwargs) -> Match:
        match = make_match(**kwargs)
        match.start_teams(random.Random(1))
        vote(match, eu, na)
        run_draft(match)
        run_veto(match)
        self.assertEqual(match.phase, Phase.LIVE)
        self.assertFalse(match.vote_open)
        return match

    def test_the_majority_wins_when_the_veto_ends(self):
        match = self.finish(2, 5)
        self.assertEqual(match.region, "na")
        self.assertEqual(match.region_decided_by, "vote")

    def test_eu_can_win_too(self):
        match = self.finish(4, 3)
        self.assertEqual(match.region, "eu")

    def test_a_tie_is_a_coin_flip(self):
        seen = set()
        for seed in range(30):
            random.seed(seed)
            match = self.finish(3, 3)
            self.assertEqual(match.region_decided_by, "coin")
            seen.add(match.region)
        self.assertEqual(seen, {"eu", "na"}, "both sides of the coin come up")

    def test_the_coin_only_chooses_between_the_tied_regions(self):
        with mock.patch("scrimbot.match.random.choice", side_effect=lambda opts: opts[-1]) as flip:
            match = self.finish(3, 3)
        flip.assert_called_once()
        self.assertEqual(sorted(flip.call_args.args[0]), ["eu", "na"])
        self.assertEqual(match.region, "na")

    def test_nobody_voting_is_a_coin_flip(self):
        match = self.finish(0, 0)
        self.assertIn(match.region, REGIONS)
        self.assertEqual(match.region_decided_by, "coin")

    def test_with_no_veto_the_vote_closes_when_the_draft_ends(self):
        match = make_match(vetoing=False)
        match.start_teams(random.Random(1))
        vote(match, 1, 6)
        run_draft(match)
        self.assertEqual(match.phase, Phase.LIVE)
        self.assertEqual(match.region, "na")

    def test_with_no_draft_the_vote_closes_when_the_veto_ends(self):
        match = self.finish(1, 2, pick_mode="balanced")
        self.assertEqual(match.region, "na")

    def test_timeouts_driving_the_flow_still_settle_the_region(self):
        match = make_match()
        rng = random.Random(4)
        match.start_teams(rng)
        vote(match, 0, 3)
        while match.phase is Phase.DRAFT:
            match.auto_pick(rng)
        while match.phase is Phase.MAP_BAN:
            match.auto_ban(rng)
        self.assertEqual(match.region, "na")


class OverrideTests(unittest.TestCase):
    def setUp(self):
        self.match = make_match()
        self.match.start_teams(random.Random(1))

    def test_an_admin_can_settle_it_while_it_is_open(self):
        vote(self.match, 0, 6)
        self.match.override_region("eu")
        self.assertEqual(self.match.region, "eu")
        self.assertEqual(self.match.region_decided_by, "admin")
        self.assertFalse(self.match.vote_open)

    def test_the_override_survives_the_veto_ending(self):
        vote(self.match, 0, 6)
        self.match.override_region("eu")
        run_draft(self.match)
        run_veto(self.match)
        self.assertEqual(self.match.region, "eu")

    def test_no_override_once_the_region_is_locked(self):
        vote(self.match, 10, 0)
        with self.assertRaises(ScrimError):
            self.match.override_region("na")
        self.assertEqual(self.match.region, "eu")

    def test_no_override_when_there_was_no_vote(self):
        match = make_match(pick_mode="random", vetoing=False)
        match.start_teams(random.Random(1))
        with self.assertRaises(ScrimError):
            match.override_region("na")
        self.assertEqual(match.region, "eu")

    def test_no_override_to_an_unknown_region(self):
        with self.assertRaises(ScrimError):
            self.match.override_region("asia")


class RosterChangeTests(unittest.TestCase):
    def setUp(self):
        self.match = make_match()
        self.match.start_teams(random.Random(1))

    def test_force_drop_takes_their_vote_with_them(self):
        self.match.vote_region(5, "na")
        self.match.remove_player(5, force=True)
        self.assertEqual(self.match.region_tally(), {"eu": 0, "na": 0})

    def test_a_short_lobby_does_not_close_early(self):
        # Nine left after a drop: the tenth slot is still to be filled by /sub.
        self.match.remove_player(10, force=True)
        vote(self.match, 0, 9)
        self.assertTrue(self.match.vote_open)

    def test_a_sub_does_not_inherit_the_outgoing_vote(self):
        self.match.vote_region(5, "na")
        self.match.substitute(5, Player(99, "sub", 1000))
        self.assertNotIn(5, self.match.region_votes)
        self.assertNotIn(99, self.match.region_votes)

    def test_the_sub_can_cast_their_own(self):
        self.match.substitute(5, Player(99, "sub", 1000))
        self.match.vote_region(99, "eu")
        self.assertEqual(self.match.region_votes[99], "eu")
        with self.assertRaises(ScrimError):
            self.match.vote_region(5, "eu")  # the outgoing player is gone

    def test_players_still_cannot_leave_once_teams_are_forming(self):
        with self.assertRaises(ScrimError):
            self.match.remove_player(3)


class RedraftTests(unittest.TestCase):
    def test_a_redraft_keeps_the_votes_and_the_vote_open(self):
        match = make_match()
        match.start_teams(random.Random(1))
        vote(match, 2, 3)
        match.redraft(random.Random(5))
        self.assertTrue(match.vote_open)
        self.assertEqual(match.region_tally(), {"eu": 2, "na": 3})

    def test_a_redraft_after_the_lock_keeps_the_region(self):
        match = make_match()
        match.start_teams(random.Random(1))
        vote(match, 1, 2)
        run_draft(match)
        run_veto(match)
        self.assertEqual(match.region, "na")
        match.redraft(random.Random(5))
        self.assertEqual(match.phase, Phase.DRAFT)
        self.assertFalse(match.vote_open, "no second vote")
        run_draft(match)
        run_veto(match)
        self.assertEqual(match.region, "na")
        self.assertEqual(match.region_decided_by, "vote")


if __name__ == "__main__":
    unittest.main()
