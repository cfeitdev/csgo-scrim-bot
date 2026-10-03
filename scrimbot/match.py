from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum

from .elo import balance_teams
from .maps import MAP_POOL, GameMap

TEAM_NAMES = ("Team A", "Team B")
TEAM_EMOJI = ("\U0001f7e6", "\U0001f7e5")  # blue / red squares


class ScrimError(Exception):
    """User-facing rule violation (wrong turn, already joined, ...)."""


class Phase(str, Enum):
    LOBBY = "lobby"
    DRAFT = "draft"
    MAP_BAN = "map_ban"
    LIVE = "live"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


# Fake players from /testfill get ids in this range. Real Discord snowflakes are
# 17-19 digits, so these can never collide with a genuine user.
TEST_ID_BASE = 1_000

@dataclass(frozen=True)
class Player:
    id: int
    name: str
    elo: int
    is_bot: bool = False

    @property
    def mention(self) -> str:
        # A fake id would render as a broken mention, so show the name instead.
        return f"`{self.name}`" if self.is_bot else f"<@{self.id}>"


def build_draft_order(picks: int) -> list[int]:
    """Snake order: a single opening pick, then pairs, alternating.

    With 10 players (2 captains + 8 picks) that is A, B, B, A, A, B, B, A --
    four picks each. The opening single is what compensates team B for team A
    picking first; every block after it is a pair, so the sides stay level all
    the way down and the last pick swings back to A.
    """
    order: list[int] = []
    team, block = 0, 1
    while len(order) < picks:
        order.extend([team] * min(block, picks - len(order)))
        team ^= 1
        block = 2
    return order


def build_ban_order(bans: int) -> list[int]:
    """Team B bans first, as compensation for the first draft pick going to A."""
    return [1 - (i % 2) for i in range(bans)]


