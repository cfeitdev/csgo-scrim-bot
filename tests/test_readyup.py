"""The match must not ready up while players are still connecting."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scrimbot.get5 import Get5Manager  # noqa: E402
from scrimbot.server import ConnectedPlayer, parse_status  # noqa: E402
from scrimbot.steam import to_steam2  # noqa: E402

from test_get5 import FakeServer, finished_match, make_config, roster_ids  # noqa: E402

STATUS = """hostname: scrim
version : 1.38.7.6 secure
map     : de_inferno
players : 3 humans, 0 bots (12/12 max)

# userid name uniqueid connected ping loss state rate adr
#  2 1 "ezekiel" STEAM_1:1:11111111 05:23 45 0 active 786432 1.2.3.4:27005
#  3 2 "avvv" STEAM_1:0:22222222 00:04 0 0 spawning 128000 1.2.3.5:27005
#  4 3 "counterfeit" STEAM_1:1:33333333 00:01 0 0 connecting 80000 1.2.3.6:27005
"""

NO_STATE = (
    '#  2 1 "ezekiel" STEAM_1:1:11111111 05:23 45 0\n'
    '#  3 2 "avvv" STEAM_1:0:22222222 00:04 0 0\n'
)


class StatusStateTests(unittest.TestCase):
    def test_the_state_column_is_captured(self):
        players = parse_status(STATUS)
        self.assertEqual([p.state for p in players], ["active", "spawning", "connecting"])

    def test_names_and_ids_still_parse(self):
        players = parse_status(STATUS)
        self.assertEqual([p.name for p in players], ["ezekiel", "avvv", "counterfeit"])
        self.assertEqual(players[0].steam_id, "STEAM_1:1:11111111")

    def test_only_active_players_are_in_game(self):
        self.assertEqual([p.in_game for p in parse_status(STATUS)], [True, False, False])

    def test_a_layout_without_a_state_column_counts_everyone(self):
        players = parse_status(NO_STATE)
        self.assertEqual(len(players), 2)
        self.assertTrue(all(p.state is None for p in players))
        self.assertTrue(all(p.in_game for p in players))

    def test_a_state_word_in_a_player_name_is_not_read_as_the_state(self):
        line = '#  2 1 "connecting_guy" STEAM_1:1:11111111 05:23 45 0 active 786432\n'
        self.assertEqual(parse_status(line)[0].state, "active")


class StatefulServer(FakeServer):
    """A server whose players connect, then go active a poll later."""

    def __init__(self, steam_ids) -> None:
        super().__init__()
        self.ids = list(steam_ids)
        self.states: dict[str, str] = {}
        self.polls = 0

    async def connected_players(self):
        self.polls += 1
        return [
            ConnectedPlayer(i, f"p{i}", to_steam2(sid), self.states.get(sid))
            for i, sid in enumerate(self.ids, start=1)
            if sid in self.states
        ]

    def arrive(self, state="spawning"):
        for sid in self.ids:
            self.states[sid] = state

    def go_active(self):
        for sid in self.ids:
            self.states[sid] = "active"


class ReadyUpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.match = finished_match()
        self.ids = roster_ids(self.match)

    def manager(self, server, **over):
        base = dict(
            get5_base_url="http://bot", get5_poll_interval=0.001,
            get5_ready_timeout=0.6,
        )
        base.update(over)
        return Get5Manager(make_config(**base), server)

    async def test_a_connecting_roster_does_not_ready_up(self):
        server = StatefulServer(self.ids.values())
        server.arrive("connecting")
        manager = self.manager(server)
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        self.assertFalse(await manager.wait_for_roster(entry))
        self.assertGreater(server.polls, 1)

    async def test_a_spawning_roster_does_not_ready_up(self):
        server = StatefulServer(self.ids.values())
        server.arrive("spawning")
        manager = self.manager(server)
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        self.assertFalse(await manager.wait_for_roster(entry))

    async def test_an_active_roster_readies_up(self):
        server = StatefulServer(self.ids.values())
        server.go_active()
        manager = self.manager(server)
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        self.assertTrue(await manager.wait_for_roster(entry))

    async def test_players_who_finish_loading_ready_up(self):
        class Late(StatefulServer):
            async def connected_players(self):
                if self.polls >= 3:
                    self.go_active()
                return await super().connected_players()

        late = Late(self.ids.values())
        late.arrive("spawning")
        manager = self.manager(late, get5_ready_timeout=2.0)
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        self.assertTrue(await manager.wait_for_roster(entry))

    async def test_one_good_poll_is_not_enough(self):
        """A single clean read can land mid-reconnect; two in a row cannot."""

        class Flicker(StatefulServer):
            async def connected_players(self):
                # self.polls counts polls already served, so 0 is this one.
                # Complete, then someone drops, then complete again.
                if self.polls == 1:
                    self.states.pop(self.ids[0], None)
                else:
                    self.go_active()
                return await super().connected_players()

        server = Flicker(self.ids.values())
        manager = self.manager(server, get5_ready_timeout=2.0)
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        self.assertTrue(await manager.wait_for_roster(entry))
        # it cannot have returned on the first complete poll
        self.assertGreaterEqual(server.polls, 4)

    async def test_the_poll_count_is_configurable(self):
        server = StatefulServer(self.ids.values())
        server.go_active()
        manager = self.manager(server, get5_ready_polls=1)
        entry = manager.prepare(self.match, self.ids, matchid="abc")
        self.assertTrue(await manager.wait_for_roster(entry))
        self.assertEqual(server.polls, 1)


if __name__ == "__main__":
    unittest.main()
