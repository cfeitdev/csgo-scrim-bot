"""Finished games can be archived to their own channel."""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import discord

    from scrimbot import embeds
    from scrimbot.config import Config
    from scrimbot.core import ScrimCore
    from scrimbot.session import ScrimSession

    HAS_DISCORD = True
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    HAS_DISCORD = False

from scrimbot.match import Match, Player  # noqa: E402

HOST_CHANNEL = 55
RESULTS_CHANNEL = 77
ARCHIVE_URL = "https://discord.com/channels/1/77/999"


class FakeMessage:
    def __init__(self, url=ARCHIVE_URL):
        self.jump_url = url
        self.embeds_seen = []

    async def edit(self, **kwargs):
        if kwargs.get("embed") is not None:
            self.embeds_seen.append(kwargs["embed"])
        return None


class FakeChannel:
    def __init__(self, channel_id=HOST_CHANNEL, fail=False):
        self.id = channel_id
        self.guild = None
        self.fail = fail
        self.lines: list[str] = []
        self.posts: list[dict] = []

    async def send(self, content=None, **kwargs):
        if self.fail:
            raise discord.HTTPException(_Response(), "no permission")
        if content:
            self.lines.append(content)
        self.posts.append(dict(kwargs, content=content))
        return FakeMessage()


class _Response:
    status = 403
    reason = "Forbidden"


class FakeGuild:
    id = 1

    def __init__(self, channels):
        self._channels = {c.id: c for c in channels}
        self.me = None

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_member(self, user_id):
        return None


def roster():
    def row(name, kills):
        return {
            "steam_id": "1",
            "name": name,
            "kills": kills,
            "deaths": 15,
            "assists": 4,
            "damage": 2000,
            "headshots": 8,
            "rounds": 27,
        }

    return [
        [row(f"a{i}", 20 - i) for i in range(5)],
        [row(f"b{i}", 18 - i) for i in range(5)],
    ]


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class ResultsChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        config = Config(
            token="x", guild_id=None, rcon_host="h", rcon_port=27015, rcon_password="p",
            rcon_enabled=False, connect_address="", steam_api_key="", lobby_size=10,
            pick_timeout=None, store_path=os.path.join(self._dir.name, "s.json"),
            default_elo=1000, default_mode="5v5", test_bot_delay=0.001,
        )
        self.core = ScrimCore(config)
        await self.core.setup()

        self.host = FakeChannel(HOST_CHANNEL)
        self.results = FakeChannel(RESULTS_CHANNEL)
        guild = FakeGuild([self.host, self.results])
        self.host.guild = guild
        self.results.guild = guild

        match = Match(host_id=1, size=10)
        for i in range(1, 11):
            match.add_player(Player(1_000_000 + i, f"p{i}", 1000))
        match.start_teams(__import__("random").Random(3))
        while match.phase.name == "DRAFT":
            match.pick(match.current_captain.id, match.available_players[0].id)
        while match.phase.name == "MAP_BAN":
            match.ban(match.current_banner.id, match.remaining_maps[0].name)

        self.session = ScrimSession(self.core, self.host, match)
        self.session.message = FakeMessage()
        self.session.player_stats = roster()
        self.session.final_score = (16, 11)

    async def asyncTearDown(self):
        await self.core.close()
        self._dir.cleanup()

    async def enable(self, channel_id=RESULTS_CHANNEL):
        await self.core.store.set_setting(
            self.session.guild_id, "results_channel", channel_id
        )

    def scrim_embed(self):
        return self.session.message.embeds_seen[-1]

    # ------------------------------------------------------------------ off

    async def test_unset_keeps_the_result_on_the_scrim_message(self):
        await self.session.finish(0)
        self.assertIsNone(self.session.result_url)
        self.assertEqual(self.results.posts, [])
        text = "\n".join(f.value for f in self.scrim_embed().fields)
        self.assertIn("ADR", text)

    # ------------------------------------------------------------------- on

    async def test_the_full_result_goes_to_the_results_channel(self):
        await self.enable()
        await self.session.finish(0)
        self.assertEqual(len(self.results.posts), 1)
        posted = self.results.posts[0]["embed"]
        text = "\n".join(f.value for f in posted.fields)
        self.assertIn("ADR", text)
        self.assertIn("16 : 11", posted.description)

    async def test_the_scrim_message_links_instead_of_repeating_the_scoreboard(self):
        await self.enable()
        await self.session.finish(0)
        self.assertEqual(self.session.result_url, ARCHIVE_URL)
        text = "\n".join(f.value for f in self.scrim_embed().fields)
        self.assertNotIn("ADR", text)
        self.assertIn(ARCHIVE_URL, text)
        # the outcome and the Elo swing still belong on the scrim message
        self.assertIn("1000 ->", text)

    async def test_the_archive_post_does_not_ping_ten_people(self):
        await self.enable()
        await self.session.finish(0)
        mentions = self.results.posts[0]["allowed_mentions"]
        self.assertEqual(mentions.users, False)
        self.assertEqual(mentions.roles, False)

    # --------------------------------------------------------------- guards

    async def test_pointing_it_at_the_host_channel_does_not_double_post(self):
        await self.enable(HOST_CHANNEL)
        await self.session.finish(0)
        self.assertIsNone(self.session.result_url)
        self.assertEqual(self.host.posts, [])
        text = "\n".join(f.value for f in self.scrim_embed().fields)
        self.assertIn("ADR", text)

    async def test_a_deleted_channel_falls_back_to_the_scrim_message(self):
        await self.enable(999999)
        await self.session.finish(0)
        self.assertIsNone(self.session.result_url)
        text = "\n".join(f.value for f in self.scrim_embed().fields)
        self.assertIn("ADR", text)

    async def test_a_failed_post_falls_back_to_the_scrim_message(self):
        self.results.fail = True
        await self.enable()
        with self.assertLogs("scrimbot.session", level=logging.ERROR):
            await self.session.finish(0)
        self.assertIsNone(self.session.result_url)
        text = "\n".join(f.value for f in self.scrim_embed().fields)
        self.assertIn("ADR", text)

    async def test_elo_is_still_written_when_the_archive_post_fails(self):
        self.results.fail = True
        await self.enable()
        with self.assertLogs("scrimbot.session", level=logging.ERROR):
            await self.session.finish(0)
        self.assertTrue(self.session.changes)


