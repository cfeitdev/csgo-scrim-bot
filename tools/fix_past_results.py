"""Repair result messages the bot already posted.

Before the map-block fix, the bot read the *last* map block in get5's stats
file. When the server changed level after a game, get5 had already opened the
next block -- a stub holding every player's name and ``roundsplayed "1"`` and
nothing else -- so results posted with a 0:0 score and a scoreboard of zeroes
under an otherwise correct winner.

This walks back over the bot's own result messages, finds the stats file each
one came from, and rewrites the score and the scoreboard in place. The Elo
lines are left exactly as they are: they were derived from the winner, which
was always correct.

Matching is by *who played*. The people named in the embed are resolved to
steamID64s through the bot's own registrations, and the stats file whose
players are those people is the right file -- whenever it happens to sit on
disk. The map name has to agree too. Timing only breaks ties, because a result
posted in the host channel is an edit of the lobby message and so carries a
timestamp from before the match even started.

    python -m tools.fix_past_results                  # report only
    python -m tools.fix_past_results --apply          # actually edit
    python -m tools.fix_past_results --channel 123 --limit 500 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402

from scrimbot import embeds  # noqa: E402
from scrimbot.config import Config  # noqa: E402
from scrimbot.get5 import Get5Manager, Get5Match  # noqa: E402
from scrimbot.match import TEAM_EMOJI, TEAM_NAMES  # noqa: E402

log = logging.getLogger("fix_past_results")

MAP_RE = re.compile(r"Map:\s*`([^`]+)`")
MATCHID_RE = re.compile(r"get5_matchstats_(\d+)-(\d+)-(\d+)\.cfg$")
# How long after a match starts its result may still be posted.
DEFAULT_WINDOW = 6 * 60 * 60


def team_of_field(name: str) -> int | None:
    """Which team an embed field belongs to, by its heading."""
    for team in (0, 1):
        if name.startswith(f"{TEAM_EMOJI[team]} {TEAM_NAMES[team]}"):
            return team
    return None


# The scoreboard fields are headed with a zero-width space, which str.strip()
# does not remove -- it is not whitespace as far as Python is concerned.
BLANK = "​‌﻿ \t\r\n"


def is_scoreboard_field(field) -> bool:
    name = (field.name or "").strip(BLANK)
    return not name and (field.value or "").lstrip().startswith("```")


MENTION_RE = re.compile(r"<@!?(\d+)>")


def mentioned_ids(embed: discord.Embed) -> set[int]:
    """Every Discord id named in the embed's team fields."""
    found: set[int] = set()
    for field in embed.fields:
        if team_of_field(field.name or "") is None:
            continue
        found.update(int(uid) for uid in MENTION_RE.findall(field.value or ""))
    return found


def is_result_embed(embed: discord.Embed) -> bool:
    if embed.type != "rich" or not embed.fields:
        return False
    teams = {team_of_field(f.name or "") for f in embed.fields}
    return {0, 1} <= teams


