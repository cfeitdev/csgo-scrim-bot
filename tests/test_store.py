"""Store, Steam ID and duration-parsing tests."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrimbot.steam import (  # noqa: E402
    parse_steam_id,
    to_steam2,
    to_steam3,
    vanity_name,
)
from scrimbot.store import Store  # noqa: E402
from scrimbot.util import parse_duration, utcnow  # noqa: E402

GUILD = 111
MODE = "5v5"


class StoreTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "store.json")
        self.store = Store(self.path, default_elo=1000)
        await self.store.load()

    async def asyncTearDown(self):
        self._dir.cleanup()


class SettingsTests(StoreTestCase):
    async def test_defaults_then_override(self):
        self.assertEqual(self.store.setting(GUILD, "pick_mode"), "captains")
        await self.store.set_setting(GUILD, "pick_mode", "balanced")
        self.assertEqual(self.store.setting(GUILD, "pick_mode"), "balanced")

    async def test_toggle_flips_and_forces(self):
        self.assertFalse(self.store.setting(GUILD, "account_lock"))
        self.assertTrue(await self.store.toggle_setting(GUILD, "account_lock"))
        self.assertFalse(await self.store.toggle_setting(GUILD, "account_lock"))
        self.assertTrue(await self.store.toggle_setting(GUILD, "account_lock", True))
        self.assertTrue(await self.store.toggle_setting(GUILD, "account_lock", True))

    async def test_settings_survive_a_reload(self):
        await self.store.set_setting(GUILD, "notify_role", 42)
        fresh = Store(self.path, default_elo=1000)
        await fresh.load()
        self.assertEqual(fresh.setting(GUILD, "notify_role"), 42)

    async def test_guilds_are_isolated(self):
        await self.store.set_setting(GUILD, "vetoing", False)
        self.assertTrue(self.store.setting(222, "vetoing"))


class RatingTests(StoreTestCase):
    async def test_new_player_gets_the_default(self):
        self.assertEqual(self.store.elo(GUILD, 1, MODE), 1000)

    async def test_add_and_set(self):
        self.assertEqual(await self.store.add_elo(GUILD, 1, MODE, 50), (1000, 1050))
        self.assertEqual(await self.store.set_elo(GUILD, 1, MODE, 700), 700)

    async def test_elo_never_goes_negative(self):
        await self.store.add_elo(GUILD, 1, MODE, -5000)
        self.assertEqual(self.store.elo(GUILD, 1, MODE), 0)

    async def test_modes_are_separate_buckets(self):
        await self.store.set_elo(GUILD, 1, "5v5", 1500)
        self.assertEqual(self.store.elo(GUILD, 1, "2v2"), 1000)

    async def test_record_result_moves_elo_and_counters(self):
        winners, losers = [1, 2], [3, 4]
        deltas = {1: 16, 2: 16, 3: -16, 4: -16}
        changes = await self.store.record_result(GUILD, MODE, deltas, winners, losers)
        self.assertEqual(changes[1], (1000, 1016))
        self.assertEqual(changes[3], (1000, 984))
        self.assertEqual(self.store.mode_stats(GUILD, 1, MODE)["wins"], 1)
        self.assertEqual(self.store.mode_stats(GUILD, 3, MODE)["losses"], 1)
        self.assertEqual(self.store.mode_stats(GUILD, 1, MODE)["losses"], 0)

    async def test_tie_bumps_ties_not_wins(self):
        await self.store.record_result(GUILD, MODE, {1: 0, 2: 0}, [1], [2], tie=True)
        self.assertEqual(self.store.mode_stats(GUILD, 1, MODE)["ties"], 1)
        self.assertEqual(self.store.mode_stats(GUILD, 1, MODE)["wins"], 0)

    async def test_manual_record_edits(self):
        await self.store.set_record(GUILD, 1, MODE, wins=10, losses=3)
        stats = await self.store.set_record(GUILD, 1, MODE, add_wins=2, add_losses=-1)
        self.assertEqual((stats["wins"], stats["losses"]), (12, 2))

    async def test_leaderboard_sorted_and_filtered(self):
        # Real Discord snowflakes -- the leaderboard ignores anything smaller.
        alice, bob, idle = 111111111111111111, 222222222222222222, 333333333333333333
        await self.store.set_elo(GUILD, alice, MODE, 1200)
        await self.store.set_elo(GUILD, bob, MODE, 1400)
        self.store.mode_stats(GUILD, idle, MODE)  # touched but never played
        rows = self.store.leaderboard(GUILD, MODE)
        self.assertEqual([uid for uid, _ in rows], [bob, alice])

    async def test_leaderboard_excludes_non_discord_ids(self):
        # /testfill bots use small ids; they must never be ranked.
        real = 444444444444444444
        await self.store.set_elo(GUILD, real, MODE, 1200)
        await self.store.set_elo(GUILD, 1001, MODE, 1500)
        await self.store.set_record(GUILD, 1001, MODE, wins=9)
        rows = self.store.leaderboard(GUILD, MODE)
        self.assertEqual([uid for uid, _ in rows], [real])

    async def test_reset_keeps_registrations(self):
        await self.store.register(GUILD, 1, "76561197960287930", "player")
        await self.store.set_elo(GUILD, 1, MODE, 1500)
        await self.store.set_record(GUILD, 1, MODE, wins=4, losses=1)
        await self.store.reset_stats(GUILD)
        self.assertEqual(self.store.elo(GUILD, 1, MODE), 1000)
        self.assertEqual(self.store.mode_stats(GUILD, 1, MODE)["wins"], 0)
        self.assertEqual(self.store.steam_id(GUILD, 1), "76561197960287930")


class RegistrationTests(StoreTestCase):
    async def test_register_and_lookup_owner(self):
        await self.store.register(GUILD, 7, "76561197960287930", "gaben")
        self.assertEqual(self.store.owner_of_steam_id(GUILD, "76561197960287930"), 7)
        self.assertEqual(len(self.store.registered(GUILD)), 1)

    async def test_unregister(self):
        await self.store.register(GUILD, 7, "76561197960287930", "gaben")
        self.assertTrue(await self.store.unregister(GUILD, 7))
        self.assertFalse(await self.store.unregister(GUILD, 7))
        self.assertEqual(self.store.registered(GUILD), [])


class BlacklistTests(StoreTestCase):
    async def test_permanent_blacklist(self):
        await self.store.blacklist(GUILD, 5, "griefing")
        active, reason, expiry = self.store.blacklist_state(GUILD, 5)
        self.assertTrue(active)
        self.assertEqual(reason, "griefing")
        self.assertIsNone(expiry)

    async def test_expired_blacklist_is_inactive(self):
        await self.store.blacklist(GUILD, 5, "afk", utcnow() - timedelta(minutes=1))
        active, _reason, _expiry = self.store.blacklist_state(GUILD, 5)
        self.assertFalse(active)
        self.assertEqual(self.store.blacklisted(GUILD), [])

    async def test_future_blacklist_is_active(self):
        await self.store.blacklist(GUILD, 5, "afk", utcnow() + timedelta(days=1))
        self.assertTrue(self.store.blacklist_state(GUILD, 5)[0])
        self.assertEqual(len(self.store.blacklisted(GUILD)), 1)

    async def test_whitelist_clears_it(self):
        await self.store.blacklist(GUILD, 5, "afk")
        self.assertTrue(await self.store.unblacklist(GUILD, 5))
        self.assertFalse(self.store.blacklist_state(GUILD, 5)[0])


class SteamIdTests(unittest.TestCase):
    # 76561197960287930 is Gabe Newell's public profile id.
    ID64 = "76561197960287930"

    def test_steam64_passthrough(self):
        self.assertEqual(parse_steam_id(self.ID64), self.ID64)

    def test_steam2_roundtrip(self):
        self.assertEqual(to_steam2(self.ID64), "STEAM_1:0:11101")
        self.assertEqual(parse_steam_id("STEAM_0:0:11101"), self.ID64)

    def test_steam3_roundtrip(self):
        self.assertEqual(to_steam3(self.ID64), "[U:1:22202]")
        self.assertEqual(parse_steam_id("[U:1:22202]"), self.ID64)

    def test_profile_url(self):
        self.assertEqual(
            parse_steam_id(f"https://steamcommunity.com/profiles/{self.ID64}/"), self.ID64
        )

    def test_vanity_url_needs_lookup(self):
        url = "https://steamcommunity.com/id/gabelogannewell"
        self.assertIsNone(parse_steam_id(url))
        self.assertEqual(vanity_name(url), "gabelogannewell")

    def test_junk_is_rejected(self):
        self.assertIsNone(parse_steam_id("not a steam id at all!!"))
        self.assertIsNone(vanity_name("not a steam id at all!!"))


class RegistrationInputTests(unittest.IsolatedAsyncioTestCase):
    """/register accepts STEAM_X:Y:Z and nothing else."""

    def setUp(self):
        from scrimbot.steam import SteamClient

        self.client = SteamClient(None)

    async def accepts(self, value):
        account = await self.client.lookup(value)
        return account.steam_id64

    async def test_steam2_is_accepted(self):
        self.assertEqual(await self.accepts("STEAM_1:0:11101"), "76561197960287930")
        self.assertEqual(await self.accepts("STEAM_0:0:11101"), "76561197960287930")

    async def test_case_and_padding_are_forgiven(self):
        self.assertEqual(await self.accepts("  steam_1:0:11101  "), "76561197960287930")
        self.assertEqual(await self.accepts('"STEAM_1:0:11101"'), "76561197960287930")

    async def test_odd_parity_bit(self):
        self.assertEqual(await self.accepts("STEAM_1:1:11101"), "76561197960287931")

    async def test_steam64_is_now_rejected(self):
        from scrimbot.steam import SteamError

        with self.assertRaises(SteamError) as ctx:
            await self.accepts("76561197960287930")
        self.assertIn("STEAM_1:0:12345678", str(ctx.exception))

    async def test_steam3_is_rejected(self):
        from scrimbot.steam import SteamError

        with self.assertRaises(SteamError):
            await self.accepts("[U:1:22202]")

    async def test_profile_url_is_rejected(self):
        from scrimbot.steam import SteamError

        with self.assertRaises(SteamError):
            await self.accepts("https://steamcommunity.com/profiles/76561197960287930")

    async def test_vanity_url_is_rejected(self):
        from scrimbot.steam import SteamError

        with self.assertRaises(SteamError):
            await self.accepts("https://steamcommunity.com/id/gabelogannewell")

    async def test_junk_is_rejected_with_guidance(self):
        from scrimbot.steam import SteamError

        with self.assertRaises(SteamError) as ctx:
            await self.accepts("my steam account")
        self.assertIn("status", str(ctx.exception))

    async def test_status_parsing_still_accepts_every_format(self):
        # The server `status` output uses STEAM_ or [U:1:...]; get5's roster
        # watcher depends on both still parsing.
        from scrimbot.steam import parse_steam_id

        self.assertEqual(parse_steam_id("[U:1:22202]"), "76561197960287930")
        self.assertEqual(parse_steam_id("STEAM_1:0:11101"), "76561197960287930")


class DurationTests(unittest.TestCase):
    def test_simple_units(self):
        self.assertEqual(parse_duration("7d"), timedelta(days=7))
        self.assertEqual(parse_duration("30m"), timedelta(minutes=30))
        self.assertEqual(parse_duration("1w"), timedelta(weeks=1))

    def test_compound(self):
        self.assertEqual(parse_duration("1h30m"), timedelta(hours=1, minutes=30))

    def test_permanent(self):
        self.assertIsNone(parse_duration(None))
        self.assertIsNone(parse_duration("perm"))

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_duration("soonish")


if __name__ == "__main__":
    unittest.main()


class CaptainBlacklistStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_persists_and_lists(self):
        import tempfile

        from scrimbot.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            store = Store(path, 1000)
            await store.load()
            self.assertFalse(store.is_captain_blacklisted(1, 42))
            self.assertFalse(await store.set_captain_blacklist(1, 42, True))
            self.assertTrue(store.is_captain_blacklisted(1, 42))
            self.assertEqual(store.captain_blacklisted(1), [42])
            self.assertEqual(store.captain_blacklisted(2), [])

            fresh = Store(path, 1000)
            await fresh.load()
            self.assertTrue(fresh.is_captain_blacklisted(1, 42))
            self.assertTrue(await fresh.set_captain_blacklist(1, 42, False))
            self.assertEqual(fresh.captain_blacklisted(1), [])
