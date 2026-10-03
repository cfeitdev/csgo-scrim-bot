"""Steam ID parsing/conversion and optional Steam Web API lookups."""

from __future__ import annotations

import re
from dataclasses import dataclass

import aiohttp  # ships with discord.py

STEAM64_BASE = 76561197960265728

_STEAM2_RE = re.compile(r"^STEAM_([0-5]):([01]):(\d+)$", re.IGNORECASE)
_STEAM3_RE = re.compile(r"^\[?U:1:(\d+)\]?$", re.IGNORECASE)
_STEAM64_RE = re.compile(r"^7656119\d{10}$")
_PROFILE_RE = re.compile(r"steamcommunity\.com/(profiles|id)/([^/?#\s]+)", re.IGNORECASE)

API = "https://api.steampowered.com"


class SteamError(Exception):
    """Anything the user needs to fix about their Steam input."""


@dataclass(frozen=True)
class SteamAccount:
    steam_id64: str
    name: str | None = None
    profile_url: str | None = None

    @property
    def steam_id2(self) -> str:
        return to_steam2(self.steam_id64)

    @property
    def steam_id3(self) -> str:
        return to_steam3(self.steam_id64)


def to_steam2(steam_id64: str | int) -> str:
    """765611980... -> STEAM_1:Y:Z (the format CS whitelists usually want)."""
    account = int(steam_id64) - STEAM64_BASE
    return f"STEAM_1:{account % 2}:{account // 2}"


def to_steam3(steam_id64: str | int) -> str:
    return f"[U:1:{int(steam_id64) - STEAM64_BASE}]"


def parse_steam_id(value: str) -> str | None:
    """Accept a steamID64 / STEAM_x:y:z / [U:1:n] / profile URL. None if vanity."""
    text = value.strip()

    match = _PROFILE_RE.search(text)
    if match:
        kind, tail = match.group(1).lower(), match.group(2)
        if kind == "profiles":
            text = tail
        else:
            return None  # /id/<vanity> needs an API lookup

    if _STEAM64_RE.match(text):
        return text

    match = _STEAM2_RE.match(text)
    if match:
        _universe, parity, account = match.groups()
        return str(int(account) * 2 + int(parity) + STEAM64_BASE)

    match = _STEAM3_RE.match(text)
    if match:
        return str(int(match.group(1)) + STEAM64_BASE)

    return None


def vanity_name(value: str) -> str | None:
    """Pull the vanity portion out of a profile URL or bare custom name."""
    text = value.strip()
    match = _PROFILE_RE.search(text)
    if match and match.group(1).lower() == "id":
        return match.group(2)
    if re.fullmatch(r"[A-Za-z0-9_.\-]{2,64}", text) and not _STEAM64_RE.match(text):
        return text
    return None


class SteamClient:
    """Thin Steam Web API wrapper.

    ``/register`` only accepts STEAM_X:Y:Z, so no API key is needed to sign
    people up. A key only adds persona-name lookup on top.
    """

    def __init__(self, api_key: str | None = None, *, timeout: float = 10.0) -> None:
        self._key = api_key or None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    async def _get(self, path: str, params: dict) -> dict:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        params = {"key": self._key, **params}
        try:
            async with self._session.get(f"{API}/{path}", params=params) as response:
                if response.status == 403:
                    raise SteamError("Steam rejected the API key (403).")
                if response.status != 200:
                    raise SteamError(f"Steam API returned HTTP {response.status}.")
                return await response.json()
        except aiohttp.ClientError as exc:
            raise SteamError(f"Could not reach the Steam API: {exc}") from exc

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def resolve_vanity(self, name: str) -> str:
        if not self.enabled:
            raise SteamError(
                "Custom profile URLs need a Steam Web API key. "
                "Set STEAM_API_KEY, or register with your steamID64."
            )
        data = await self._get(
            "ISteamUser/ResolveVanityURL/v1/", {"vanityurl": name}
        )
        payload = data.get("response", {})
        if payload.get("success") != 1 or not payload.get("steamid"):
            raise SteamError(f"No Steam profile found for `{name}`.")
        return payload["steamid"]

    async def lookup(self, value: str) -> SteamAccount:
        """Resolve a STEAM_X:Y:Z id into a SteamAccount.

        Deliberately the only accepted input format -- it is the one players
        can read straight out of the game console, and accepting a single
        shape means one obvious error message instead of five.
        """
        text = value.strip().strip("\"'")
        if not _STEAM2_RE.match(text):
            raise SteamError(
                f"`{text[:60]}` is not a Steam ID. It has to look like "
                "`STEAM_1:0:12345678`.\n"
                "Find yours by opening the console in CS:GO (`~`) while on a "
                "server and typing `status` -- yours is on the line with your name."
            )
        steam_id = parse_steam_id(text)
        if steam_id is None:  # pragma: no cover - the regex already guaranteed it
            raise SteamError("Could not read that Steam ID.")

        if not self.enabled:
            return SteamAccount(steam_id64=steam_id)

        account = SteamAccount(
            steam_id64=steam_id,
            profile_url=f"https://steamcommunity.com/profiles/{steam_id}",
        )
        summaries = await self._get(
            "ISteamUser/GetPlayerSummaries/v2/", {"steamids": steam_id}
        )
        players = summaries.get("response", {}).get("players", [])
        if not players:
            raise SteamError(f"Steam has no profile for `{steam_id}`.")
        profile = players[0]
        return SteamAccount(
            steam_id64=steam_id,
            name=profile.get("personaname"),
            profile_url=profile.get("profileurl") or account.profile_url,
        )
