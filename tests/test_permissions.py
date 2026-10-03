"""Permission tier tests. Skipped if discord.py is not installed."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import discord
    from discord.ext import commands

    from scrimbot.cogs import EXTENSIONS
    from scrimbot.config import Config
    from scrimbot.core import ScrimCore

    HAS_DISCORD = True
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    HAS_DISCORD = False

GUILD = 77
HOSTER_ROLE = 999

ADMIN_COMMANDS = {
    "accountlockgames",
    "channels",
    "cleargames",
    "elo",
    "game",
    "games",
    "listids",
    "liststaffids",
    "pickmode",
    "randomcaptains",
    "randompassword",
    "rcon",
    "region",
    "resetstats",
    "servercleanup",
    "setresultschannel",
    "settings",
    "togglevetoing",
    "voicecleanup",
}
HOSTER_COMMANDS = {
    "blacklist",
    "cancel",
    "forcecaptain",
    "forcedrop",
    "forcejoin",
    "host",
    "redraft",
    "sub",
    "whitelist",
}


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class FakeUser:
    def __init__(self, *, admin=False, manage=False, roles=()) -> None:
        self.id = 1
        self.guild_permissions = discord.Permissions(
            administrator=admin, manage_guild=manage
        )
        self.roles = [FakeRole(r) for r in roles]


class FakeInteraction:
    def __init__(self, user: FakeUser) -> None:
        self.user = user
        self.guild_id = GUILD


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        config = Config(
            token="x", guild_id=None, rcon_host="h", rcon_port=27015, rcon_password="p",
            rcon_enabled=False, connect_address="", steam_api_key="", lobby_size=10,
            pick_timeout=120.0, store_path=os.path.join(self._dir.name, "store.json"),
            default_elo=1000, default_mode="5v5",
        )
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.bot.core = ScrimCore(config)
        await self.bot.core.setup()
        for extension in EXTENSIONS:
            await self.bot.load_extension(extension)
        self.cog = self.bot.get_cog("ScrimCog")

    async def asyncTearDown(self):
        await self.bot.core.close()
        await self.bot.close()
        self._dir.cleanup()

    def command(self, name: str):
        return self.bot.tree.get_command(name)

    # ------------------------------------------------------------ discord UI

    async def test_admin_commands_are_administrator_gated(self):
        for name in sorted(ADMIN_COMMANDS):
            with self.subTest(command=name):
                command = self.command(name)
                self.assertIsNotNone(command, f"/{name} is missing")
                self.assertIsNotNone(command.default_permissions, f"/{name} has no gate")
                self.assertTrue(command.default_permissions.administrator)

    async def test_hoster_commands_stay_visible_for_the_runtime_check(self):
        # Discord cannot express "has role X" in default_member_permissions,
        # so these must not carry a permission gate -- the cog checks instead.
        for name in sorted(HOSTER_COMMANDS):
            with self.subTest(command=name):
                command = self.command(name)
                self.assertIsNotNone(command, f"/{name} is missing")
                self.assertIsNone(command.default_permissions)

    async def test_public_commands_have_no_gate(self):
        for name in ("register", "unregister", "stats", "leaderboard", "notify", "status"):
            with self.subTest(command=name):
                self.assertIsNone(self.command(name).default_permissions)

    # --------------------------------------------------------------- runtime

    async def test_admin_check(self):
        self.assertTrue(self.cog.is_admin(FakeInteraction(FakeUser(admin=True))))
        self.assertFalse(self.cog.is_admin(FakeInteraction(FakeUser(manage=True))))
        self.assertFalse(self.cog.is_admin(FakeInteraction(FakeUser())))

    async def test_hoster_falls_back_to_admins_when_no_role_is_set(self):
        self.assertTrue(self.cog.is_hoster(FakeInteraction(FakeUser(admin=True))))
        self.assertFalse(self.cog.is_hoster(FakeInteraction(FakeUser(manage=True))))
        self.assertFalse(self.cog.is_hoster(FakeInteraction(FakeUser(roles=[HOSTER_ROLE]))))

    async def test_hoster_role_grants_access(self):
        await self.bot.core.store.set_setting(GUILD, "hoster_role", HOSTER_ROLE)
        self.assertTrue(self.cog.is_hoster(FakeInteraction(FakeUser(roles=[HOSTER_ROLE]))))
        self.assertFalse(self.cog.is_hoster(FakeInteraction(FakeUser(roles=[123]))))
        self.assertTrue(self.cog.is_hoster(FakeInteraction(FakeUser(admin=True))))

    async def test_hoster_role_is_per_guild(self):
        await self.bot.core.store.set_setting(GUILD, "hoster_role", HOSTER_ROLE)
        elsewhere = FakeInteraction(FakeUser(roles=[HOSTER_ROLE]))
        elsewhere.guild_id = 12345
        self.assertFalse(self.cog.is_hoster(elsewhere))


if __name__ == "__main__":
    unittest.main()
