from __future__ import annotations

import discord

from .match import TEAM_EMOJI, TEAM_NAMES, Match, Phase
from .regions import region_name

COLOR_LOBBY = discord.Color.blurple()
COLOR_DRAFT = discord.Color.gold()
COLOR_BAN = discord.Color.orange()
COLOR_LIVE = discord.Color.green()
COLOR_DONE = discord.Color.dark_teal()
COLOR_ERROR = discord.Color.red()


def _player_table(match: Match) -> str:
    """The lobby roster as a monospaced block: slot, name, Elo."""
    width = min(max(max((len(p.name) for p in match.players), default=6), 6), 24)
    rows = [f"  # {'PLAYER'.ljust(width)}   ELO", f"  {'-' * (width + 8)}"]
    for slot in range(match.size):
        if slot < len(match.players):
            player = match.players[slot]
            rows.append(f"{slot + 1:>3} {player.name[:width].ljust(width)}  {player.elo:>4}")
        else:
            rows.append(f"{slot + 1:>3} {'(open)'.ljust(width)}     -")
    body = "\n".join(rows)
    return f"```\n{body}\n```"


def _roster_lines(match: Match, team: int) -> str:
    lines = []
    for index, player in enumerate(match.teams[team]):
        tag = " *(captain)*" if index == 0 else ""
        lines.append(f"`{player.elo:>4}` {player.mention}{tag}")
    while len(lines) < match.team_size:
        lines.append("`----` *empty*")
    return "\n".join(lines)


def _team_fields(embed: discord.Embed, match: Match, *, show_elo: bool = True) -> None:
    for team in (0, 1):
        suffix = f" (avg {match.team_elo(team)})" if show_elo else ""
        embed.add_field(
            name=f"{TEAM_EMOJI[team]} {TEAM_NAMES[team]}{suffix}",
            value=_roster_lines(match, team),
            inline=True,
        )


def lobby_embed(match: Match) -> discord.Embed:
    embed = discord.Embed(
        title=f"{match.mode} Scrim -- Queue",
        description=(
            f"Press **Join** to enter the queue. Teams are formed automatically "
            f"at **{match.size}** players."
        ),
        color=COLOR_LOBBY,
    )
    embed.add_field(name="Queue", value=f"**{len(match.players)} / {match.size}**", inline=True)
    embed.add_field(name="Pick mode", value=f"`{match.pick_mode}`", inline=True)
    embed.add_field(name="Players", value=_player_table(match), inline=False)
    if match.players:
        average = round(sum(p.elo for p in match.players) / len(match.players))
        embed.set_footer(text=f"Average Elo: {average}")

    forced = [
        f"{TEAM_EMOJI[team]} <@{uid}>"
        for team, uid in enumerate(match.forced_captains)
        if uid is not None
    ]
    if forced:
        embed.add_field(name="Forced captains", value=" ".join(forced), inline=False)
    return embed


def draft_embed(match: Match) -> discord.Embed:
    embed = discord.Embed(title=f"{match.mode} Scrim -- Draft", color=COLOR_DRAFT)

    captain = match.current_captain
    if captain is not None:
        remaining = match.picks_left_for_current_captain()
        plural = "picks" if remaining > 1 else "pick"
        embed.description = (
            f"{TEAM_EMOJI[match.current_draft_team]} **{captain.name}** is on the clock "
            f"-- **{remaining}** {plural} in a row."
        )
    else:
        embed.description = "Draft complete."

    _team_fields(embed, match)

    available = match.available_players
    embed.add_field(
        name=f"Available ({len(available)})",
        value=", ".join(p.mention for p in available) if available else "*none*",
        inline=False,
    )
    # Only what is still to come: picks already made are struck from the list,
    # so the leftmost square is always whoever is on the clock.
    upcoming = [
        (f"**{TEAM_EMOJI[t]}**" if i == match.draft_index else TEAM_EMOJI[t])
        for i, t in enumerate(match.draft_order)
        if i >= match.draft_index
    ]
    embed.add_field(name="Order", value=" ".join(upcoming) or "*complete*", inline=False)
    embed.set_footer(text="Snake pick order: 1, then 2 at a time.")
    return embed