@dataclass
class Match:
    host_id: int
    size: int = 10
    mode: str = "5v5"
    map_pool: tuple[GameMap, ...] = MAP_POOL
    pick_mode: str = "captains"
    random_captains: bool = True
    vetoing: bool = True

    phase: Phase = Phase.LOBBY
    players: list[Player] = field(default_factory=list)
    captains: list[Player] = field(default_factory=list)
    forced_captains: list[int | None] = field(default_factory=lambda: [None, None])
    # Players who must not be picked captain (/blackcapt). Only overridden when
    # there are not enough other players to fill both captaincies.
    captain_blacklist: set[int] = field(default_factory=set)
    teams: list[list[Player]] = field(default_factory=lambda: [[], []])
    draft_order: list[int] = field(default_factory=list)
    draft_index: int = 0
    remaining_maps: list[GameMap] = field(default_factory=list)
    ban_order: list[int] = field(default_factory=list)
    ban_index: int = 0
    banned: list[tuple[int, GameMap]] = field(default_factory=list)
    selected_map: GameMap | None = None
    password: str | None = None
    winner: int | None = None
    tie: bool = False

    # Server-region vote. With fewer than two regions there is no vote and
    # ``region`` stays None, which means the default (home) server.
    regions: tuple[str, ...] = ()
    default_region: str = "eu"
    region_votes: dict[int, str] = field(default_factory=dict)
    vote_open: bool = False
    region: str | None = None
    # How the region was settled: "vote", "coin", "admin" or "default".
    region_decided_by: str | None = None

    # ------------------------------------------------------------------ lobby

    @property
    def team_size(self) -> int:
        return self.size // 2

    @property
    def is_full(self) -> bool:
        return len(self.players) >= self.size

    @property
    def teams_locked(self) -> bool:
        return self.phase in (Phase.MAP_BAN, Phase.LIVE, Phase.COMPLETE)

    @property
    def finished(self) -> bool:
        return self.phase in (Phase.COMPLETE, Phase.CANCELLED)

    @property
    def has_bots(self) -> bool:
        """True once /testfill has padded the lobby -- this is a dry run."""
        return any(p.is_bot for p in self.players)

    @property
    def current_actor(self) -> "Player | None":
        """Whoever the bot is waiting on: the drafting captain or the banner."""
        return self.current_captain or self.current_banner

    def has_player(self, user_id: int) -> bool:
        return any(p.id == user_id for p in self.players)

    def get_player(self, user_id: int) -> Player | None:
        return next((p for p in self.players if p.id == user_id), None)

    def team_of(self, user_id: int) -> int | None:
        for index, roster in enumerate(self.teams):
            if any(p.id == user_id for p in roster):
                return index
        return None

    def add_player(self, player: Player) -> None:
        if self.phase is not Phase.LOBBY:
            raise ScrimError(
                "The lobby is closed -- teams are already being picked. Use `/sub` instead."
            )
        if self.has_player(player.id):
            raise ScrimError(f"{player.name} is already in this lobby.")
        if self.is_full:
            raise ScrimError("This lobby is full.")
        self.players.append(player)

    def remove_player(self, user_id: int, *, force: bool = False) -> Player:
        player = self.get_player(user_id)
        if player is None:
            raise ScrimError("That player is not in this lobby.")
        if self.phase is not Phase.LOBBY and not force:
            raise ScrimError(
                "You cannot leave once the draft has started -- ask staff for a `/sub`."
            )
        self.players.remove(player)
        self.region_votes.pop(user_id, None)
        for roster in self.teams:
            if player in roster:
                roster.remove(player)
        if player in self.captains:
            self.captains = [c for c in self.captains if c.id != player.id]
        self.forced_captains = [None if c == user_id else c for c in self.forced_captains]
        return player

    def substitute(self, out_id: int, incoming: Player) -> tuple[Player, int | None]:
        """Swap a player out for a new one, keeping their team and captaincy."""
        outgoing = self.get_player(out_id)
        if outgoing is None:
            raise ScrimError("That player is not in this game.")
        if self.has_player(incoming.id):
            raise ScrimError(f"{incoming.name} is already in this game.")
        if self.phase in (Phase.COMPLETE, Phase.CANCELLED):
            raise ScrimError("This game is already over.")

        self.players[self.players.index(outgoing)] = incoming
        # The vote belongs to the person, not the slot: the sub casts their own.
        self.region_votes.pop(out_id, None)
        team = None
        for index, roster in enumerate(self.teams):
            if outgoing in roster:
                roster[roster.index(outgoing)] = incoming
                team = index
        if outgoing in self.captains:
            self.captains[self.captains.index(outgoing)] = incoming
        return outgoing, team

    # ----------------------------------------------------------- team forming

    def set_forced_captain(self, user_id: int, team: int) -> Player:
        player = self.get_player(user_id)
        if player is None:
            raise ScrimError("That player is not in this lobby.")
        other = self.forced_captains[1 - team]
        if user_id in self.captain_blacklist:
            raise ScrimError(
                f"{player.name} is captain-blacklisted. Lift it with `/blackcapt` first."
            )
        if other == user_id:
            self.forced_captains[1 - team] = None
        self.forced_captains[team] = user_id
        if self.phase in (Phase.DRAFT, Phase.MAP_BAN) and all(self.teams):
            # Teams already exist, so install them now rather than rerolling --
            # a re-draft would throw away every pick made so far.
            self._install_captain(player, team)
        return player

    def _install_captain(self, player: Player, team: int) -> None:
        """Make ``player`` captain of ``team``, leaving the draft intact.

        Whoever they displace takes their place: their old team slot if they
        were already drafted, otherwise back into the available pool.
        """
        roster = self.teams[team]
        current = roster[0] if roster else None
        if current is player:
            return

        origin = None
        for index, side in enumerate(self.teams):
            if player in side:
                origin = (index, side.index(player))
                break

        if origin is not None and origin[0] == team:
            # Already on this side -- a straight swap keeps both players.
            roster[0], roster[origin[1]] = roster[origin[1]], roster[0]
        else:
            if origin is not None:
                self.teams[origin[0]].pop(origin[1])
            if current is not None:
                roster.pop(0)
            roster.insert(0, player)
            if current is not None and origin is not None:
                self.teams[origin[0]].insert(origin[1], current)

        self.captains = [self.teams[0][0], self.teams[1][0]]

    def _choose_captains(
        self, rng: random.Random, avoid: set[int] | None = None
    ) -> list[Player]:
        """Forced captains win; the rest are random or the highest rated.

        ``avoid`` holds the previous captains on a re-draft so the reroll
        actually produces new ones where there are enough players to do so.
        """
        chosen: list[Player | None] = [None, None]
        for team, forced_id in enumerate(self.forced_captains):
            if forced_id is not None:
                chosen[team] = self.get_player(forced_id)

        pool = [p for p in self.players if p not in chosen]
        if not self.random_captains:
            pool.sort(key=lambda p: p.elo, reverse=True)
        else:
            rng.shuffle(pool)
            if avoid:
                # Stable sort, so the shuffle still decides the order within
                # each group -- it just pushes last game's captains to the back.
                pool.sort(key=lambda p: p.id in avoid)
        if self.captain_blacklist:
            # Stable again: blacklisted players go last, so they only captain
            # when nobody else is left (e.g. a 2-player lobby).
            pool.sort(key=lambda p: p.id in self.captain_blacklist)
        for team in (0, 1):
            if chosen[team] is None:
                chosen[team] = pool.pop(0)
        return [chosen[0], chosen[1]]

    def _captain_first(self, roster) -> list[Player]:
        """Put the first player who may captain at the front of a roster."""
        roster = list(roster)
        for index, player in enumerate(roster):
            if player.id not in self.captain_blacklist:
                roster.insert(0, roster.pop(index))
                break
        return roster

    def start_teams(
        self, rng: random.Random | None = None, avoid_captains: set[int] | None = None
    ) -> Phase:
        """Lobby is full -- form teams according to the configured pick mode."""
        if self.phase is not Phase.LOBBY:
            raise ScrimError("Teams have already been formed.")
        if not self.is_full:
            raise ScrimError(f"Need {self.size} players to start (currently {len(self.players)}).")
        rng = rng or random

        if self.pick_mode == "captains":
            self.start_draft(rng, avoid_captains)
            self._open_vote()
            return self.phase

        self.captains = self._choose_captains(rng, avoid_captains)
        if self.pick_mode == "random":
            shuffled = list(self.players)
            rng.shuffle(shuffled)
            first = [self.captains[0]]
            second = [self.captains[1]]
            for player in shuffled:
                if player in self.captains:
                    continue
                (first if len(first) <= len(second) else second).append(player)
            self.teams = [first, second]
        elif self.pick_mode == "balanced":
            self.teams = [self._captain_first(t) for t in balance_teams(self.players)]
            self.captains = [self.teams[0][0], self.teams[1][0]]
        else:
            raise ScrimError(f"Unknown pick mode: {self.pick_mode}")

        self._start_map_ban()
        self._open_vote()
        return self.phase

    def start_draft(
        self, rng: random.Random | None = None, avoid_captains: set[int] | None = None
    ) -> None:
        if self.phase not in (Phase.LOBBY, Phase.DRAFT):
            raise ScrimError("The teams are already locked in.")
        if not self.is_full:
            raise ScrimError(f"Need {self.size} players to start (currently {len(self.players)}).")

        rng = rng or random
        self.captains = self._choose_captains(rng, avoid_captains)
        self.teams = [[self.captains[0]], [self.captains[1]]]
        self.draft_order = build_draft_order(self.size - 2)
        self.draft_index = 0
        self.phase = Phase.DRAFT

    def redraft(self, rng: random.Random | None = None) -> None:
        """Tear the teams down and run the draft again with the same players."""
        if self.phase in (Phase.COMPLETE, Phase.CANCELLED):
            raise ScrimError("This game is already over.")
        previous = {c.id for c in self.captains}
        # A re-draft is a fresh roll: forced picks do not carry over.
        self.forced_captains = [None, None]
        self.teams = [[], []]
        self.captains = []
        self.banned = []
        self.remaining_maps = []
        self.selected_map = None
        self.draft_index = 0
        self.ban_index = 0
        self.phase = Phase.LOBBY
        self.start_teams(rng, avoid_captains=previous)

    # ------------------------------------------------------------------ draft

    @property
    def available_players(self) -> list[Player]:
        drafted = {p.id for team in self.teams for p in team}
        return [p for p in self.players if p.id not in drafted]

    @property
    def current_draft_team(self) -> int | None:
        if self.phase is not Phase.DRAFT or self.draft_index >= len(self.draft_order):
            return None
        return self.draft_order[self.draft_index]

    @property
    def current_captain(self) -> Player | None:
        team = self.current_draft_team
        return None if team is None else self.captains[team]

    def picks_left_for_current_captain(self) -> int:
        """How many picks in a row the captain on the clock still has."""
        team = self.current_draft_team
        if team is None:
            return 0
        count = 0
        for slot in self.draft_order[self.draft_index :]:
            if slot != team:
                break
            count += 1
        return count

    def pick(self, captain_id: int, player_id: int) -> tuple[int, Player]:
        if self.phase is not Phase.DRAFT:
            raise ScrimError("There is no draft in progress.")
        team = self.current_draft_team
        if team is None:
            raise ScrimError("The draft is already finished.")
        if self.captains[team].id != captain_id:
            raise ScrimError(f"It is not your pick -- {self.captains[team].name} is on the clock.")
        player = next((p for p in self.available_players if p.id == player_id), None)
        if player is None:
            raise ScrimError("That player is not available.")

        self.teams[team].append(player)
        self.draft_index += 1
        self._autofill_forced_picks()
        if self.draft_index >= len(self.draft_order):
            self._start_map_ban()
        return team, player

    def _autofill_forced_picks(self) -> None:
        """Assign the last player automatically -- there is nothing to choose.

        Picks and available players run down together, so one left always
        means one pick left.
        """
        while self.draft_index < len(self.draft_order):
            remaining = self.available_players
            if len(remaining) != 1:
                return
            self.teams[self.draft_order[self.draft_index]].append(remaining[0])
            self.draft_index += 1

    def auto_pick(self, rng: random.Random | None = None) -> tuple[int, Player] | None:
        """Used when a captain runs out the pick clock."""
        captain = self.current_captain
        if captain is None:
            return None
        rng = rng or random
        return self.pick(captain.id, rng.choice(self.available_players).id)

    # ---------------------------------------------------------------- map ban

    def _start_map_ban(self, rng: random.Random | None = None) -> None:
        self.remaining_maps = list(self.map_pool)
        self.banned = []
        self.ban_index = 0
        if not self.vetoing or len(self.remaining_maps) < 2:
            rng = rng or random
            self.selected_map = rng.choice(self.remaining_maps)
            self.ban_order = []
            self.phase = Phase.LIVE
            self._settle_region(rng)
            return
        self.ban_order = build_ban_order(len(self.remaining_maps) - 1)
        self.phase = Phase.MAP_BAN

    @property
    def current_ban_team(self) -> int | None:
        if self.phase is not Phase.MAP_BAN or self.ban_index >= len(self.ban_order):
            return None
        return self.ban_order[self.ban_index]

    @property
    def current_banner(self) -> Player | None:
        team = self.current_ban_team
        return None if team is None else self.captains[team]

    def ban(self, captain_id: int, map_name: str) -> tuple[int, GameMap]:
        if self.phase is not Phase.MAP_BAN:
            raise ScrimError("There is no map veto in progress.")
        team = self.current_ban_team
        if team is None:
            raise ScrimError("The map veto is already finished.")
        if self.captains[team].id != captain_id:
            raise ScrimError(f"It is not your ban -- {self.captains[team].name} is on the clock.")
        game_map = next((m for m in self.remaining_maps if m.name == map_name), None)
        if game_map is None:
            raise ScrimError("That map has already been banned.")

        self.remaining_maps.remove(game_map)
        self.banned.append((team, game_map))
        self.ban_index += 1
        if len(self.remaining_maps) == 1:
            self.selected_map = self.remaining_maps[0]
            self.phase = Phase.LIVE
            self._settle_region()
        return team, game_map

    def auto_ban(self, rng: random.Random | None = None) -> tuple[int, GameMap] | None:
        captain = self.current_banner
        if captain is None:
            return None
        rng = rng or random
        return self.ban(captain.id, rng.choice(self.remaining_maps).name)

    # ---------------------------------------------------------- region vote

    @property
    def has_region_vote(self) -> bool:
        return len(self.regions) >= 2

    def _open_vote(self) -> None:
        """Open the vote alongside the draft/veto, unless it is already settled.

        If the teams went straight to a live map (no draft and no veto) there
        is no time to vote, and the region has already been defaulted.
        """
        if self.has_region_vote and self.region is None and self.phase in (
            Phase.DRAFT,
            Phase.MAP_BAN,
        ):
            self.vote_open = True

    def region_tally(self) -> dict[str, int]:
        tally = {region: 0 for region in self.regions}
        for choice in self.region_votes.values():
            if choice in tally:
                tally[choice] += 1
        return tally

    def vote_region(self, user_id: int, region: str) -> bool:
        """Cast or change a vote. True if this vote closed the poll.

        The poll closes early once every slot in the lobby has voted.
        """
        if not self.vote_open:
            raise ScrimError("The server vote is closed.")
        if not self.has_player(user_id):
            raise ScrimError("Only players in this game can vote.")
        if region not in self.regions:
            raise ScrimError(f"Unknown region: {region}")
        self.region_votes[user_id] = region
        if len(self.region_votes) >= self.size:
            self._settle_region()
            return True
        return False

    def override_region(self, region: str) -> None:
        """An administrator settles it. Only while the vote is still open."""
        if region not in self.regions:
            raise ScrimError(f"Unknown region: {region}")
        if not self.vote_open:
            if self.region is not None:
                raise ScrimError(
                    "The server region is already locked -- it can only be "
                    "overridden while the vote is open."
                )
            raise ScrimError("There is no server vote running.")
        self.vote_open = False
        self.region = region
        self.region_decided_by = "admin"

    def _settle_region(self, rng: random.Random | None = None) -> None:
        """Lock the region in: the vote result, a coin flip, or the default."""
        if not self.has_region_vote or self.region is not None:
            return
        if not self.vote_open:
            # The vote never opened -- no draft and no veto to vote during.
            self.region = self.default_region
            self.region_decided_by = "default"
            return
        self.vote_open = False
        tally = self.region_tally()
        best = max(tally.values())
        leaders = [region for region, count in tally.items() if count == best]
        if len(leaders) == 1:
            self.region = leaders[0]
            self.region_decided_by = "vote"
        else:  # a tie, including nobody voting at all
            self.region = (rng or random).choice(leaders)
            self.region_decided_by = "coin"

    # ------------------------------------------------------------------- end

    def finish(self, winner: int | None, *, tie: bool = False) -> None:
        if self.phase in (Phase.COMPLETE, Phase.CANCELLED):
            raise ScrimError("This game has already been closed out.")
        if not self.teams_locked:
            raise ScrimError("Teams have not been picked yet -- use `/cancel` instead.")
        self.winner = winner
        self.tie = tie
        self.phase = Phase.COMPLETE

    def cancel(self) -> None:
        self.phase = Phase.CANCELLED

    # ----------------------------------------------------------------- misc

    def team_elo(self, team: int) -> int:
        roster = self.teams[team]
        return round(sum(p.elo for p in roster) / len(roster)) if roster else 0

    @property
    def active_captain(self) -> Player | None:
        return self.current_captain or self.current_banner
