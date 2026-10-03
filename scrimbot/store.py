from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
from datetime import datetime
from typing import Any, Iterable

from .util import utcnow

PICK_MODES = ("captains", "random", "balanced")

# The smallest possible Discord snowflake. Anything below this is not a real
# account (e.g. a /testfill bot) and must never appear in the rankings.
MIN_SNOWFLAKE = 1 << 22

DEFAULT_SETTINGS: dict[str, Any] = {
    # gameplay
    "pick_mode": "captains",  # captains | random | balanced
    "random_captains": True,  # False -> two highest-Elo players captain
    "vetoing": True,  # False -> the bot rolls a random map
    "map_pool": None,  # None -> the built-in pool
    # gatekeeping
    "account_lock": False,  # require /register before queueing
    # server
    "random_password": False,  # randomise sv_password each game
    "server_cleanup": False,  # kick players off the server after a game
    "voice_cleanup": False,  # disconnect players from voice after a game
    # discord wiring
    "notify_role": None,
    "command_log_channel": None,
    "host_channel": None,  # bot-only channel: everyone else's messages are deleted
    "results_channel": None,  # where finished games are archived; None -> the host message
    "staff_role": None,
    "hoster_role": None,  # who may /host, /sub, /forcejoin, /forcedrop, /redraft
    "team1_role": None,  # handed out when teams are formed, stripped when the game ends
    "team2_role": None,
    "registered_role": None,  # mirrors who has a Steam account bound
    "auto_voice": False,  # move players into their team call when teams lock
    "team1_voice": None,  # voice channels /spec and team moves use
    "team2_voice": None,
    "spec_muted": None,  # user ids currently server-muted as spectators
}


