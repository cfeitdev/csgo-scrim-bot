"""Leaderboard paging buttons and the shrinking draft order."""

from __future__ import annotations

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import discord  # noqa: F401

    from scrimbot import embeds
    from scrimbot.leaderboardview import LeaderboardView

    HAS_DISCORD = True
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    HAS_DISCORD = False

from scrimbot.match import Match, Phase, Player  # noqa: E402


def rows(count):
    return [
        (1_000_000 + i, {"elo": 1500 - i, "wins": 10, "losses": 2}) for i in range(count)
    ]


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class LeaderboardPagingTests(unittest.TestCase):
    def test_page_count(self):
        self.assertEqual(LeaderboardView("5v5", rows(0)).pages, 1)
        self.assertEqual(LeaderboardView("5v5", rows(10)).pages, 1)
        self.assertEqual(LeaderboardView("5v5", rows(11)).pages, 2)
        self.assertEqual(LeaderboardView("5v5", rows(23)).pages, 3)

    def test_first_page_cannot_go_back(self):
        view = LeaderboardView("5v5", rows(23))
        self.assertTrue(view.previous.disabled)
        self.assertFalse(view.next.disabled)

    def test_last_page_cannot_go_forward(self):
        view = LeaderboardView("5v5", rows(23), page=3)
        self.assertFalse(view.previous.disabled)
        self.assertTrue(view.next.disabled)

    def test_a_single_page_disables_both(self):
        view = LeaderboardView("5v5", rows(4))
        self.assertTrue(view.previous.disabled)
        self.assertTrue(view.next.disabled)

    def test_an_out_of_range_page_is_clamped(self):
        self.assertEqual(LeaderboardView("5v5", rows(23), page=99).page, 3)
        self.assertEqual(LeaderboardView("5v5", rows(23), page=-5).page, 1)

    def test_ranks_continue_across_pages(self):
        view = LeaderboardView("5v5", rows(23), page=2)
        self.assertIn("`11.`", view.embed.description)
        self.assertIn("`20.`", view.embed.description)
        self.assertNotIn("`21.`", view.embed.description)

    def test_the_last_page_is_not_padded(self):
        view = LeaderboardView("5v5", rows(23), page=3)
        self.assertEqual(len(view.embed.description.splitlines()), 3)

    def test_the_footer_tracks_the_page(self):
        view = LeaderboardView("5v5", rows(23), page=2)
        self.assertIn("Page 2/3", view.embed.footer.text)
        self.assertIn("23 ranked players", view.embed.footer.text)

    def test_medals_only_on_the_first_three(self):
        first = LeaderboardView("5v5", rows(23)).embed.description
        self.assertIn("🥇", first)
        self.assertIn("🥉", first)
        self.assertNotIn("🥇", LeaderboardView("5v5", rows(23), page=2).embed.description)

    def test_the_view_object_is_reused_so_custom_ids_stay_stable(self):
        view = LeaderboardView("5v5", rows(23))
        before = [item.custom_id for item in view.children]
        view.page = 2
        view._sync()
        self.assertEqual([item.custom_id for item in view.children], before)


class DraftOrderTests(unittest.TestCase):
    def match(self):
        match = Match(host_id=1, size=10)
        for i in range(1, 11):
            match.add_player(Player(i, f"p{i}", 1000))
        match.start_teams(random.Random(4))
        return match

    def order(self, match):
        field = next(f for f in embeds.draft_embed(match).fields if f.name == "Order")
        return field.value

    @unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
    def test_the_order_shrinks_by_one_per_pick(self):
        match = self.match()
        seen = []
        while match.phase is Phase.DRAFT:
            seen.append(len(self.order(match).split()))
            match.pick(match.current_captain.id, match.available_players[0].id)
        self.assertEqual(seen, sorted(seen, reverse=True))
        self.assertEqual(seen[0] - seen[-1], len(seen) - 1)

    @unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
    def test_the_leftmost_square_is_always_on_the_clock(self):
        match = self.match()
        while match.phase is Phase.DRAFT:
            first = self.order(match).split()[0]
            self.assertTrue(first.startswith("**"), first)
            match.pick(match.current_captain.id, match.available_players[0].id)

    @unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
    def test_only_one_square_is_bolded(self):
        match = self.match()
        while match.phase is Phase.DRAFT:
            self.assertEqual(self.order(match).count("**"), 2)
            match.pick(match.current_captain.id, match.available_players[0].id)

    @unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
    def test_the_last_pick_leaves_a_single_square(self):
        match = self.match()
        picks = len(match.draft_order)
        for _ in range(picks - 1):
            match.pick(match.current_captain.id, match.available_players[0].id)
        self.assertEqual(len(self.order(match).split()), 1)


if __name__ == "__main__":
    unittest.main()
