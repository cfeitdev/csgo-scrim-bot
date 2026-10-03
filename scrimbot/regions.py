"""The game servers a match can be played on, one per region.

EU is the home server: normally on the bot's machine, and configured by
the original RCON_* / GET5_* settings, unchanged. NA is optional and reached
over SSH (see remote.py). A match picks its region once, by vote, and every
server call for that match goes through the chosen GameServer from then on.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .get5 import Get5Manager
from .rcon import NullRconClient, RconClient
from .remote import SftpFiles, SshLink
from .server import ServerController

EU = "eu"
NA = "na"
DEFAULT_REGION = EU

REGION_LABELS = {EU: "EU", NA: "NA"}
REGION_FLAGS = {EU: "\U0001f1ea\U0001f1fa", NA: "\U0001f1fa\U0001f1f8"}


try:  # slash-command choices; the rest of this module has no Discord dependency
    from discord import app_commands

    REGION_CHOICES = [
        app_commands.Choice(name=REGION_LABELS[key], value=key) for key in (EU, NA)
    ]
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    REGION_CHOICES = []


def region_name(key: str | None) -> str:
    key = key or DEFAULT_REGION
    return f"{REGION_FLAGS.get(key, '')} {REGION_LABELS.get(key, key.upper())}".strip()


@dataclass
class GameServer:
    key: str
    config: Config  # this server's view: rcon_*, connect and get5_* are its own
    rcon: RconClient
    server: ServerController
    get5: Get5Manager
    link: SshLink | None = None

    @property
    def label(self) -> str:
        return REGION_LABELS.get(self.key, self.key.upper())

    @property
    def name(self) -> str:
        return region_name(self.key)

    @property
    def connect_target(self) -> str:
        return self.config.connect_target

    @property
    def secrets(self) -> list[str]:
        """Addresses that must never be printed in a channel."""
        values = [self.config.rcon_target, self.config.rcon_host]
        if self.link is not None:
            values += [f"{self.link.host}:{self.link.port}", self.link.host]
        return [v for v in values if v]

    async def close(self) -> None:
        await self.rcon.close()
        if self.link is not None:
            await self.link.close()


def build_servers(config: Config) -> dict[str, GameServer]:
    servers = {EU: _build_eu(config)}
    if config.na_enabled:
        servers[NA] = _build_na(config)
    return servers


def _build_eu(config: Config) -> GameServer:
    rcon = (
        RconClient(config.rcon_host, config.rcon_port, config.rcon_password)
        if config.rcon_enabled
        else NullRconClient()
    )
    server = ServerController(rcon)
    return GameServer(EU, config, rcon, server, Get5Manager(config, server))


def _build_na(config: Config) -> GameServer:
    try:
        import asyncssh  # noqa: F401 - fail at startup, not mid-match
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "NA_SSH_HOST is set but the asyncssh package is missing. "
            "Run: pip install -r requirements.txt"
        ) from exc
    na = config.for_na()
    link = SshLink(
        "NA",
        config.na_ssh_host,
        config.na_ssh_port,
        config.na_ssh_user,
        config.na_ssh_key,
        config.na_ssh_known_hosts,
    )
    if config.rcon_enabled:
        rcon: RconClient = RconClient(
            na.rcon_host,
            na.rcon_port,
            na.rcon_password,
            # An SSH connect can take a few seconds on top of the RCON auth.
            timeout=20.0,
            connector=lambda: link.open_tcp(na.rcon_host, na.rcon_port),
        )
    else:
        rcon = NullRconClient()
    server = ServerController(rcon)
    get5 = Get5Manager(na, server, files=SftpFiles(link))
    return GameServer(NA, na, rcon, server, get5, link)
