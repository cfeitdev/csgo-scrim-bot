from __future__ import annotations

import os
from dataclasses import dataclass, replace

_FALSEY = {"0", "false", "no", "off", ""}


def _env(key: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(key, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value or ""


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int | None
    rcon_host: str
    rcon_port: int
    rcon_password: str
    rcon_enabled: bool
    connect_address: str
    steam_api_key: str
    lobby_size: int
    pick_timeout: float | None
    store_path: str
    default_elo: int
    default_mode: str
    # --- get5 --- (integration is off until a base URL or config dir is set)
    get5_base_url: str = ""
    get5_http_host: str = "0.0.0.0"
    get5_http_port: int = 8080
    get5_config_dir: str = ""
    get5_config_prefix: str = "addons/sourcemod/configs/get5"
    get5_config_format: str = ""  # "kv" | "json"; blank picks per mode
    get5_stats_dir: str = ""      # blank derives the csgo root from the config dir
    get5_stats_format: str = "get5_matchstats_{MATCHID}.cfg"
    get5_auth_header: str = ""
    get5_auth_value: str = ""
    get5_load_delay: float = 15.0
    get5_load_timeout: float = 120.0  # how long to wait for RCON after a map change
    get5_poll_interval: float = 10.0
    get5_ready_timeout: float = 900.0
    get5_ready_polls: int = 2     # consecutive full-roster polls before readying up
    get5_match_timeout: float = 10800.0
    get5_autostart: bool = True
    test_bot_delay: float = 1.5
    # --- NA game server --- (off until NA_SSH_HOST is set; everything above
    # is the EU (home) server, normally on the bot's own machine). The bot reaches
    # NA over one SSH connection: SFTP for get5 files, and RCON tunnelled
    # through it so the RCON port never has to face the internet.
    na_ssh_host: str = ""
    na_ssh_port: int = 22
    na_ssh_user: str = ""
    na_ssh_key: str = ""
    na_ssh_known_hosts: str = ""
    na_rcon_host: str = "127.0.0.1"  # as seen from the NA machine itself
    na_rcon_port: int = 27015
    na_rcon_password: str = ""
    na_connect_address: str = ""
    na_get5_config_dir: str = ""      # path on the NA machine
    na_get5_config_prefix: str = "addons/sourcemod/configs/get5"
    na_get5_stats_dir: str = ""       # path on the NA machine; blank derives it

    @property
    def rcon_target(self) -> str:
        return f"{self.rcon_host}:{self.rcon_port}"

    @property
    def connect_target(self) -> str:
        """What players should type into the console -- may differ from RCON."""
        return self.connect_address or self.rcon_target

    @property
    def na_enabled(self) -> bool:
        return bool(self.na_ssh_host)

    def for_na(self) -> "Config":
        """This config as the NA server sees it.

        Every server-facing setting is swapped for its NA_* counterpart, so
        the existing RCON, server and get5 code runs against NA unchanged.
        URL mode is EU-only: NA is always driven in file mode over SFTP.
        """
        return replace(
            self,
            rcon_host=self.na_rcon_host,
            rcon_port=self.na_rcon_port,
            rcon_password=self.na_rcon_password,
            connect_address=(
                self.na_connect_address or f"{self.na_ssh_host}:{self.na_rcon_port}"
            ),
            get5_base_url="",
            get5_config_dir=self.na_get5_config_dir,
            get5_config_prefix=self.na_get5_config_prefix,
            get5_stats_dir=self.na_get5_stats_dir,
        )

    def validate(self) -> None:
        """Refuse half-configured setups at startup rather than mid-match."""
        if not self.na_enabled:
            return
        missing = [
            key
            for key, value in (
                ("NA_SSH_USER", self.na_ssh_user),
                ("NA_SSH_KEY", self.na_ssh_key),
                ("NA_SSH_KNOWN_HOSTS", self.na_ssh_known_hosts),
                ("NA_RCON_PASSWORD", self.na_rcon_password),
            )
            if not value
        ]
        get5_on = bool(self.get5_base_url or self.get5_config_dir)
        if get5_on and not self.na_get5_config_dir:
            # Otherwise an NA match would silently skip get5 and its team lock.
            missing.append("NA_GET5_CONFIG_DIR")
        if missing:
            raise RuntimeError(
                "NA_SSH_HOST is set, so the NA server needs: " + ", ".join(missing)
            )

    @classmethod
    def from_env(cls) -> "Config":
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ModuleNotFoundError:  # dotenv is a convenience, not a requirement
            pass

        guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
        timeout = float(_env("PICK_TIMEOUT", "120"))
        lobby_size = int(_env("LOBBY_SIZE", "10"))
        if lobby_size < 2 or lobby_size % 2:
            raise RuntimeError("LOBBY_SIZE must be an even number of at least 2.")

        config = cls(
            token=_env("DISCORD_TOKEN", required=True),
            guild_id=int(guild_id) if guild_id else None,
            rcon_host=_env("RCON_HOST", "127.0.0.1"),
            rcon_port=int(_env("RCON_PORT", "27015")),
            rcon_password=_env("RCON_PASSWORD"),
            rcon_enabled=_env("RCON_ENABLED", "1").lower() not in _FALSEY,
            connect_address=_env("CONNECT_ADDRESS"),
            steam_api_key=_env("STEAM_API_KEY"),
            lobby_size=lobby_size,
            pick_timeout=timeout if timeout > 0 else None,
            store_path=_env("STORE_PATH", "data/store.json"),
            default_elo=int(_env("DEFAULT_ELO", "1000")),
            default_mode=_env("DEFAULT_GAMEMODE", "5v5"),
            get5_base_url=_env("GET5_BASE_URL"),
            get5_http_host=_env("GET5_HTTP_HOST", "0.0.0.0"),
            get5_http_port=int(_env("GET5_HTTP_PORT", "8080")),
            get5_config_dir=_env("GET5_CONFIG_DIR"),
            get5_config_prefix=_env("GET5_CONFIG_PREFIX", "addons/sourcemod/configs/get5"),
            get5_config_format=_env("GET5_CONFIG_FORMAT"),
            get5_stats_dir=_env("GET5_STATS_DIR"),
            get5_stats_format=_env(
                "GET5_STATS_FORMAT", "get5_matchstats_{MATCHID}.cfg"
            ),
            get5_auth_header=_env("GET5_AUTH_HEADER"),
            get5_auth_value=_env("GET5_AUTH_VALUE"),
            get5_load_delay=float(_env("GET5_LOAD_DELAY", "15")),
            get5_load_timeout=float(_env("GET5_LOAD_TIMEOUT", "120")),
            get5_poll_interval=float(_env("GET5_POLL_INTERVAL", "10")),
            get5_ready_timeout=float(_env("GET5_READY_TIMEOUT", "900")),
            get5_ready_polls=max(1, int(_env("GET5_READY_POLLS", "2"))),
            get5_match_timeout=float(_env("GET5_MATCH_TIMEOUT", "10800")),
            get5_autostart=_env("GET5_AUTOSTART", "1").lower() not in _FALSEY,
            test_bot_delay=float(_env("TEST_BOT_DELAY", "1.5")),
            na_ssh_host=_env("NA_SSH_HOST"),
            na_ssh_port=int(_env("NA_SSH_PORT", "22")),
            na_ssh_user=_env("NA_SSH_USER"),
            na_ssh_key=_env("NA_SSH_KEY"),
            na_ssh_known_hosts=_env("NA_SSH_KNOWN_HOSTS"),
            na_rcon_host=_env("NA_RCON_HOST", "127.0.0.1"),
            na_rcon_port=int(_env("NA_RCON_PORT", "27015")),
            na_rcon_password=_env("NA_RCON_PASSWORD"),
            na_connect_address=_env("NA_CONNECT_ADDRESS"),
            na_get5_config_dir=_env("NA_GET5_CONFIG_DIR"),
            na_get5_config_prefix=_env(
                "NA_GET5_CONFIG_PREFIX", "addons/sourcemod/configs/get5"
            ),
            na_get5_stats_dir=_env("NA_GET5_STATS_DIR"),
        )
        config.validate()
        return config
