from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .match import TEAM_EMOJI, TEAM_NAMES, Phase
from .regions import REGION_FLAGS, REGION_LABELS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .session import ScrimSession


class SessionView(discord.ui.View):
    """Base view bound to a session; forwards its own timeout to the session."""

    def __init__(self, session: "ScrimSession", *, timeout: float | None) -> None:
        super().__init__(timeout=timeout)
        self.session = session

    def refresh_items(self) -> None:
        """Re-sync components in place.

        The view object is reused for the whole phase so its auto-generated
        custom_ids stay stable -- rebuilding it would invalidate every button on
        any copy of the message a client has not re-rendered yet, and those
        clicks then go unanswered.
        """
        return None

    async def on_timeout(self) -> None:
        await self.session.handle_timeout(self)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        await self.session.handle_error(interaction, error)


# --------------------------------------------------------------------- lobby


class LobbyView(SessionView):
    def __init__(self, session: "ScrimSession") -> None:
        super().__init__(session, timeout=None)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, emoji="✅")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.session.handle_join(interaction)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, emoji="❌")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.session.handle_leave(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.session.handle_cancel(interaction)


# --------------------------------------------------------------------- draft


class PlayerSelect(discord.ui.Select):
    def __init__(self, session: "ScrimSession") -> None:
        super().__init__(
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="none", value="none")],
        )
        self.session = session
        self.sync()

    def sync(self) -> None:
        match = self.session.match
        captain = match.current_captain
        options = [
            discord.SelectOption(
                label=player.name[:100],
                value=str(player.id),
                description=f"Elo {player.elo}",
            )
            for player in match.available_players[:25]
        ]
        placeholder = f"{captain.name}: pick a player" if captain else "Draft complete"
        self.placeholder = placeholder[:150]
        self.options = options or [discord.SelectOption(label="none", value="none")]
        self.disabled = not options

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.session.handle_pick(interaction, int(self.values[0]))


class DraftView(SessionView):
    def __init__(self, session: "ScrimSession") -> None:
        super().__init__(session, timeout=session.pick_timeout)
        self.select = PlayerSelect(session)
        self.add_item(self.select)

    def refresh_items(self) -> None:
        self.select.sync()


# ------------------------------------------------------------------- map ban


class MapSelect(discord.ui.Select):
    def __init__(self, session: "ScrimSession") -> None:
        super().__init__(
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="none", value="none")],
        )
        self.session = session
        self.sync()

    def sync(self) -> None:
        match = self.session.match
        banner = match.current_banner
        options = [
            discord.SelectOption(
                label=game_map.display[:100],
                value=game_map.name,
                description=game_map.name,
            )
            for game_map in match.remaining_maps[:25]
        ]
        placeholder = f"{banner.name}: ban a map" if banner else "Veto complete"
        self.placeholder = placeholder[:150]
        self.options = options or [discord.SelectOption(label="none", value="none")]
        self.disabled = not options

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.session.handle_ban(interaction, self.values[0])


class MapBanView(SessionView):
    def __init__(self, session: "ScrimSession") -> None:
        super().__init__(session, timeout=session.pick_timeout)
        self.select = MapSelect(session)
        self.add_item(self.select)

    def refresh_items(self) -> None:
        self.select.sync()


# ---------------------------------------------------------------------- live


class WinnerButton(discord.ui.Button):
    def __init__(self, session: "ScrimSession", team: int) -> None:
        super().__init__(
            label=f"{TEAM_NAMES[team]} won",
            emoji=TEAM_EMOJI[team],
            style=discord.ButtonStyle.primary,
        )
        self.session = session
        self.team = team

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.session.handle_finish(interaction, self.team)


class LiveView(SessionView):
    """Shown while the game is on: report the result or re-fire the map change."""

    def __init__(self, session: "ScrimSession") -> None:
        super().__init__(session, timeout=None)
        self.add_item(WinnerButton(session, 0))
        self.add_item(WinnerButton(session, 1))

    @discord.ui.button(
        label="Connect info", style=discord.ButtonStyle.success, emoji="\U0001f511", row=0
    )
    async def connect(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.session.handle_connect_info(interaction)

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.secondary, row=1)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.session.handle_finish(interaction, None, tie=True)

    @discord.ui.button(
        label="Change map again", style=discord.ButtonStyle.secondary, emoji="\U0001f504", row=1
    )
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.session.handle_rcon_retry(interaction)


# ------------------------------------------------------------- region vote


class RegionButton(discord.ui.Button):
    def __init__(self, session: "ScrimSession", region: str) -> None:
        super().__init__(
            label=REGION_LABELS.get(region, region.upper()),
            emoji=REGION_FLAGS.get(region),
            style=discord.ButtonStyle.primary,
        )
        self.session = session
        self.region = region

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.session.handle_region_vote(interaction, self.region)


class RegionVoteView(discord.ui.View):
    """The server vote. A message of its own, with no timeout: it closes when
    everyone has voted or the veto ends, not on a clock."""

    def __init__(self, session: "ScrimSession") -> None:
        super().__init__(timeout=None)
        self.session = session
        for region in session.match.regions:
            self.add_item(RegionButton(session, region))

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        await self.session.handle_error(interaction, error)


def view_for(session: "ScrimSession") -> discord.ui.View | None:
    phase = session.match.phase
    if phase is Phase.LOBBY:
        return LobbyView(session)
    if phase is Phase.DRAFT:
        return DraftView(session)
    if phase is Phase.MAP_BAN:
        return MapBanView(session)
    if phase is Phase.LIVE:
        return LiveView(session)
    return None


__all__ = ["LobbyView", "DraftView", "MapBanView", "LiveView", "RegionVoteView", "view_for"]
