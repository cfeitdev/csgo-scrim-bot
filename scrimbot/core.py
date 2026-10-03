"""Shared services every cog talks to: store, game servers, steam, sessions."""

from __future__ import annotations

import logging

from .auditlog import AuditLogger
from .config import Config
from .regions import DEFAULT_REGION, GameServer, build_servers
from .roles import RoleManager
from .steam import SteamClient
from .store import Store
from .voice import VoiceManager

log = logging.getLogger(__name__)


class ScrimCore:
    def __init__(self, config: Config, bot=None) -> None:
        self.config = config
        self.bot = bot
        self.store = Store(config.store_path, config.default_elo)
        # region key -> server. EU always exists; NA only when configured.
        self.servers: dict[str, GameServer] = build_servers(config)
        home = self.servers[DEFAULT_REGION]
        # The home (EU) server, kept under the old names for everything that
        # is not tied to a particular match.
        self.rcon = home.rcon
        self.server = home.server
        self.get5 = home.get5
        self.steam = SteamClient(config.steam_api_key)
        self.roles = RoleManager(self.store)
        self.voice = VoiceManager(self.store)
        self.audit = AuditLogger(bot, self.store)
        # channel id -> session
        self.sessions: dict[int, "object"] = {}

    async def setup(self) -> None:
        await self.store.load()
        await self.get5.start()

    async def close(self) -> None:
        await self.get5.stop()
        for server in self.servers.values():
            await server.close()
        await self.steam.close()

    # --------------------------------------------------------------- servers

    @property
    def regions(self) -> tuple[str, ...]:
        """Regions players vote between. One region means no vote at all."""
        return tuple(self.servers)

    def server_for(self, region: str | None) -> GameServer:
        return self.servers.get(region or DEFAULT_REGION) or self.servers[DEFAULT_REGION]

    @property
    def secrets(self) -> list[str]:
        """Every server address that must be scrubbed from channel output."""
        found: list[str] = []
        for server in self.servers.values():
            found += server.secrets
        # Longest first, so "host:port" is replaced before a bare "host".
        return sorted(set(found), key=len, reverse=True)

    # -------------------------------------------------------------- sessions

    def session_for(self, channel_id: int | None):
        if channel_id is None:
            return None
        session = self.sessions.get(channel_id)
        if session is not None and session.finished:
            del self.sessions[channel_id]
            return None
        return session

    def register(self, channel_id: int, session) -> None:
        self.sessions[channel_id] = session

    def forget(self, session) -> None:
        channel_id = getattr(session.channel, "id", None)
        if channel_id is not None and self.sessions.get(channel_id) is session:
            del self.sessions[channel_id]

    def guild_sessions(self, guild_id: int) -> list:
        return [
            session
            for session in self.sessions.values()
            if getattr(session.guild, "id", None) == guild_id
        ]

    def clear_guild(self, guild_id: int) -> int:
        targets = self.guild_sessions(guild_id)
        for session in targets:
            self.forget(session)
        return len(targets)