class StatsIndex:
    """Every stats file on disk, grouped by the channel that produced it."""

    def __init__(self, manager: Get5Manager) -> None:
        self.manager = manager
        self._cache: dict[str, object] = {}
        self.by_channel: dict[int, list[tuple[int, str]]] = {}
        pattern = os.path.join(manager.stats_dir, "get5_matchstats_*.cfg")
        for path in glob.glob(pattern):
            found = MATCHID_RE.search(path.replace("\\", "/"))
            if not found:
                continue
            _guild, channel, started = (int(part) for part in found.groups())
            self.by_channel.setdefault(channel, []).append((started, path))
        for entries in self.by_channel.values():
            entries.sort()

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.by_channel.values())

    def matchid_of(self, path: str) -> str:
        found = MATCHID_RE.search(path.replace("\\", "/"))
        return "-".join(found.groups()) if found else ""

    def candidates(self, channel_id: int, before: float, window: int) -> list[str]:
        """Stats files from this channel started near ``before``, nearest first.

        The window is symmetric on purpose. A result posted in the host channel
        is an *edit* of the lobby message, so its created_at predates the match
        start; one posted in a results channel comes after it.
        """
        entries = self.by_channel.get(channel_id, [])
        near = [
            (abs(started - before), path)
            for started, path in entries
            if abs(started - before) <= window
        ]
        return [path for _distance, path in sorted(near)]

    def read(self, path: str):
        """(result, players, mapname, steam_ids) for one file, or None."""
        if path in self._cache:
            return self._cache[path]
        entry = Get5Match(
            matchid=self.matchid_of(path), token="", payload={}, steam_ids=set(), fmt="kv"
        )
        stats = self.manager.read_stats(entry)
        found = None
        if stats is not None:
            played = self.manager._last_map(stats)
            if played is not None:
                result = self.manager.read_stats_result(entry)
                players = self.manager.read_player_stats(entry)
                if result is not None and players and any(players):
                    steam_ids = {
                        str(player["steam_id"])
                        for team in players
                        for player in team
                    }
                    found = (result, players, str(played.get("mapname", "")), steam_ids)
        self._cache[path] = found
        return found

    def find(
        self,
        channel_id: int,
        message_time: float,
        game_map: str,
        window: int,
        roster: set[str] | None = None,
        min_overlap: int = 6,
    ):
        """The stats file this message came from, or None.

        Identity beats timing: a stats file whose players are the people named
        in the embed is the right file whenever it turns up, however far from
        the message it sits. Time only breaks ties between equally good
        matches, and the message's own channel is preferred over another's.
        """
        best = None
        for started, path in self.every():
            found = self.read(path)
            if found is None:
                continue
            _result, _players, mapname, steam_ids = found
            if game_map and mapname and mapname != game_map:
                continue

            overlap = len(roster & steam_ids) if roster else 0
            if roster:
                if overlap < min_overlap:
                    continue
            elif abs(started - message_time) > window:
                # Nothing to identify it by, so fall back to being near in time.
                continue

            key = (
                -overlap,
                0 if self.channel_of(path) == channel_id else 1,
                abs(started - message_time),
            )
            if best is None or key < best[0]:
                best = (key, path, found)

        if best is None:
            return None
        _key, path, (result, players, _mapname, _ids) = best
        return path, result, players

    def every(self):
        for channel, entries in self.by_channel.items():
            for started, path in entries:
                yield started, path

    def channel_of(self, path: str) -> int:
        found = MATCHID_RE.search(path.replace("\\", "/"))
        return int(found.group(2)) if found else 0


def repair(embed: discord.Embed, score, players) -> discord.Embed | None:
    """A copy of ``embed`` with the real score and scoreboards, or None if it
    already says the same thing."""
    # Snapshot first: Embed.copy() shares the underlying field list, so
    # clearing the copy would empty the original we are reading from.
    original = [
        (field.name, field.value, field.inline, is_scoreboard_field(field))
        for field in embed.fields
    ]
    fixed = embed.copy()

    game_map = ""
    found = MAP_RE.search(embed.description or "")
    if found:
        game_map = found.group(1)
    headline = [f"**{TEAM_NAMES[0]} {score[0]} : {score[1]} {TEAM_NAMES[1]}**"]
    if game_map:
        headline.append(f"Map: `{game_map}`")
    fixed.description = "  •  ".join(headline)

    before_description = embed.description
    fixed.clear_fields()
    for name, value, inline, scoreboard_field in original:
        if scoreboard_field:
            continue  # re-emitted below, next to its own team
        fixed.add_field(name=name, value=value, inline=inline)
        team = team_of_field(name or "")
        if team is None or team >= len(players):
            continue
        table = embeds.scoreboard(players[team])
        if table:
            fixed.add_field(name="​", value=table, inline=False)

    unchanged = [(f.name, f.value) for f in fixed.fields] == [
        (name, value) for name, value, _inline, _sb in original
    ]
    if unchanged and fixed.description == before_description:
        return None
    return fixed


