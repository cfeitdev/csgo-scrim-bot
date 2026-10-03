"""The Steam requirement: no unregistered player can end up in a get5 match."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import discord  # noqa: F401

    from scrimbot.cogs.settings import steam_required_label
    from scrimbot.config import Config
    from scrimbot.core import ScrimCore
    from scrimbot.session import ScrimSession

    HAS_DISCORD = True
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    HAS_DISCORD = False

from scrimbot.match import Match, ScrimError  # noqa: E402

REGISTERED = 501
UNREGISTERED = 502
BLACKLISTED = 503


class FakeMessage:
    async def edit(self, **kwargs):
        return None


class FakeChannel:
    id = 55
    guild = None

    async def send(self, content=None, **kwargs):
        return FakeMessage()


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.display_name = f"user{user_id}"
        self.mention = f"<@{user_id}>"


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class SteamRequirementTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, *, get5: bool, account_lock: bool = False):
        self._dir = tempfile.TemporaryDirectory()
        config = Config(
            token="x", guild_id=None, rcon_host="h", rcon_port=27015, rcon_password="p",
            rcon_enabled=False, connect_address="", steam_api_key="", lobby_size=10,
            pick_timeout=None, store_path=os.path.join(self._dir.name, "s.json"),
            default_elo=1000, default_mode="5v5",
            get5_config_dir=os.path.join(self._dir.name, "get5") if get5 else "",
        )
        self.core = ScrimCore(config)
        await self.core.setup()
        await self.core.store.register(0, REGISTERED, "76561198000000501", None)
        await self.core.store.register(0, BLACKLISTED, "76561198000000503", None)
        await self.core.store.blacklist(0, BLACKLISTED, None, None)
        if account_lock:
            await self.core.store.set_setting(0, "account_lock", True)
        self.session = ScrimSession(self.core, FakeChannel(), Match(host_id=1, size=10))
        self.session.message = FakeMessage()

    async def asyncTearDown(self):
        await self.core.close()
        self._dir.cleanup()

    async def test_forcejoin_refuses_an_unregistered_player_while_get5_is_on(self):
        await self.make(get5=True)
        with self.assertRaises(ScrimError) as ctx:
            await self.session.force_join(FakeUser(UNREGISTERED))
        self.assertIn("/register", str(ctx.exception))
        self.assertFalse(self.session.match.has_player(UNREGISTERED))

    async def test_forcejoin_admits_a_registered_player(self):
        await self.make(get5=True)
        await self.session.force_join(FakeUser(REGISTERED))
        self.assertTrue(self.session.match.has_player(REGISTERED))

    async def test_forcejoin_still_overrides_the_blacklist(self):
        await self.make(get5=True)
        self.assertIsNotNone(self.session.join_blocker(BLACKLISTED))
        await self.session.force_join(FakeUser(BLACKLISTED))
        self.assertTrue(self.session.match.has_player(BLACKLISTED))

    async def test_forcejoin_honours_the_account_lock_without_get5(self):
        await self.make(get5=False, account_lock=True)
        with self.assertRaises(ScrimError):
            await self.session.force_join(FakeUser(UNREGISTERED))

    async def test_with_neither_anyone_can_be_forcejoined(self):
        await self.make(get5=False)
        await self.session.force_join(FakeUser(UNREGISTERED))
        self.assertTrue(self.session.match.has_player(UNREGISTERED))

    async def test_the_join_button_check_is_unchanged(self):
        await self.make(get5=True)
        self.assertIn("get5", self.session.join_blocker(UNREGISTERED))
        self.assertIsNone(self.session.join_blocker(REGISTERED))
        self.assertIn("blacklisted", self.session.join_blocker(BLACKLISTED))


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class SettingsLabelTests(unittest.TestCase):
    def test_get5_makes_it_required_whatever_the_toggle_says(self):
        self.assertIn("get5 requires it", steam_required_label(False, True))
        self.assertIn("get5 requires it", steam_required_label(True, True))

    def test_without_get5_it_shows_the_toggle(self):
        self.assertEqual(steam_required_label(True, False), "`on`")
        self.assertEqual(steam_required_label(False, False), "`off`")


if __name__ == "__main__":
    unittest.main()
