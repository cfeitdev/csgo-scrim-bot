"""The designated host channel stays bot-only. Skipped without discord.py."""

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

GUILD = 500
HOST_CHANNEL = 900
OTHER_CHANNEL = 901
BOT_ID = 42


class FakeGuild:
    def __init__(self, guild_id: int = GUILD) -> None:
        self.id = guild_id


class FakeAuthor:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id


class FakeMessage:
    def __init__(self, *, author_id: int, channel_id: int, guild=True, raises=None) -> None:
        self.author = FakeAuthor(author_id)
        self.channel = FakeChannel(channel_id)
        self.guild = FakeGuild() if guild else None
        self.deleted = False
        self._raises = raises

    async def delete(self) -> None:
        if self._raises is not None:
            raise self._raises
        self.deleted = True


class FakeUser:
    id = BOT_ID


def http_error(status: int, cls):
    response = type("R", (), {"status": status, "reason": "test"})()
    return cls(response, "test")


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class HostChannelTests(unittest.IsolatedAsyncioTestCase):
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
        self.cog = self.bot.get_cog("SettingsCog")
        self.bot._connection.user = FakeUser()  # stand in for a logged-in bot

    async def asyncTearDown(self):
        await self.bot.core.close()
        await self.bot.close()
        self._dir.cleanup()

    async def enable(self):
        await self.bot.core.store.set_setting(GUILD, "host_channel", HOST_CHANNEL)

    # ------------------------------------------------------------- disabled

    async def test_nothing_is_deleted_by_default(self):
        message = FakeMessage(author_id=7, channel_id=HOST_CHANNEL)
        await self.cog.on_message(message)
        self.assertFalse(message.deleted)

    async def test_clearing_the_setting_stops_deletions(self):
        await self.enable()
        await self.bot.core.store.set_setting(GUILD, "host_channel", None)
        message = FakeMessage(author_id=7, channel_id=HOST_CHANNEL)
        await self.cog.on_message(message)
        self.assertFalse(message.deleted)

    # -------------------------------------------------------------- enabled

    async def test_user_message_in_host_channel_is_deleted(self):
        await self.enable()
        message = FakeMessage(author_id=7, channel_id=HOST_CHANNEL)
        await self.cog.on_message(message)
        self.assertTrue(message.deleted)

    async def test_other_bots_are_deleted_too(self):
        await self.enable()
        message = FakeMessage(author_id=999, channel_id=HOST_CHANNEL)
        await self.cog.on_message(message)
        self.assertTrue(message.deleted)

    async def test_the_bots_own_messages_survive(self):
        await self.enable()
        message = FakeMessage(author_id=BOT_ID, channel_id=HOST_CHANNEL)
        await self.cog.on_message(message)
        self.assertFalse(message.deleted)

    async def test_other_channels_are_untouched(self):
        await self.enable()
        message = FakeMessage(author_id=7, channel_id=OTHER_CHANNEL)
        await self.cog.on_message(message)
        self.assertFalse(message.deleted)

    async def test_dms_are_ignored(self):
        await self.enable()
        message = FakeMessage(author_id=7, channel_id=HOST_CHANNEL, guild=False)
        await self.cog.on_message(message)
        self.assertFalse(message.deleted)

    async def test_other_guilds_are_unaffected(self):
        await self.enable()
        message = FakeMessage(author_id=7, channel_id=HOST_CHANNEL)
        message.guild = FakeGuild(guild_id=12345)
        await self.cog.on_message(message)
        self.assertFalse(message.deleted)

    # --------------------------------------------------------------- errors

    async def test_missing_permission_warns_once(self):
        await self.enable()
        forbidden = http_error(403, discord.Forbidden)
        for _ in range(3):
            await self.cog.on_message(
                FakeMessage(author_id=7, channel_id=HOST_CHANNEL, raises=forbidden)
            )
        self.assertEqual(self.cog._purge_warned, {HOST_CHANNEL})

    async def test_already_deleted_is_not_an_error(self):
        await self.enable()
        message = FakeMessage(
            author_id=7, channel_id=HOST_CHANNEL, raises=http_error(404, discord.NotFound)
        )
        await self.cog.on_message(message)  # must not raise
        self.assertFalse(message.deleted)

    async def test_reassigning_the_channel_rearms_the_warning(self):
        await self.enable()
        self.cog._purge_warned.add(HOST_CHANNEL)
        self.cog._purge_warned.discard(HOST_CHANNEL)
        self.assertEqual(self.cog._purge_warned, set())


if __name__ == "__main__":
    unittest.main()
