"""A server still loading a map is not a bad RCON password."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrimbot.rcon import RconAuthError, RconError, RconNotReady  # noqa: E402
from scrimbot.server import ServerController  # noqa: E402


class FlakyRcon:
    """Refuses RCON until the map has 'loaded'."""

    def __init__(self, failures: int, error=None) -> None:
        self.failures = failures
        self.error = error or RconNotReady("still loading")
        self.calls = 0

    async def execute(self, command: str, *, expect_response: bool = True) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return "scrimbot_ping"

    async def close(self) -> None:
        return None


class ErrorTaxonomyTests(unittest.TestCase):
    def test_not_ready_is_not_an_auth_failure(self):
        self.assertTrue(issubclass(RconNotReady, RconError))
        self.assertFalse(issubclass(RconNotReady, RconAuthError))

    def test_a_bad_password_is_still_an_auth_failure(self):
        self.assertTrue(issubclass(RconAuthError, RconError))


class WaitUntilReadyTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, rcon):
        return ServerController(rcon)

    async def test_an_answering_server_returns_at_once(self):
        rcon = FlakyRcon(0)
        self.assertTrue(await self.controller(rcon).wait_until_ready(interval=0.001))
        self.assertEqual(rcon.calls, 1)

    async def test_it_waits_out_a_map_load(self):
        rcon = FlakyRcon(4)
        ready = await self.controller(rcon).wait_until_ready(timeout=5.0, interval=0.001)
        self.assertTrue(ready)
        self.assertEqual(rcon.calls, 5)

    async def test_it_gives_up_at_the_timeout(self):
        rcon = FlakyRcon(10_000)
        ready = await self.controller(rcon).wait_until_ready(timeout=0.05, interval=0.001)
        self.assertFalse(ready)

    async def test_a_bad_password_is_not_retried(self):
        rcon = FlakyRcon(10_000, RconAuthError("wrong password"))
        with self.assertRaises(RconAuthError):
            await self.controller(rcon).wait_until_ready(timeout=5.0, interval=0.001)
        self.assertEqual(rcon.calls, 1)

    async def test_an_unreachable_server_is_retried(self):
        rcon = FlakyRcon(2, RconError("Could not reach host"))
        self.assertTrue(
            await self.controller(rcon).wait_until_ready(timeout=5.0, interval=0.001)
        )

    async def test_progress_is_reported_while_waiting(self):
        rcon = FlakyRcon(6)
        seen = []

        async def on_wait(attempt):
            seen.append(attempt)

        await self.controller(rcon).wait_until_ready(
            timeout=5.0, interval=0.001, on_wait=on_wait
        )
        self.assertEqual(seen, [1, 2, 3, 4, 5, 6])

    async def test_it_does_not_spin_without_sleeping(self):
        rcon = FlakyRcon(3)
        started = asyncio.get_running_loop().time()
        await self.controller(rcon).wait_until_ready(timeout=5.0, interval=0.02)
        elapsed = asyncio.get_running_loop().time() - started
        self.assertGreaterEqual(elapsed, 0.05)


if __name__ == "__main__":
    unittest.main()
