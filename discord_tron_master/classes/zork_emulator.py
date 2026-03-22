"""Legacy import shim for the text-game-engine-backed emulator.

The active Discord runtime uses ``EmulatorBridge``, which delegates to
``text_game_engine.ZorkEmulator``. Keep this module as a compatibility import
path only so old code can still do:

    from discord_tron_master.classes.zork_emulator import ZorkEmulator
"""

from discord_tron_master.adapters.emulator_bridge import EmulatorBridge as ZorkEmulator

__all__ = ["ZorkEmulator"]