def ban_embed(match: Match) -> discord.Embed:
    embed = discord.Embed(title=f"{match.mode} Scrim -- Map Veto", color=COLOR_BAN)

    banner = match.current_banner
    if banner is not None:
        embed.description = (
            f"{TEAM_EMOJI[match.current_ban_team]} **{banner.name}** bans "
            f"({len(match.remaining_maps)} maps left)."
        )
    else:
        embed.description = "Veto complete."

    _team_fields(embed, match, show_elo=False)
    embed.add_field(
        name=f"Remaining ({len(match.remaining_maps)})",
        value="\n".join(f"`{m.name}`" for m in match.remaining_maps) or "*none*",
        inline=False,
    )
    if match.banned:
        embed.add_field(
            name="Banned",
            value=" ".join(f"{TEAM_EMOJI[t]} ~~`{m.name}`~~" for t, m in match.banned),
            inline=False,
        )
    return embed


def live_embed(
    match: Match,
    rcon_status: str | None = None,
    connect: str | None = None,
    get5: str | None = None,
    region: str | None = None,
) -> discord.Embed:
    game_map = match.selected_map
    description = (
        f"Map: **{game_map.display}**  (`{game_map.name}`)" if game_map else "Ready"
    )
    if region:
        description += f"\nServer: **{region}**"
    embed = discord.Embed(
        title=f"{match.mode} Scrim -- Live",
        description=description,
        color=COLOR_LIVE,
    )
    _team_fields(embed, match)
    if connect:
        embed.add_field(
            name="Connect",
            value=(
                "Press **Connect info** below for the server address"
                + (" and password" if match.password else "")
                + " — only players in this match can see it."
            ),
            inline=False,
        )
    if rcon_status:
        embed.add_field(name="Server", value=rcon_status, inline=False)
    if get5:
        embed.add_field(name="get5", value=get5, inline=False)
    embed.set_footer(text="Report the result with /game end when you are done.")
    return embed


def scoreboard(roster: list[dict]) -> str:
    """A monospaced K/A/D/ADR/HS table for one team."""
    if not roster:
        return ""
    # An all-zero team means get5 recorded nothing for this map. Printing a
    # table of zeroes reads as a result; printing nothing reads as no data.
    if not any(
        player["kills"] or player["deaths"] or player["assists"] or player["damage"]
        for player in roster
    ):
        return ""
    # Never narrower than the header, or the columns shift by a character.
    width = min(max(max((len(p["name"]) for p in roster), default=6), 6), 16)
    rows = [f"{'PLAYER'.ljust(width)}   K   A   D    ADR  HS%"]
    for player in roster:
        rounds = player["rounds"] or 1
        adr = player["damage"] / rounds
        hs = (player["headshots"] / player["kills"] * 100) if player["kills"] else 0
        rows.append(
            f"{player['name'][:width].ljust(width)} "
            f"{player['kills']:>3} {player['assists']:>3} {player['deaths']:>3} "
            f"{adr:>6.1f} {hs:>3.0f}%"
        )
    body = "\n".join(rows)
    return f"```\n{body}\n```"


LEADERBOARD_PAGE_SIZE = 10