class Store:
    """One JSON file holding every guild's settings, players and stats.

    Reads are synchronous against an in-memory snapshot; writes go through
    ``save()``. Swap this class for a database-backed one with the same
    surface and nothing else in the bot changes.
    """

    def __init__(self, path: str, default_elo: int = 1000) -> None:
        self._path = path
        self._default_elo = default_elo
        self._data: dict[str, Any] = {"guilds": {}}
        self._lock = asyncio.Lock()

    @property
    def default_elo(self) -> int:
        return self._default_elo

    # ------------------------------------------------------------ persistence

    async def load(self) -> None:
        self._data = await asyncio.to_thread(self._read)

    def _read(self) -> dict[str, Any]:
        if not os.path.exists(self._path):
            return {"guilds": {}}
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {"guilds": {}}
        data.setdefault("guilds", {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    async def save(self) -> None:
        async with self._lock:
            snapshot = copy.deepcopy(self._data)
        await asyncio.to_thread(self._write, snapshot)

    # ---------------------------------------------------------------- guilds

    def _guild(self, guild_id: int) -> dict[str, Any]:
        guilds = self._data.setdefault("guilds", {})
        guild = guilds.setdefault(str(guild_id), {})
        guild.setdefault("settings", {})
        guild.setdefault("players", {})
        return guild

    def settings(self, guild_id: int) -> dict[str, Any]:
        merged = dict(DEFAULT_SETTINGS)
        merged.update(self._guild(guild_id)["settings"])
        return merged

    def setting(self, guild_id: int, key: str) -> Any:
        return self.settings(guild_id).get(key)

    async def set_setting(self, guild_id: int, key: str, value: Any) -> Any:
        self._guild(guild_id)["settings"][key] = value
        await self.save()
        return value

    async def toggle_setting(self, guild_id: int, key: str, value: bool | None = None) -> bool:
        current = bool(self.setting(guild_id, key))
        new = (not current) if value is None else bool(value)
        await self.set_setting(guild_id, key, new)
        return new

    # --------------------------------------------------------------- players

    def _new_player(self) -> dict[str, Any]:
        return {
            "steam_id": None,
            "steam_name": None,
            "registered_at": None,
            "blacklisted": False,
            "blacklist_reason": None,
            "blacklist_until": None,
            "captain_blacklisted": False,
            "modes": {},
        }

    def player(self, guild_id: int, user_id: int) -> dict[str, Any]:
        players = self._guild(guild_id)["players"]
        record = players.setdefault(str(user_id), self._new_player())
        for key, value in self._new_player().items():
            record.setdefault(key, value)
        return record

    def players(self, guild_id: int) -> dict[int, dict[str, Any]]:
        return {int(uid): rec for uid, rec in self._guild(guild_id)["players"].items()}

    def mode_stats(self, guild_id: int, user_id: int, mode: str) -> dict[str, Any]:
        modes = self.player(guild_id, user_id)["modes"]
        return modes.setdefault(
            mode, {"elo": self._default_elo, "wins": 0, "losses": 0, "ties": 0}
        )

    def elo(self, guild_id: int, user_id: int, mode: str) -> int:
        return int(self.mode_stats(guild_id, user_id, mode)["elo"])

    async def set_elo(self, guild_id: int, user_id: int, mode: str, value: int) -> int:
        stats = self.mode_stats(guild_id, user_id, mode)
        stats["elo"] = max(0, int(value))
        await self.save()
        return stats["elo"]

    async def add_elo(self, guild_id: int, user_id: int, mode: str, delta: int) -> tuple[int, int]:
        stats = self.mode_stats(guild_id, user_id, mode)
        before = int(stats["elo"])
        stats["elo"] = max(0, before + int(delta))
        await self.save()
        return before, stats["elo"]

    async def record_result(
        self,
        guild_id: int,
        mode: str,
        deltas: dict[int, int],
        winners: Iterable[int],
        losers: Iterable[int],
        tie: bool = False,
    ) -> dict[int, tuple[int, int]]:
        """Apply Elo deltas and bump win/loss counters. Returns {uid: (before, after)}."""
        changes: dict[int, tuple[int, int]] = {}
        for user_id, delta in deltas.items():
            stats = self.mode_stats(guild_id, user_id, mode)
            before = int(stats["elo"])
            stats["elo"] = max(0, before + int(delta))
            changes[user_id] = (before, stats["elo"])
        if tie:
            for user_id in list(winners) + list(losers):
                self.mode_stats(guild_id, user_id, mode)["ties"] += 1
        else:
            for user_id in winners:
                self.mode_stats(guild_id, user_id, mode)["wins"] += 1
            for user_id in losers:
                self.mode_stats(guild_id, user_id, mode)["losses"] += 1
        await self.save()
        return changes

    async def set_record(
        self,
        guild_id: int,
        user_id: int,
        mode: str,
        *,
        wins: int | None = None,
        losses: int | None = None,
        add_wins: int = 0,
        add_losses: int = 0,
    ) -> dict[str, Any]:
        stats = self.mode_stats(guild_id, user_id, mode)
        if wins is not None:
            stats["wins"] = max(0, int(wins))
        if losses is not None:
            stats["losses"] = max(0, int(losses))
        stats["wins"] = max(0, stats["wins"] + add_wins)
        stats["losses"] = max(0, stats["losses"] + add_losses)
        await self.save()
        return stats

    async def reset_stats(self, guild_id: int, mode: str | None = None) -> int:
        """Wipe Elo and W/L. Steam registrations and blacklists are kept."""
        touched = 0
        for record in self._guild(guild_id)["players"].values():
            modes = record.get("modes", {})
            targets = [mode] if mode else list(modes)
            for name in targets:
                if name in modes:
                    modes[name] = {
                        "elo": self._default_elo,
                        "wins": 0,
                        "losses": 0,
                        "ties": 0,
                    }
                    touched += 1
        await self.save()
        return touched

    def leaderboard(self, guild_id: int, mode: str) -> list[tuple[int, dict[str, Any]]]:
        rows = []
        for user_id, record in self.players(guild_id).items():
            if user_id < MIN_SNOWFLAKE:
                continue  # a test bot, not a person
            stats = record.get("modes", {}).get(mode)
            if not stats:
                continue
            if not (stats.get("wins") or stats.get("losses") or stats.get("ties")):
                if int(stats.get("elo", self._default_elo)) == self._default_elo:
                    continue
            rows.append((user_id, stats))
        rows.sort(key=lambda row: (-int(row[1].get("elo", 0)), -int(row[1].get("wins", 0))))
        return rows

    # ------------------------------------------------------------ steam bind

    async def register(
        self, guild_id: int, user_id: int, steam_id: str, steam_name: str | None
    ) -> dict[str, Any]:
        record = self.player(guild_id, user_id)
        record["steam_id"] = steam_id
        record["steam_name"] = steam_name
        record["registered_at"] = utcnow().isoformat()
        await self.save()
        return record

    async def unregister(self, guild_id: int, user_id: int) -> bool:
        record = self.player(guild_id, user_id)
        if not record.get("steam_id"):
            return False
        record["steam_id"] = None
        record["steam_name"] = None
        record["registered_at"] = None
        await self.save()
        return True

    def steam_id(self, guild_id: int, user_id: int) -> str | None:
        return self.player(guild_id, user_id).get("steam_id")

    def owner_of_steam_id(self, guild_id: int, steam_id: str) -> int | None:
        for user_id, record in self.players(guild_id).items():
            if record.get("steam_id") == steam_id:
                return user_id
        return None

    def registered(self, guild_id: int) -> list[tuple[int, dict[str, Any]]]:
        return [
            (uid, rec)
            for uid, rec in sorted(self.players(guild_id).items())
            if rec.get("steam_id")
        ]

    # ------------------------------------------------------------- blacklist

    async def blacklist(
        self,
        guild_id: int,
        user_id: int,
        reason: str | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        record = self.player(guild_id, user_id)
        record["blacklisted"] = True
        record["blacklist_reason"] = reason
        record["blacklist_until"] = until.isoformat() if until else None
        await self.save()
        return record

    async def unblacklist(self, guild_id: int, user_id: int) -> bool:
        record = self.player(guild_id, user_id)
        was = bool(record.get("blacklisted"))
        record["blacklisted"] = False
        record["blacklist_reason"] = None
        record["blacklist_until"] = None
        await self.save()
        return was

    def blacklist_state(self, guild_id: int, user_id: int) -> tuple[bool, str | None, datetime | None]:
        """(active, reason, expiry). Expired entries report inactive."""
        record = self.player(guild_id, user_id)
        if not record.get("blacklisted"):
            return False, None, None
        raw = record.get("blacklist_until")
        expiry = None
        if raw:
            try:
                expiry = datetime.fromisoformat(raw)
            except ValueError:
                expiry = None
            if expiry and expiry <= utcnow():
                return False, None, None
        return True, record.get("blacklist_reason"), expiry

    async def set_captain_blacklist(self, guild_id: int, user_id: int, value: bool) -> bool:
        """Bar (or re-allow) a user from being picked captain. Returns the old value."""
        record = self.player(guild_id, user_id)
        was = bool(record.get("captain_blacklisted"))
        record["captain_blacklisted"] = bool(value)
        await self.save()
        return was

    def is_captain_blacklisted(self, guild_id: int, user_id: int) -> bool:
        record = self._guild(guild_id)["players"].get(str(user_id))
        return bool(record and record.get("captain_blacklisted"))

    def captain_blacklisted(self, guild_id: int) -> list[int]:
        return sorted(
            uid for uid, rec in self.players(guild_id).items()
            if rec.get("captain_blacklisted")
        )

    def blacklisted(self, guild_id: int) -> list[tuple[int, dict[str, Any]]]:
        rows = []
        for user_id, record in self.players(guild_id).items():
            active, _reason, _expiry = self.blacklist_state(guild_id, user_id)
            if active:
                rows.append((user_id, record))
        return rows