class Fixer(discord.Client):
    def __init__(self, config: Config, args) -> None:
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.args = args
        self.index = StatsIndex(Get5Manager(config, None))

    async def on_ready(self) -> None:
        try:
            await self.run_scan()
        finally:
            await self.close()

    def channels(self) -> list[int]:
        if self.args.channel:
            return list(self.args.channel)
        found = []
        for guild in self.guilds:
            for key in ("results_channel", "host_channel"):
                value = self.store_setting(guild.id, key)
                if value:
                    found.append(int(value))
        return found

    def store_setting(self, guild_id: int, key: str):
        return self._settings.get(str(guild_id), {}).get(key)

    async def run_scan(self) -> None:
        import json

        try:
            with open(self.config.store_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
        self._settings = {
            gid: guild.get("settings", {})
            for gid, guild in (data.get("guilds") or {}).items()
        }
        # Discord id -> steamID64, so a message can be matched to the stats
        # file whose players are the people it names.
        self._steam: dict[int, str] = {}
        for guild in (data.get("guilds") or {}).values():
            for uid, record in (guild.get("players") or {}).items():
                steam_id = (record or {}).get("steam_id")
                if steam_id:
                    self._steam[int(uid)] = str(steam_id)
        print(f"{len(self._steam)} registered Steam account(s) for matching")

        print(f"{self.index.total} stats file(s) under {self.index.manager.stats_dir}")
        targets = self.channels()
        if not targets:
            print("No channels to scan. Pass --channel <id>.")
            return

        checked = repaired = skipped = 0
        for channel_id in targets:
            channel = self.get_channel(channel_id)
            if channel is None:
                print(f"  #{channel_id}: not visible to the bot, skipping")
                continue
            print(f"\n--- #{channel} ({channel_id})")
            async for message in channel.history(limit=self.args.limit):
                if message.author.id != self.user.id or not message.embeds:
                    continue
                embed = message.embeds[0]
                if not is_result_embed(embed):
                    continue
                checked += 1

                game_map = ""
                found = MAP_RE.search(embed.description or "")
                if found:
                    game_map = found.group(1)

                roster = {
                    self._steam[uid]
                    for uid in mentioned_ids(embed)
                    if uid in self._steam
                }
                hit = self.index.find(
                    channel_id,
                    message.created_at.timestamp(),
                    game_map,
                    self.args.window,
                    roster=roster,
                    min_overlap=self.args.min_overlap,
                )
                if hit is None:
                    skipped += 1
                    why = (
                        f"{len(roster)} of its players registered"
                        if roster
                        else "none of its players are registered, so only timing could match"
                    )
                    print(
                        f"  {message.id}  no stats file matched "
                        f"({game_map or 'no map'}; {why})"
                    )
                    continue

                path, result, players = hit
                _winner, score = result
                fixed = repair(embed, score, players)
                if fixed is None:
                    print(f"  {message.id}  already correct")
                    continue

                print(
                    f"  {message.id}  {game_map or '?'}  ->  {score[0]}:{score[1]}"
                    f"   [{os.path.basename(path)}]"
                )
                if self.args.apply:
                    try:
                        await message.edit(embed=fixed)
                    except discord.HTTPException as exc:
                        print(f"      edit failed: {exc}")
                        continue
                repaired += 1

        verb = "repaired" if self.args.apply else "would repair"
        print(
            f"\n{checked} result message(s) checked, {verb} {repaired}, "
            f"{skipped} with no matching stats file."
        )
        if not self.args.apply and repaired:
            print("Re-run with --apply to write these changes.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channel", type=int, action="append",
        help="Channel id to scan (repeatable). Defaults to the configured "
             "results and host channels.",
    )
    parser.add_argument("--limit", type=int, default=200, help="Messages per channel")
    parser.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW,
        help="How many seconds before a message a match may have started",
    )
    parser.add_argument(
        "--min-overlap", type=int, default=6, dest="min_overlap",
        help="How many of a message's registered players must appear in a "
             "stats file for it to count as the same game (default 6)",
    )
    parser.add_argument("--apply", action="store_true", help="Actually edit the messages")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    config = Config.from_env()
    if not config.get5_config_dir and not config.get5_stats_dir:
        raise SystemExit("GET5_CONFIG_DIR (or GET5_STATS_DIR) must be set.")
    client = Fixer(config, args)
    asyncio.run(client.start(config.token))


if __name__ == "__main__":
    main()
