"""get5 integration (https://github.com/splewis/get5).

Targets **get5 0.6.0** (CS:GO). That version's match config schema and the
commands used here -- ``get5_loadmatch``, ``get5_loadmatch_url``,
``get5_forceready``, ``get5_endmatch``, ``get5_status`` -- are documented in the
0.6.0 README and ``configs/get5/example_match.json``.

Two ways to hand get5 a match config:

* **URL** (``GET5_BASE_URL``) -- the bot serves the JSON over HTTP and calls
  ``get5_loadmatch_url``. Needs the SteamWorks extension on the game server.
* **File** (``GET5_CONFIG_DIR``) -- the bot writes the JSON somewhere the game
  server can read (a shared/mounted path) and calls ``get5_loadmatch``.

Set neither and the integration stays off.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from dataclasses import dataclass, field

from aiohttp import web

from .match import TEAM_NAMES, Match
from .remote import LocalFiles
from .steam import parse_steam_id

log = logging.getLogger(__name__)

# get5 0.6.0 GameState enum (scripting/include/get5.inc)
GAMESTATE_NONE = 0
GAMESTATE_LIVE = 7
GAMESTATE_POSTGAME = 8

# RCON echoes a cvar read as:  "get5_game_state" = "3" ( def. "0" ) - description
_CVAR_VALUE = re.compile(r'"get5_game_state"\s*=\s*"(-?\d+)"')


# Things get5 replies with over RCON when a load is refused. Its *detailed*
# reasons go to LogError (addons/sourcemod/logs/errors_*.log) and never reach
# us, which is why a load is also verified against get5_status afterwards.
LOAD_REFUSALS = (
    "already loaded",
    "without the steamworks extension",
    "failed to load match config",
    "usage: get5_loadmatch",
)


class Get5Error(RuntimeError):
    """Something stopped the match config from reaching the server."""


def _kv_escape(value) -> str:
    return str(value).replace("\\", "\\\\").replace(chr(34), "\\" + chr(34))


def _kv_scalar(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return _kv_escape(value)


def _kv_block(data: dict, depth: int) -> list[str]:
    pad = "	" * depth
    inner = "	" * (depth + 1)
    lines: list[str] = []
    for key, value in data.items():
        quoted = chr(34) + _kv_escape(key) + chr(34)
        if isinstance(value, dict):
            lines += [pad + quoted, pad + "{"]
            lines += _kv_block(value, depth + 1)
            lines += [pad + "}"]
        elif isinstance(value, (list, tuple)):
            # maplist and similar: each entry is a key with an empty value
            lines += [pad + quoted, pad + "{"]
            for item in value:
                lines.append(inner + chr(34) + _kv_escape(item) + chr(34) + '		""')
            lines += [pad + "}"]
        else:
            body = chr(34) + _kv_scalar(value) + chr(34)
            lines.append(pad + quoted + "		" + body)
    return lines


def to_keyvalues(payload: dict, root: str = "Match") -> str:
    """Render a match config as KeyValues.

    get5 0.6.0 accepts JSON *or* KeyValues, but JSON needs the smjansson
    extension, which is not present on a stock SourceMod install. KeyValues
    parses with no extension at all, so it is the safer default for file mode.
    """
    lines = [chr(34) + root + chr(34), "{"]
    lines += _kv_block(payload, 1)
    lines.append("}")
    return "\n".join(lines) + "\n"


_KV_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*|\s+')


def parse_keyvalues(text: str) -> dict:
    """Parse a Valve KeyValues document into nested dicts.

    Used to read get5's match stats file, which is how the final score is
    recovered when ``get5_status`` is unavailable (no smjansson).
    """
    tokens: list[str] = []
    for match in _KV_TOKEN.finditer(text):
        if match.group(1) is not None:
            tokens.append(match.group(1).replace('\\"', '"').replace("\\\\", "\\"))
        elif match.group(2) is not None:
            tokens.append(match.group(2))

    def block(index: int) -> tuple[dict, int]:
        out: dict = {}
        while index < len(tokens):
            token = tokens[index]
            if token == "}":
                return out, index + 1
            key = token
            index += 1
            if index >= len(tokens):
                break
            if tokens[index] == "{":
                value, index = block(index + 1)
            else:
                value, index = tokens[index], index + 1
            out[key] = value
        return out, index

    start = 0
    # A document is normally a single named root: "Stats" { ... }
    if len(tokens) >= 2 and tokens[1] == "{":
        start = 2
    result, _ = block(start)
    return result


@dataclass
class Get5Match:
    """One prepared match config, waiting to be served or written."""

    matchid: str
    token: str
    payload: dict
    steam_ids: set[str] = field(default_factory=set)
    fmt: str = "json"

    @property
    def extension(self) -> str:
        return "cfg" if self.fmt == "kv" else "json"

    @property
    def filename(self) -> str:
        return f"{self.matchid}-{self.token}.{self.extension}"

    @property
    def path(self) -> str:
        return f"/match/{self.filename}"

    def dumps(self) -> str:
        if self.fmt == "kv":
            return to_keyvalues(self.payload)
        return json.dumps(self.payload, indent=2)

    def name_for(self, steam_id: str) -> str:
        """The roster name behind a steam id, for readable progress messages."""
        for key in ("team1", "team2"):
            players = self.payload.get(key, {}).get("players")
            if isinstance(players, dict) and steam_id in players:
                return players[steam_id] or steam_id
        return steam_id


def build_match_config(
    match: Match,
    steam_ids: dict[int, str],
    *,
    matchid: str,
    team_names: tuple[str, str] = TEAM_NAMES,
    cvars: dict[str, str] | None = None,
) -> dict:
    """Turn a finished draft + veto into a get5 0.6.0 match config.

    The veto already happened in Discord, so ``skip_veto`` is on and the map
    list holds the single chosen map.
    """
    if match.selected_map is None:
        raise Get5Error("No map has been selected yet.")

    teams = []
    for index, roster in enumerate(match.teams):
        players = {}
        for player in roster:
            steam_id = steam_ids.get(player.id)
            if not steam_id:
                raise Get5Error(
                    f"{player.name} has no Steam account registered -- "
                    "get5 cannot lock them to a team."
                )
            players[steam_id] = player.name[:32]
        teams.append(
            {
                "name": team_names[index],
                "tag": team_names[index].replace(" ", "")[:15],
                "players": players,
            }
        )

    config: dict = {
        "matchid": matchid,
        "num_maps": 1,
        "players_per_team": match.team_size,
        "min_players_to_ready": match.team_size,
        "min_spectators_to_ready": 0,
        "skip_veto": True,
        "side_type": "standard",  # knife round for sides
        "maplist": [match.selected_map.name],
        "team1": teams[0],
        "team2": teams[1],
        # get5_check_auths is what pins players to the team they were drafted
        # onto -- anyone not in a player list gets moved out.
        "cvars": {"get5_check_auths": "1"},
    }
    if cvars:
        config["cvars"].update({k: str(v) for k, v in cvars.items()})
    return config


class Get5Manager:
    """Serves match configs and drives get5 over RCON."""

    def __init__(self, config, server, files=None) -> None:
        self.config = config
        self.server = server
        # Where the match configs and stats files live: the bot's own disk,
        # or a remote server's over SFTP. Same paths either way.
        self.files = files or LocalFiles()
        self._matches: dict[str, Get5Match] = {}
        self._runner: web.AppRunner | None = None

    # ------------------------------------------------------------- lifecycle

    @property
    def mode(self) -> str:
        if self.config.get5_base_url:
            return "url"
        if self.config.get5_config_dir:
            return "file"
        return "off"

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def config_format(self) -> str:
        """"kv" or "json". KeyValues needs no SourceMod extension."""
        explicit = (getattr(self.config, "get5_config_format", "") or "").strip().lower()
        if explicit in ("kv", "keyvalues", "cfg"):
            return "kv"
        if explicit == "json":
            return "json"
        # get5_loadmatch_url always fetches JSON; a local file can be either,
        # so default that to KeyValues and skip the smjansson dependency.
        return "json" if self.mode == "url" else "kv"

    async def start(self) -> None:
        if self.mode != "url":
            return
        app = web.Application()
        app.router.add_get("/match/{name}", self._serve)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner, self.config.get5_http_host, self.config.get5_http_port
        )
        await site.start()
        log.info(
            "get5 config server listening on %s:%s (public base %s)",
            self.config.get5_http_host,
            self.config.get5_http_port,
            self.config.get5_base_url,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _serve(self, request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        entry = next(
            (m for m in self._matches.values() if m.filename == name), None
        )
        if entry is None:
            raise web.HTTPNotFound()
        if self.config.get5_auth_header:
            supplied = request.headers.get(self.config.get5_auth_header)
            if supplied != self.config.get5_auth_value:
                raise web.HTTPUnauthorized()
        return web.json_response(entry.payload)

    # --------------------------------------------------------------- matches

    def prepare(
        self,
        match: Match,
        steam_ids: dict[int, str],
        *,
        matchid: str,
        cvars: dict[str, str] | None = None,
    ) -> Get5Match:
        payload = build_match_config(match, steam_ids, matchid=matchid, cvars=cvars)
        entry = Get5Match(
            matchid=matchid,
            token=secrets.token_urlsafe(12),
            payload=payload,
            steam_ids={sid for sid in steam_ids.values() if sid},
            fmt=self.config_format,
        )
        self._matches[matchid] = entry
        return entry

    def forget(self, matchid: str) -> None:
        entry = self._matches.pop(matchid, None)
        if entry is not None and self.mode == "file":
            self.files.discard(
                self.files.join(self.config.get5_config_dir, entry.filename)
            )

    def url_for(self, entry: Get5Match) -> str:
        return self.config.get5_base_url.rstrip("/") + entry.path

    # ----------------------------------------------------------------- rcon

    async def game_state(self) -> int | None:
        """Current get5 GameState from the cvar.

        ``get5_game_state`` is set on every state change and is a plain cvar,
        so unlike ``get5_status`` it can be read without the smjansson
        extension. None when the reply cannot be parsed (plugin not loaded).
        """
        reply = await self.server.execute("get5_game_state")
        match = _CVAR_VALUE.search(reply or "")
        if match:
            return int(match.group(1))
        stripped = (reply or "").strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
        return None

    async def ensure_idle(self) -> bool:
        """Clear a stale match so a new config can load.

        get5 refuses ``get5_loadmatch`` outright while another match is loaded,
        which is what happens when a previous scrim was never closed out.
        """
        try:
            state = await self.game_state()
            if state is None:
                payload = await self.status()
                state = None if payload is None else int(
                    payload.get("gamestate", GAMESTATE_NONE)
                )
        except Exception:  # noqa: BLE001 - the load attempt will report instead
            return False
        if state is None:
            return False  # cannot tell; let the load speak for itself
        if state != GAMESTATE_NONE:
            log.info("Ending a stale get5 match before loading the new one")
            await self.server.execute("get5_endmatch")
            await asyncio.sleep(1.0)
        return True

    async def verify_loaded(self, entry: Get5Match, attempts: int = 4) -> bool | None:
        """Did the server really take the config? None when it cannot be checked."""
        undecided = False
        for _ in range(attempts):
            await asyncio.sleep(1.0)
            try:
                state = await self.game_state()
            except Exception:  # noqa: BLE001
                continue

            if state is None:
                # No cvar to read -- fall back to get5_status, which needs
                # smjansson and may not be there either.
                try:
                    payload = await self.status()
                except Exception:  # noqa: BLE001
                    continue
                if payload is None:
                    undecided = True
                    continue
                state = int(payload.get("gamestate", GAMESTATE_NONE))
                loaded = str(payload.get("matchid", ""))
                if state != GAMESTATE_NONE and loaded and loaded != entry.matchid:
                    raise Get5Error(f"The server loaded a different match (`{loaded}`).")

            if state != GAMESTATE_NONE:
                return True
        return None if undecided else False

    async def load(self, entry: Get5Match) -> str:
        """Tell the server to load this match config. Returns what was sent."""
        if self.mode == "url":
            url = self.url_for(entry)
            command = f'get5_loadmatch_url "{url}"'
            if self.config.get5_auth_header:
                command += (
                    f' "{self.config.get5_auth_header}" "{self.config.get5_auth_value}"'
                )
            self._check_reply(await self.server.execute(command))
            return f"get5_loadmatch_url {url}"

        target = self.files.join(self.config.get5_config_dir, entry.filename)
        await self.files.write(target, entry.dumps())
        # get5_loadmatch takes a path relative to the server's csgo directory,
        # which is rarely the same string as the bot's local write path.
        prefix = self.config.get5_config_prefix.strip("/")
        relative = f"{prefix}/{entry.filename}" if prefix else entry.filename
        self._check_reply(await self.server.execute(f'get5_loadmatch "{relative}"'))
        return f"get5_loadmatch {relative}"

    @staticmethod
    def _check_reply(reply: str) -> None:
        lowered = (reply or "").lower()
        for needle in LOAD_REFUSALS:
            if needle in lowered:
                raise Get5Error(reply.strip()[:300])

    async def swap_player(
        self,
        entry: Get5Match,
        *,
        out_steam_id: str | None,
        in_steam_id: str,
        team: int,
        name: str | None = None,
    ) -> None:
        """Apply a Discord /sub to a match get5 has already loaded.

        Without this the server keeps enforcing the original roster, so the
        replacement gets moved out by get5_check_auths and the player they
        replaced stays whitelisted.
        """
        if out_steam_id:
            await self.server.execute(f'get5_removeplayer "{out_steam_id}"')
            entry.steam_ids.discard(out_steam_id)

        slot = f"team{team + 1}"
        command = f'get5_addplayer "{in_steam_id}" {slot}'
        if name:
            command += f' "{name[:32]}"'
        await self.server.execute(command)
        entry.steam_ids.add(in_steam_id)

        # Keep the stored config in step so a reload or backup restore is right.
        players = entry.payload.get(slot, {}).get("players")
        if isinstance(players, dict):
            if out_steam_id:
                players.pop(out_steam_id, None)
            players[in_steam_id] = (name or "")[:32]

    async def force_start(self) -> None:
        await self.server.execute("get5_forceready")

    async def status(self) -> dict | None:
        """Parse `get5_status`. None when the reply is not JSON.

        0.6.0 needs the smjansson extension for this -- the same one it needs
        to read a JSON match config, so if the config loaded, this works.
        """
        reply = await self.server.execute("get5_status")
        start, end = reply.find("{"), reply.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(reply[start : end + 1])
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _scores(payload: dict) -> tuple[tuple[int, int], tuple[int, int]]:
        """((series1, series2), (map1, map2)) out of a get5_status payload."""
        first, second = payload.get("team1") or {}, payload.get("team2") or {}
        return (
            (int(first.get("series_score", 0)), int(second.get("series_score", 0))),
            (
                int(first.get("current_map_score", 0)),
                int(second.get("current_map_score", 0)),
            ),
        )

    @property
    def stats_dir(self) -> str:
        """Where get5 writes its match stats -- the server's csgo directory.

        get5 exports the stats KeyValues with a path relative to the game
        directory, and our config dir is <csgo>/<config prefix>, so stripping
        the prefix gets us back to the csgo root.
        """
        explicit = getattr(self.config, "get5_stats_dir", "") or ""
        if explicit:
            return explicit
        base = (self.config.get5_config_dir or "").replace("\\", "/").rstrip("/")
        prefix = (self.config.get5_config_prefix or "").replace("\\", "/").strip("/")
        if prefix and base.endswith(prefix):
            return base[: -len(prefix)].rstrip("/")
        return base

    def stats_path(self, matchid: str) -> str:
        template = (
            getattr(self.config, "get5_stats_format", "")
            or "get5_matchstats_{MATCHID}.cfg"
        )
        return self.files.join(self.stats_dir, template.replace("{MATCHID}", matchid))

    def read_stats(self, entry: Get5Match) -> dict | None:
        """Parse get5's stats file from local disk, or None if unreadable.

        Synchronous and local-only, for the repair tool. The match flow uses
        ``fetch_stats``, which also works for a remote server.
        """
        path = self.stats_path(entry.matchid)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                stats = parse_keyvalues(handle.read())
        except OSError:
            return None
        return stats or None

    async def fetch_stats(self, entry: Get5Match) -> dict | None:
        """get5's stats file for this match, wherever the server keeps it."""
        text = await self.files.read(self.stats_path(entry.matchid))
        if text is None:
            return None
        return parse_keyvalues(text) or None

    async def fetch_player_stats(self, entry: Get5Match) -> list[list[dict]] | None:
        return self.player_stats_from(await self.fetch_stats(entry))

    async def fetch_stats_result(
        self, entry: Get5Match
    ) -> tuple[int | None, tuple[int, int]] | None:
        return self.stats_result_from(await self.fetch_stats(entry))

    @staticmethod
    def _map_blocks(stats: dict) -> list[tuple[int, dict]]:
        """Every mapN block, in map order -- map10 sorts after map9."""
        blocks: list[tuple[int, dict]] = []
        for key, value in stats.items():
            if not key.startswith("map") or not isinstance(value, dict):
                continue
            try:
                blocks.append((int(key[3:]), value))
            except ValueError:
                continue
        return sorted(blocks, key=lambda pair: pair[0])

    @staticmethod
    def _map_score(block: dict, team: str) -> int:
        side = block.get(team)
        if not isinstance(side, dict):
            return 0
        try:
            return int(side.get("score", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _played(cls, block: dict) -> bool:
        """Did this map actually produce a result?

        get5 opens the next map's block the moment the server changes level,
        and that stub carries every player's name and "roundsplayed" but no
        score and no winner. Reading it yields a scoreboard of zeroes.
        """
        if str(block.get("winner", "")):
            return True
        return any(cls._map_score(block, team) for team in ("team1", "team2"))

    @classmethod
    def _last_map(cls, stats: dict) -> dict | None:
        """The last map that finished -- never a stub get5 has just opened."""
        played = [block for _, block in cls._map_blocks(stats) if cls._played(block)]
        return played[-1] if played else None

    def read_player_stats(self, entry: Get5Match) -> list[list[dict]] | None:
        return self.player_stats_from(self.read_stats(entry))

    @classmethod
    def player_stats_from(cls, stats: dict | None) -> list[list[dict]] | None:
        """Per-player scoreboard for each team, best first.

        get5 keys players by steamID64 (AuthId_SteamID64), the same form the
        bot stores, so no conversion is needed. Only written at map end.
        """
        if stats is None:
            return None
        last = cls._last_map(stats)
        if last is None:
            return None

        def number(block: dict, field: str) -> int:
            try:
                return int(block.get(field, 0) or 0)
            except (TypeError, ValueError):
                return 0

        teams: list[list[dict]] = []
        for key in ("team1", "team2"):
            block = last.get(key)
            roster: list[dict] = []
            if isinstance(block, dict):
                for steam_id, player in block.items():
                    # "score" and friends are plain values; players are blocks.
                    if not isinstance(player, dict):
                        continue
                    roster.append(
                        {
                            "steam_id": steam_id,
                            "name": player.get("name") or steam_id,
                            "kills": number(player, "kills"),
                            "deaths": number(player, "deaths"),
                            "assists": number(player, "assists"),
                            "damage": number(player, "damage"),
                            "headshots": number(player, "headshot_kills"),
                            "rounds": number(player, "roundsplayed"),
                        }
                    )
            roster.sort(key=lambda p: (-p["kills"], p["deaths"]))
            teams.append(roster)
        return teams

    def read_stats_result(
        self, entry: Get5Match
    ) -> tuple[int | None, tuple[int, int]] | None:
        return self.stats_result_from(self.read_stats(entry))

    @classmethod
    def stats_result_from(
        cls, stats: dict | None
    ) -> tuple[int | None, tuple[int, int]] | None:
        """Winner and score from get5's stats file, or None if unreadable.

        Works without smjansson, unlike get5_status.
        """
        if stats is None:
            return None

        last = cls._last_map(stats)
        scores = (0, 0)
        map_winner = ""
        if last is not None:
            scores = (cls._map_score(last, "team1"), cls._map_score(last, "team2"))
            map_winner = str(last.get("winner", ""))

        winner_text = str(stats.get("winner", "")) or map_winner
        if not winner_text and scores == (0, 0):
            # A stats file get5 has created but not filled in yet. Reporting
            # that as a 0:0 draw would settle a live match on an empty file.
            return None
        if winner_text == "team1":
            return 0, scores
        if winner_text == "team2":
            return 1, scores
        if winner_text:  # "none" -- a draw or an abandoned map
            return None, scores
        if scores[0] != scores[1]:
            return (0 if scores[0] > scores[1] else 1), scores
        return None, scores

    async def wait_for_result(
        self, entry: Get5Match, *, on_update=None
    ) -> tuple[int | None, tuple[int, int]] | None:
        """Poll until get5 reaches the post-game state.

        Returns ``(winner, (score1, score2))`` where winner is 0, 1 or None for
        a draw -- or None if the match never finished inside the timeout.
        """
        interval = self.config.get5_poll_interval
        deadline = (
            asyncio.get_running_loop().time() + self.config.get5_match_timeout
        )
        seen_live = False
        last_series = last_map = (0, 0)
        have_scores = False
        reported: tuple[int, int] | None = None

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(interval)

            # get5_status carries the live scores but needs smjansson. The
            # get5_game_state cvar needs nothing, so it is the fallback for
            # the one thing that decides when to stop: the game state itself.
            payload = None
            try:
                payload = await self.status()
            except Exception:  # noqa: BLE001 - a hiccup should not end the wait
                log.debug("get5_status poll failed", exc_info=True)

            state: int | None = None
            maps: tuple[int, int] | None = None
            if payload is not None:
                series, maps = self._scores(payload)
                if any(series) or any(maps):
                    last_series, last_map, have_scores = series, maps, True
                raw = payload.get("gamestate")
                if raw is not None:
                    try:
                        state = int(raw)
                    except (TypeError, ValueError):
                        state = None

            if state is None:
                # No payload, or a payload whose gamestate we cannot read --
                # never let a surprise in the JSON stall the watcher.
                try:
                    state = await self.game_state()
                except Exception:  # noqa: BLE001
                    log.debug("get5_game_state poll failed", exc_info=True)
                    continue

            if state is None:
                continue
            if state >= GAMESTATE_LIVE:
                seen_live = True
                if maps is not None and on_update is not None and maps != reported:
                    reported = maps
                    await on_update(maps)

            finished = state == GAMESTATE_POSTGAME or (
                seen_live and state <= GAMESTATE_NONE
            )
            if not finished:
                continue

            # Prefer what get5 wrote to disk: it survives the state reset that
            # follows post-game, and it is the only source without smjansson.
            from_file = await self.fetch_stats_result(entry)
            if from_file is not None:
                return from_file
            if have_scores:
                return _decide(last_series, last_map), last_map
            log.warning(
                "Match finished but no result could be read (no smjansson and no "
                "stats file at %s)",
                self.stats_path(entry.matchid),
            )
            return None
        return None


    async def end_match(self, winner: int | None = None) -> None:
        argument = "" if winner is None else f" team{winner + 1}"
        await self.server.execute(f"get5_endmatch{argument}")

    async def connected_steam_ids(self, *, in_game_only: bool = False) -> set[str]:
        """steamID64s currently on the game server, read from `status`.

        With ``in_game_only`` a player who is still connecting or spawning in
        does not count -- they are on the server but not yet on their team,
        and readying the match there starts it a man down.
        """
        players = await self.server.connected_players()
        if in_game_only:
            players = [player for player in players if player.in_game]
        found = set()
        for player in players:
            resolved = parse_steam_id(player.steam_id)
            if resolved:
                found.add(resolved)
        return found

    async def wait_for_roster(
        self,
        entry: Get5Match,
        *,
        on_progress=None,
    ) -> bool:
        """Poll `status` until every drafted player is connected.

        Returns True once the roster is complete (and the caller should start
        the match), False if the wait timed out or was cancelled.
        """
        interval = self.config.get5_poll_interval
        deadline = asyncio.get_running_loop().time() + self.config.get5_ready_timeout
        seen = -1
        empty_polls = 0
        complete_polls = 0
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(interval)
            try:
                connected = await self.connected_steam_ids(in_game_only=True)
            except Exception:  # noqa: BLE001 - a hiccup should not end the wait
                log.debug("status poll failed", exc_info=True)
                continue

            present = entry.steam_ids & connected
            missing = entry.steam_ids - connected
            empty_polls = empty_polls + 1 if not connected else 0

            if len(present) != seen or empty_polls == 3:
                seen = len(present)
                if on_progress is not None:
                    await on_progress(present, missing, connected, empty_polls)
            if entry.steam_ids and present == entry.steam_ids:
                # Hold for a second poll before readying up. One good read can
                # land while someone is mid-reconnect; two in a row means the
                # roster is really settled on their teams.
                complete_polls += 1
                if complete_polls >= self.config.get5_ready_polls:
                    return True
            else:
                complete_polls = 0
        return False


def _decide(series: tuple[int, int], maps: tuple[int, int]) -> int | None:
    """Winner index from the series score, falling back to the map score."""
    for first, second in (series, maps):
        if first != second:
            return 0 if first > second else 1
    return None

