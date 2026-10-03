"""Command logging: formatting, redaction and channel resolution."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import discord

    from scrimbot.auditlog import AuditLogger, flatten_options, redact
    from scrimbot.store import Store

    HAS_DISCORD = True
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    HAS_DISCORD = False

GUILD = 800
LOG_CHANNEL = 900
OTHER_CHANNEL = 901


class FakeChannel:
    def __init__(self, channel_id, can_send=True) -> None:
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.sent: list = []
        self._can_send = can_send

    def permissions_for(self, member):
        return discord.Permissions(
            send_messages=self._can_send, embed_links=self._can_send
        )

    async def send(self, *, embed=None, **kwargs):
        self.sent.append(embed)


class FakeUser:
    def __init__(self, user_id=7) -> None:
        self.id = user_id
        self.mention = f"<@{user_id}>"

    def __str__(self):
        return "someone#0001"


class FakeGuild:
    def __init__(self, channels) -> None:
        self.id = GUILD
        self._channels = {c.id: c for c in channels}
        self.me = object()

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


class FakeInteraction:
    def __init__(self, guild, data, user=None, channel_id=555) -> None:
        self.guild = guild
        self.guild_id = getattr(guild, "id", None)
        self.data = data
        self.user = user or FakeUser()
        self.channel_id = channel_id


class FakeCommand:
    def __init__(self, name) -> None:
        self.qualified_name = name


class RedactionTests(unittest.TestCase):
    def test_password_value_is_masked(self):
        self.assertEqual(redact("rcon_password hunter2"), "rcon_password ***")
        self.assertEqual(redact('sv_password "abc"'), "sv_password ***")

    def test_case_insensitive(self):
        self.assertEqual(redact("SV_PASSWORD abc"), "SV_PASSWORD ***")

    def test_harmless_commands_untouched(self):
        self.assertEqual(redact("changelevel de_dust2"), "changelevel de_dust2")
        self.assertEqual(redact("status"), "status")

    def test_token_and_secret_are_masked(self):
        self.assertEqual(redact("set_token abcdef"), "set_token ***")
        self.assertEqual(redact("api_key 12345"), "api_key ***")


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class OptionFormattingTests(unittest.TestCase):
    def test_plain_options(self):
        path, options = flatten_options(
            [{"name": "amount", "type": 4, "value": 25}]
        )
        self.assertEqual(path, [])
        self.assertEqual(options, [("amount", "25")])

    def test_user_option_renders_as_a_mention(self):
        _path, options = flatten_options([{"name": "user", "type": 6, "value": "42"}])
        self.assertEqual(options, [("user", "<@42>")])

    def test_channel_and_role_options(self):
        _path, options = flatten_options(
            [
                {"name": "channel", "type": 7, "value": "5"},
                {"name": "role", "type": 8, "value": "6"},
            ]
        )
        self.assertEqual(options, [("channel", "<#5>"), ("role", "<@&6>")])

    def test_booleans_are_readable(self):
        _path, options = flatten_options([{"name": "clear", "type": 5, "value": True}])
        self.assertEqual(options, [("clear", "true")])

    def test_subcommand_path_is_extracted(self):
        path, options = flatten_options(
            [
                {
                    "name": "give",
                    "type": 1,
                    "options": [
                        {"name": "user", "type": 6, "value": "42"},
                        {"name": "amount", "type": 4, "value": 50},
                    ],
                }
            ]
        )
        self.assertEqual(path, ["give"])
        self.assertEqual(options, [("user", "<@42>"), ("amount", "50")])

    def test_secret_looking_option_names_are_masked(self):
        _path, options = flatten_options(
            [{"name": "password", "type": 3, "value": "hunter2"}]
        )
        self.assertEqual(options, [("password", "***")])

    def test_rcon_password_value_is_redacted(self):
        _path, options = flatten_options(
            [{"name": "command", "type": 3, "value": "rcon_password hunter2"}]
        )
        self.assertEqual(options, [("command", "rcon_password ***")])


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class LoggerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self._dir.name, "s.json"), 1000)
        await self.store.load()
        self.logger = AuditLogger(None, self.store)
        self.channel = FakeChannel(LOG_CHANNEL)
        self.guild = FakeGuild([self.channel, FakeChannel(OTHER_CHANNEL)])

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def enable(self):
        await self.store.set_setting(GUILD, "command_log_channel", LOG_CHANNEL)

    async def test_nothing_is_logged_by_default(self):
        interaction = FakeInteraction(self.guild, {"options": []})
        await self.logger.log_command(interaction, FakeCommand("host"))
        self.assertEqual(self.channel.sent, [])

    async def test_successful_command_is_logged(self):
        await self.enable()
        interaction = FakeInteraction(
            self.guild, {"options": [{"name": "gamemode", "type": 3, "value": "5v5"}]}
        )
        await self.logger.log_command(interaction, FakeCommand("host"))
        embed = self.channel.sent[0]
        self.assertEqual(embed.title, "/host")
        self.assertIn("<@7>", embed.description)
        self.assertEqual(embed.fields[0].name, "Arguments")
        self.assertIn("gamemode: 5v5", embed.fields[0].value)

    async def test_subcommand_shows_the_full_path(self):
        await self.enable()
        data = {
            "options": [
                {
                    "name": "give",
                    "type": 1,
                    "options": [{"name": "amount", "type": 4, "value": 50}],
                }
            ]
        }
        await self.logger.log_command(FakeInteraction(self.guild, data), FakeCommand("elo give"))
        self.assertEqual(self.channel.sent[0].title, "/elo give")

    async def test_failures_are_logged_in_red(self):
        await self.enable()
        interaction = FakeInteraction(self.guild, {"options": []})
        await self.logger.log_command(
            interaction, FakeCommand("rcon"), RuntimeError("boom")
        )
        embed = self.channel.sent[0]
        self.assertIn("failed", embed.title)
        self.assertEqual(embed.color, discord.Color.red())
        self.assertIn("RuntimeError: boom", embed.fields[-1].value)

    async def test_rcon_password_never_reaches_the_channel(self):
        await self.enable()
        data = {"options": [{"name": "command", "type": 3, "value": "rcon_password hunter2"}]}
        await self.logger.log_command(FakeInteraction(self.guild, data), FakeCommand("rcon"))
        rendered = self.channel.sent[0].fields[0].value
        self.assertNotIn("hunter2", rendered)
        self.assertIn("***", rendered)

    async def test_actions_are_logged(self):
        await self.enable()
        await self.logger.log_action(
            self.guild, FakeUser(), "Cancelled the game", "5v5 lobby"
        )
        embed = self.channel.sent[0]
        self.assertEqual(embed.title, "Cancelled the game")
        self.assertIn("5v5 lobby", embed.description)

    async def test_channel_from_another_guild_is_not_used(self):
        await self.store.set_setting(GUILD, "command_log_channel", 12345)
        await self.logger.log_action(self.guild, FakeUser(), "x")
        self.assertEqual(self.channel.sent, [])

    async def test_missing_send_permission_warns_once(self):
        blocked = FakeChannel(LOG_CHANNEL, can_send=False)
        guild = FakeGuild([blocked])
        await self.enable()
        for _ in range(3):
            await self.logger.log_action(guild, FakeUser(), "x")
        self.assertEqual(blocked.sent, [])
        self.assertEqual(self.logger._warned, {LOG_CHANNEL})

    async def test_dms_are_ignored(self):
        await self.enable()
        await self.logger.log_command(FakeInteraction(None, {}), FakeCommand("host"))
        self.assertEqual(self.channel.sent, [])


if __name__ == "__main__":
    unittest.main()