def leaderboard_pages(rows: list, per_page: int = LEADERBOARD_PAGE_SIZE) -> int:
    return max(1, -(-len(rows) // per_page))


def leaderboard_embed(
    mode: str, rows: list, page: int = 1, per_page: int = LEADERBOARD_PAGE_SIZE
) -> discord.Embed:
    """One page of the leaderboard. ``rows`` is the full ranked list."""
    pages = leaderboard_pages(rows, per_page)
    page = max(1, min(page, pages))
    window = rows[(page - 1) * per_page : page * per_page]

    lines = []
    for offset, (user_id, stats) in enumerate(window, start=(page - 1) * per_page + 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(offset, f"`{offset:>2}.`")
        wins, losses = int(stats.get("wins", 0)), int(stats.get("losses", 0))
        lines.append(
            f"{medal} <@{user_id}> -- **{stats.get('elo', 0)}** ({wins}W / {losses}L)"
        )

    embed = discord.Embed(
        title=f"{mode} Leaderboard",
        description="\n".join(lines) or "*empty*",
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Page {page}/{pages} -- {len(rows)} ranked players")
    return embed


def result_embed(
    match: Match,
    changes: dict[int, tuple[int, int]] | None = None,
    players: list[list[dict]] | None = None,
    score: tuple[int, int] | None = None,
    result_url: str | None = None,
) -> discord.Embed:
    """The final card. With ``result_url`` the full copy lives elsewhere, so
    this one keeps the outcome and Elo and points at it instead of repeating
    the scoreboard."""
    changes = changes or {}
    if match.tie:
        title = f"{match.mode} Scrim -- Draw"
    elif match.winner is not None:
        title = f"{TEAM_EMOJI[match.winner]} {TEAM_NAMES[match.winner]} wins"
    else:
        title = f"{match.mode} Scrim -- Finished"

    headline = []
    if score is not None and score != (0, 0):
        headline.append(f"**{TEAM_NAMES[0]} {score[0]} : {score[1]} {TEAM_NAMES[1]}**")
    if match.selected_map:
        headline.append(f"Map: `{match.selected_map.name}`")

    embed = discord.Embed(
        title=title,
        description="  •  ".join(headline) or None,
        color=COLOR_DONE,
    )
    order = (0, 1) if match.winner is None else (match.winner, 1 - match.winner)
    for team in order:
        lines = []
        for player in match.teams[team]:
            before, after = changes.get(player.id, (player.elo, player.elo))
            lines.append(f"{player.mention} `{before} -> {after}` ({after - before:+d})")
        embed.add_field(
            name=f"{TEAM_EMOJI[team]} {TEAM_NAMES[team]}",
            value="\n".join(lines) or "*empty*",
            inline=False,
        )
        if result_url:
            continue
        table = scoreboard(players[team]) if players and team < len(players) else ""
        if table:
            embed.add_field(name="​", value=table, inline=False)
    if result_url:
        embed.add_field(
            name="​",
            value=f"[Full scoreboard]({result_url})",
            inline=False,
        )
    return embed


COLOR_VOTE = discord.Color.blurple()


def region_vote_embed(match: Match) -> discord.Embed:
    """The server vote: running tally while open, the outcome once locked."""
    tally = match.region_tally()
    counts = "   ".join(f"{region_name(r)}  **{tally[r]}**" for r in match.regions)
    voted = len(match.region_votes)

    if match.vote_open and not match.finished:
        embed = discord.Embed(
            title="🌍 Server vote",
            description=(
                "Players in this game: where should it be played?\n"
                f"Closes once all **{match.size}** have voted, or when the map veto "
                "ends. You can change your vote until then. A tie is a coin flip."
            ),
            color=COLOR_VOTE,
        )
        embed.add_field(name="Votes", value=counts, inline=False)
        embed.set_footer(text=f"{voted}/{match.size} voted")
        return embed

    if match.region is None:
        # The game ended before the vote did.
        return discord.Embed(
            title="🌍 Server vote -- closed",
            description="The game ended before the vote finished.",
            color=COLOR_DONE,
        )

    how = {
        "vote": "won the vote",
        "coin": "won the coin flip" if voted else "nobody voted -- coin flip",
        "admin": "set by an administrator",
        "default": "no vote",
    }.get(match.region_decided_by or "", "")
    embed = discord.Embed(
        title=f"🌍 Server: {region_name(match.region)}",
        description=f"Locked in ({how}).",
        color=COLOR_DONE,
    )
    embed.add_field(name="Votes", value=counts, inline=False)
    embed.set_footer(text=f"{voted}/{match.size} voted")
    return embed


def cancelled_embed(match: Match, reason: str = "This scrim was cancelled.") -> discord.Embed:
    return discord.Embed(
        title=f"{match.mode} Scrim -- Cancelled", description=reason, color=COLOR_ERROR
    )


def render(
    match: Match,
    rcon_status: str | None = None,
    *,
    connect: str | None = None,
    changes: dict[int, tuple[int, int]] | None = None,
    get5: str | None = None,
    players: list[list[dict]] | None = None,
    score: tuple[int, int] | None = None,
    result_url: str | None = None,
    region: str | None = None,
) -> discord.Embed:
    """Pick the right embed for whatever phase the match is in."""
    if match.phase is Phase.LOBBY:
        return lobby_embed(match)
    if match.phase is Phase.DRAFT:
        return draft_embed(match)
    if match.phase is Phase.MAP_BAN:
        return ban_embed(match)
    if match.phase is Phase.LIVE:
        return live_embed(match, rcon_status, connect, get5, region)
    if match.phase is Phase.COMPLETE:
        return result_embed(match, changes, players, score, result_url)
    return cancelled_embed(match)
