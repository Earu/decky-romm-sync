"""ActiveCoreResolver — the single read-path core-resolution seam per ROM.

The one place that answers "which RetroArch core will this ROM actually launch
with?", combining the per-game ``emulator_override`` and per-platform core
selection (the two deviations the plugin owns) with the system-layer
ES-DE/RetroDECK resolution. Every per-game core read consumer and every
launch-bake site draws from this seam so the read-path core never diverges from
the launched core.

Precedence: DB ``emulator_override`` (top) → ``settings.json`` per-platform core
→ es_systems default → core_defaults. The retired ES-DE gamelist
``<alternativeEmulator>`` is never consulted. A pinned per-game or per-platform
label that no longer resolves to a ``.so`` degrades to the next layer rather than
raising — so a stale label never blocks a read or bakes a bogus ``None.so``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.shortcut_data import EmulatorInvocation, label_to_core_so

if TYPE_CHECKING:
    import logging

    from domain.rom import Rom
    from services.protocols import (
        CoreInfoProvider,
        PlatformCoreReader,
        SystemResolver,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class ActiveCoreResolverConfig:
    """Frozen wiring bundle handed to ``ActiveCoreResolver.__init__``.

    Carries the SQLite Unit-of-Work factory (to read the ROM's
    ``platform_slug`` + ``emulator_override``), the ES-DE core-info read seam
    (available cores + system-layer active core), the per-platform core reader
    (the ``settings.json`` ``platform_cores`` map), the platform-slug-to-system
    resolver, and the logger used to warn on a stale label.
    """

    uow_factory: UnitOfWorkFactory
    core_info: CoreInfoProvider
    platform_core_reader: PlatformCoreReader
    resolve_system: SystemResolver
    logger: logging.Logger


class ActiveCoreResolver:
    """Resolve the active RetroArch core for one ROM by ``rom_id``."""

    def __init__(self, *, config: ActiveCoreResolverConfig) -> None:
        self._uow_factory = config.uow_factory
        self._core_info = config.core_info
        self._platform_core_reader = config.platform_core_reader
        self._resolve_system = config.resolve_system
        self._logger = config.logger

    def active_emulator_for_rom(self, rom_id: int) -> EmulatorInvocation | None:
        """Return the :class:`EmulatorInvocation` the ROM ``rom_id`` will launch with.

        The launch-bake seam. Reads the ROM's ``platform_slug`` +
        ``emulator_override`` once, then applies the four-layer precedence:

        1. Per-game DB ``emulator_override`` (a libretro core LABEL from the
           picker) → a libretro invocation when it resolves.
        2. Per-platform ``settings.json`` core (also a libretro LABEL) → a
           libretro invocation when it resolves.
        3. / 4. System-layer default via ``get_default_emulator`` — which is
           **standalone-aware**: it returns a standalone invocation (PCSX2, RPCS3,
           …) for systems whose working emulator isn't a libretro core, else the
           libretro es_systems / ``core_defaults`` default.

        Returns ``None`` when the platform has no resolvable emulator at all (the
        caller bakes the plain launch). A stale per-game/per-platform label is
        never fatal — it degrades to the next layer with a WARNING.
        """
        rom = self._read_rom(rom_id)
        if rom is None:
            self._logger.warning("active_core_resolver: no ROM for rom_id=%s; resolving to plain launch", rom_id)
            return None

        system = self._resolve_system(rom.platform_slug)
        available = self._core_info.get_available_cores(system)

        override = rom.emulator_override
        if override is not None:
            core_so = label_to_core_so(available, override)
            if core_so is not None:
                return EmulatorInvocation.libretro(core_so, override)
            self._logger.warning(
                "active_core_resolver: per-game override '%s' for rom_id=%s no longer resolves on %s; "
                "degrading to the per-platform/system default",
                override,
                rom_id,
                system,
            )

        platform_label = self._platform_core_reader.get_platform_core(rom.platform_slug)
        if platform_label is not None:
            core_so = label_to_core_so(available, platform_label)
            if core_so is not None:
                return EmulatorInvocation.libretro(core_so, platform_label)
            self._logger.warning(
                "active_core_resolver: per-platform core '%s' for %s (rom_id=%s) no longer resolves; "
                "degrading to the system default",
                platform_label,
                rom.platform_slug,
                rom_id,
            )

        return self._core_info.get_default_emulator(system)

    def active_core_for_rom(self, rom_id: int) -> tuple[str | None, str | None]:
        """Return the ``(core_so, label)`` the ROM ``rom_id`` will launch with.

        The read-path projection of :meth:`active_emulator_for_rom`, kept for the
        ``.so``-space consumers (BIOS status, per-core save dir, save-emulator
        tag, core-change detection, the cores menu's active marker). A **libretro**
        emulator yields its ``(core_so, label)``; a **standalone** emulator yields
        ``(None, label)`` — those consumers already degrade on a ``None`` core
        exactly as they did for the old ``(None, None)`` resolution, so the
        read-path core never disagrees with the (now possibly standalone) launch.
        """
        emulator = self.active_emulator_for_rom(rom_id)
        if emulator is None:
            return (None, None)
        return (emulator.core_so, emulator.label)

    def _read_rom(self, rom_id: int) -> Rom | None:
        with self._uow_factory() as uow:
            return uow.roms.get(rom_id)
