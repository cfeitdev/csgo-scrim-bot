from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GameMap:
    """One entry in the scrim map pool.

    ``workshop_id`` is for maps that are not shipped with the server (classic
    remakes, community maps). Fill it in and the bot will use
    ``host_workshop_map`` instead of ``changelevel``.
    """

    name: str
    display: str
    workshop_id: str | None = None

    def changelevel_command(self) -> str:
        if self.workshop_id:
            return f"host_workshop_map {self.workshop_id}"
        return f"changelevel {self.name}"


# Order here is the order shown in the ban UI.
MAP_POOL: tuple[GameMap, ...] = (
    GameMap("de_dust2_old", "Dust II (Classic)"),
    GameMap("de_cache", "Cache"),
    GameMap("de_mirage", "Mirage"),
    GameMap("de_train", "Train"),
    GameMap("cs_italy", "Italy"),
    GameMap("cs_assault", "Assault"),
    GameMap("de_overpass", "Overpass"),
    GameMap("cs_agency", "Agency"),
    GameMap("de_vertigo", "Vertigo"),
    GameMap("cs_militia", "Militia"),
    GameMap("de_aztec", "Aztec"),
    GameMap("de_dust", "Dust"),
    GameMap("cs_office", "Office"),
    GameMap("cs_workout", "Workout"),
    # Community maps -- neither ships with CS:GO. If they are not installed in
    # csgo/maps/ on the server, give them a workshop_id here (or via
    # /maps add name:... workshop_id:...) so host_workshop_map is used instead.
    GameMap("de_tulip", "Tulip"),
    GameMap("de_season", "Season"),
)

MAPS_BY_NAME: dict[str, GameMap] = {m.name: m for m in MAP_POOL}


def resolve_pool(
    names: list[str] | None, workshop_ids: dict[str, str] | None = None
) -> tuple[GameMap, ...]:
    """Turn a stored per-guild list of map names into GameMap objects."""
    workshop_ids = workshop_ids or {}
    if not names:
        names = [m.name for m in MAP_POOL]
    pool = []
    for name in names:
        known = MAPS_BY_NAME.get(name)
        display = known.display if known else name.replace("_", " ").title()
        workshop = workshop_ids.get(name) or (known.workshop_id if known else None)
        pool.append(GameMap(name, display, workshop))
    return tuple(pool)


def display_name(name: str) -> str:
    game_map = MAPS_BY_NAME.get(name)
    return game_map.display if game_map else name