@unittest.skipUnless(HAS_DISCORD, "discord.py is not installed")
class ResultEmbedLinkTests(unittest.TestCase):
    def match(self):
        match = Match(host_id=1, size=10)
        for i in range(1, 11):
            match.add_player(Player(1_000_000 + i, f"p{i}", 1000))
        match.start_teams(__import__("random").Random(3))
        while match.phase.name == "DRAFT":
            match.pick(match.current_captain.id, match.available_players[0].id)
        while match.phase.name == "MAP_BAN":
            match.ban(match.current_banner.id, match.remaining_maps[0].name)
        match.finish(0)
        return match

    def test_without_a_url_the_scoreboard_is_inline(self):
        embed = embeds.result_embed(self.match(), {}, roster(), (16, 11))
        text = "\n".join(f.value for f in embed.fields)
        self.assertIn("ADR", text)
        self.assertNotIn("Full scoreboard", text)

    def test_with_a_url_the_scoreboard_is_replaced_by_one_link(self):
        embed = embeds.result_embed(self.match(), {}, roster(), (16, 11), ARCHIVE_URL)
        text = "\n".join(f.value for f in embed.fields)
        self.assertNotIn("ADR", text)
        self.assertEqual(text.count("Full scoreboard"), 1)

    def test_the_link_survives_having_no_player_stats(self):
        embed = embeds.result_embed(self.match(), {}, None, (16, 11), ARCHIVE_URL)
        text = "\n".join(f.value for f in embed.fields)
        self.assertIn(ARCHIVE_URL, text)


if __name__ == "__main__":
    unittest.main()
