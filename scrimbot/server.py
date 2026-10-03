"""Game-server actions built on top of the raw RCON transport."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from .maps import GameMap
from .rcon import RconAuthError, RconClient, RconError

log = logging.getLogger(__name__)

# Tolerant across CS:GO and CS2 `status` layouts: a leading "# <userid>",
# a quoted name somewhere on the line, and a steam id in any format.
_STATUS_LINE = re.compile(
    r'^#\s*(\d+)\s+.*?"(?P<name>.*)"\s+(?P<steam>STEAM_\d:\d:\d+|\[U:1:\d+\])'
    r'(?P<rest>[^\n]*)',
    re.MULTILINE,
)
# CS:GO reports a connection state after the ping/loss columns. A player who
# is still downloading or spawning in is on the server but not yet on a team.
_STATE = re.compile(r"\b(active|spawning|connecting|challenging)\b")


@dataclass(frozen=True)
class ConnectedPlayer:
    userid: int
    name: str
    steam_id: str
    state: str | None = None  # None when the layout does not report one

    @property
    def in_game(self) -> bool:
        """Actually playing, rather than still connecting or spawning in.

        An unknown state counts as in-game: CS2 and modded layouts do not
        always print the column, and refusing to start is worse than starting
        a moment early.
        """
        return self.state in (None, "active")


def parse_status(output: str) -> list[ConnectedPlayer]:
    players = []
    for match in _STATUS_LINE.finditer(output or ""):
        state = _STATE.search(match.group("rest") or "")
        players.append(
            ConnectedPlayer(
                int(match.group(1)),
                match.group("name"),
                match.group("steam"),
                state.group(1) if state else None,
            )
        )
    return players


class ServerController:
    """Everything the bot asks of the CS server, in one place."""

    def __init__(self, rcon: RconClient) -> None:
        self.rcon = rcon

    async def execute(self, command: str, *, expect_response: bool = True) -> str:
        return await self.rcon.execute(command, expect_response=expect_response)

    async def change_map(self, game_map: GameMap) -> str:
        # changelevel stalls the server mid-load and drops the connection, so
        # it is fired without waiting for a reply.
        return await self.execute(game_map.changelevel_command(), expect_response=False)

    async def set_password(self, password: str | None) -> str:
        value = password or ""
        return await self.execute(f'sv_password "{value}"')

    async def set_team_names(self, first: str, second: str) -> None:
        await self.execute(f'mp_teamname_1 "{first}"')
        await self.execute(f'mp_teamname_2 "{second}"')

    async def wait_until_ready(
        self, timeout: float = 120.0, interval: float = 3.0, on_wait=None
    ) -> bool:
        """Poll until the server answers RCON again after a map change.

        srcds refuses RCON auth while it loads, and how long that takes varies
        with the map and whether it is warm in cache, so waiting a fixed number
        of seconds and hoping is not good enough.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        attempt = 0
        while True:
            try:
                await self.execute("echo scrimbot_ping")
                return True
            except RconAuthError:
                raise  # a bad password will not fix itself
            except RconError as exc:
                if loop.time() >= deadline:
                    log.warning("Server never came back for RCON: %s", exc)
                    return False
                attempt += 1
                if on_wait is not None:
                    await on_wait(attempt)
                await asyncio.sleep(interval)

    async def connected_players(self) -> list[ConnectedPlayer]:
        reply = await self.execute("status")
        players = parse_status(reply)
        if not players and reply.strip():
            # Either nobody is on, or the layout does not match the parser --
            # log the raw reply once so the difference is visible.
            log.debug("status returned no parsable players. Raw reply:\n%s", reply)
        return players

    async def kick_all(self, reason: str = "Scrim over") -> int:
        """Kick everyone currently on the server (post-game cleanup)."""
        players = await self.connected_players()
        kicked = 0
        for player in players:
            try:
                await self.execute(f'kickid {player.userid} "{reason}"')
                kicked += 1
            except Exception:  # noqa: BLE001 - one bad kick must not stop the rest
                log.exception("Failed to kick %s", player.steam_id)
        return kicked

    async def whitelist(self, steam_ids: list[str]) -> None:
        """Push a steam id whitelist, one `sm_` style add per id.

        Servers vary wildly here (SourceMod plugin, sv_whitelist file, a
        matchmaking plugin, ...), so this is the seam to adapt: everything else
        in the bot calls this one method.
        """
        for steam_id in steam_ids:
            await self.execute(f"sm_whitelist_add {steam_id}")
