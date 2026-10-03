"""The paging control under /leaderboard."""

from __future__ import annotations

import logging

import discord

from . import embeds

log = logging.getLogger(__name__)


class LeaderboardView(discord.ui.View):
    """Previous/next paging over a snapshot of the ranked list.

    The rows are captured when the command runs, so paging never reshuffles
    under someone mid-read. The view object is reused for every page -- its
    components carry auto-generated custom_ids, and rebuilding it would
    invalidate the buttons on any copy of the message a client is still
    showing.
    """

    def __init__(
        self,
        mode: str,
        rows: list,
        page: int = 1,
        per_page: int = embeds.LEADERBOARD_PAGE_SIZE,
        timeout: float | None = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.mode = mode
        self.rows = rows
        self.per_page = per_page
        self.pages = embeds.leaderboard_pages(rows, per_page)
        self.page = max(1, min(page, self.pages))
        self.message: discord.Message | None = None
        self._sync()

    @property
    def embed(self) -> discord.Embed:
        return embeds.leaderboard_embed(self.mode, self.rows, self.page, self.per_page)

    def _sync(self) -> None:
        self.previous.disabled = self.page <= 1
        self.next.disabled = self.page >= self.pages

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        self.page = max(1, min(self.page + delta, self.pages))
        self._sync()
        await interaction.response.edit_message(embed=self.embed, view=self)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary)
    async def previous(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._turn(interaction, -1)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary)
    async def next(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._turn(interaction, 1)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            log.debug("Could not disable the leaderboard buttons on timeout")

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("Leaderboard paging failed", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Something went wrong turning the page.", ephemeral=True
            )
