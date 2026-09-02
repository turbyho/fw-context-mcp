"""Read which interrupt each handler was registered for, from the build.

A build that generates its interrupt table writes resolved ADDRESSES into
the generated C, so the slots that are actually in use carry no name that
an index can read.  Measured on the Zephyr project: ``_sw_isr_table`` is
declared ``[290]``, 284 slots name the spurious stub, and the 6 in use name
nothing at all.

The registrations are in the build, though.  Zephyr's ``IRQ_CONNECT``
writes one ``struct _isr_list`` into a ``.intList`` section, the linker
gathers them into ``zephyr_pre0.elf``, and ``gen_isr_tables.py`` reads that
to emit the table.  Reading the same section answers "which interrupt does
this handler serve" with a name.

Verified on three configurations, where the interrupt numbers matched the
gaps the index had already found, every time:

    nRF52840 app       48 slots, 5 registrations: 0, 2, 6, 17, 41
    nRF54L   app      290 slots, 6 registrations: 89, 198, 219, 228, 269, 270
    nRF54L   app_flpr 287 slots, 3 registrations: 234, 242, 284   (RISC-V)

**The object files are NOT read**, although they carry the same section
with the names as relocations, which needs no address resolution.  They
were measured to live in archives — ``libdrivers__gpio.a`` and four more —
and a build that ships only an archive or a prebuilt library would be
missed without a sound.  The linked artifact is complete by construction.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Zephyr writes `struct int_list_header { uint32_t table_size; uint32_t
# offset; }` and then one `struct _isr_list { int32_t irq; int32_t flags;
# void *func; const void *param; }` for each registration.
_HEADER_SIZE = 8
_ENTRY_SIZE = 16

_SECTION = ".intList"
_ARTIFACT = Path("zephyr") / "zephyr_pre0.elf"
_KCONFIG = Path("zephyr") / ".config"
_DYNAMIC_OPTION = "CONFIG_DYNAMIC_INTERRUPTS=y"

# Symbol types worth resolving an address to: a handler is a function, and
# an argument is usually a device instance.
_SYMBOL_FUNC = 2
_SYMBOL_OBJECT = 1


@dataclass(frozen=True)
class IsrRegistration:
    """One ``IRQ_CONNECT``, as the build recorded it.

    Attributes:
        irq: The interrupt number the source asked for.
        slot: The index in the software table, which is *irq* less the
            start vector of the build.  The two differ whenever
            ``CONFIG_GEN_IRQ_START_VECTOR`` is not zero, so the offset is
            read from the artifact and never assumed.
        handler: Name of the function the interrupt reaches.
        argument: Name of the symbol passed to the handler, or "" when the
            argument is a plain value rather than a symbol.  It is an
            ARGUMENT and not a second handler: measured, it is
            ``nrfx_power_clock_irq_handler`` behind the ``nrfx_isr`` shim
            in one driver and the device ``__device_dts_ord_116`` in
            another.  Only a reader who knows the handler can say which.
    """

    irq: int
    slot: int
    handler: str
    argument: str


@dataclass(frozen=True)
class IsrTable:
    """Every registration one build recorded, and what limits the answer.

    Attributes:
        registrations: One entry for each ``IRQ_CONNECT`` in the build.
        declared: Length of the software table, from the artifact.
        offset: The start vector, which turns an interrupt number into a
            slot.
        dynamic: True when the build enables ``CONFIG_DYNAMIC_INTERRUPTS``.
            An interrupt connected at run time leaves nothing in the
            section, so the list is then INCOMPLETE and a reader has to be
            told.
        artifact: The file the answer came from.
    """

    registrations: tuple[IsrRegistration, ...]
    declared: int
    offset: int
    dynamic: bool
    artifact: str


@dataclass(frozen=True)
class _Section:
    """One ELF section header, reduced to the fields this needs."""

    name: str
    offset: int
    size: int
    link: int       # index of a related section, e.g. a symbol table's strings
    item_size: int  # size of one entry, for a table


class _Elf:
    """The little of ELF this needs: section contents and symbol addresses.

    Written here rather than taken from a dependency because the whole of
    it is under a hundred lines, and a section plus a symbol table is all
    that is wanted.  32-bit only, which is what firmware is; a 64-bit file
    is refused rather than decoded by guess, because the entry layout grows
    with the pointer and there was no 64-bit build to verify against.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data = path.read_bytes()
        if self._data[:4] != b"\x7fELF":
            raise ValueError(f"{path} is not an ELF file")
        if self._data[4] != 1:
            raise ValueError(f"{path} is not a 32-bit ELF file")
        self.byte_order = "<" if self._data[5] == 1 else ">"
        # Kept as a list because a section header refers to another section
        # BY INDEX — a symbol table points at the string table holding its
        # names that way — so index has to stay the primary key.
        self._sections: list[_Section] = self._read_section_table()
        self._by_name = {section.name: section for section in self._sections}

    def _u(self, fmt: str, offset: int) -> tuple:
        return struct.unpack_from(self.byte_order + fmt, self._data, offset)

    def _read_section_table(self) -> list[_Section]:
        table_offset, = self._u("I", 32)
        entry_size, count, name_index = self._u("HHH", 46)
        if not table_offset or not count or name_index >= count:
            raise ValueError(f"{self._path} has no usable section table")

        # The name string table is found first, because every other
        # section's name is an offset into it.
        names_entry = table_offset + name_index * entry_size
        names_offset, names_size = self._u("II", names_entry + 16)
        names = self._data[names_offset:names_offset + names_size]

        found: list[_Section] = []
        for index in range(count):
            entry = table_offset + index * entry_size
            name_offset, = self._u("I", entry)
            offset, size = self._u("II", entry + 16)
            link, = self._u("I", entry + 24)
            item_size, = self._u("I", entry + 36)
            end = names.find(b"\x00", name_offset)
            found.append(_Section(
                name=names[name_offset:end].decode("ascii", "replace"),
                offset=offset,
                size=size,
                link=link,
                item_size=item_size,
            ))
        return found

    def section(self, name: str) -> bytes:
        """Return the bytes of *name*, or empty when there is no such section."""
        found = self._by_name.get(name)
        if found is None:
            return b""
        return self._data[found.offset:found.offset + found.size]

    def addresses_to_names(self) -> dict[int, str]:
        """Map the address of every function and object symbol to its name.

        The address is used exactly as stored.  On ARM a Thumb function
        carries its low bit set, and the ``.intList`` entry carries the
        same odd value, so the two match without masking — measured, along
        with the even values of a RISC-V build.

        The first name for an address wins, so a later alias cannot
        displace the definition it aliases.
        """
        symbols = self._by_name.get(".symtab")
        if symbols is None or symbols.item_size < 16:
            return {}
        if symbols.link >= len(self._sections):
            return {}
        strings = self._sections[symbols.link]
        if not strings.size:
            return {}
        names = self._data[strings.offset:strings.offset + strings.size]

        out: dict[int, str] = {}
        end_of_table = symbols.offset + symbols.size
        for position in range(symbols.offset, end_of_table, symbols.item_size):
            if position + 16 > end_of_table:
                break
            name_offset, address = self._u("II", position)
            kind = self._data[position + 12] & 0xF
            if kind not in (_SYMBOL_FUNC, _SYMBOL_OBJECT):
                continue
            if not address or address in out:
                continue
            end = names.find(b"\x00", name_offset)
            name = names[name_offset:end].decode("ascii", "replace")
            if name:
                out[address] = name
        return out


def read_isr_registrations(build_dir: Path) -> IsrTable | None:
    """Read the interrupt registrations one build recorded.

    Returns None whenever the answer is not there to be had: no artifact,
    no ``.intList`` section, or a file this cannot decode.  That is the
    quiet path on purpose — a build system that records nothing here is
    not an error, and measured on Mbed, CMSIS and Arduino projects not one
    artifact of 60 carried the section.

    Args:
        build_dir: The directory holding ``compile_commands.json``, whose
            ``zephyr/`` subdirectory holds the artifact.
    """
    artifact = build_dir / _ARTIFACT
    if not artifact.is_file():
        return None

    try:
        elf = _Elf(artifact)
        raw = elf.section(_SECTION)
    except (OSError, ValueError, struct.error):
        log.debug("could not read %s from %s", _SECTION, artifact, exc_info=True)
        return None

    if len(raw) < _HEADER_SIZE:
        return None

    try:
        declared, offset = struct.unpack_from(elf.byte_order + "II", raw, 0)
        names = elf.addresses_to_names()
        registrations = _decode(raw, declared, offset, names, elf.byte_order)
    except (ValueError, struct.error):
        log.debug("could not decode %s of %s", _SECTION, artifact, exc_info=True)
        return None

    return IsrTable(
        registrations=registrations,
        declared=declared,
        offset=offset,
        dynamic=_dynamic_interrupts(build_dir),
        artifact=str(artifact),
    )


def _decode(
    raw: bytes,
    declared: int,
    offset: int,
    names: dict[int, str],
    byte_order: str,
) -> tuple[IsrRegistration, ...]:
    """Turn the section body into registrations, dropping what makes no sense."""
    out: list[IsrRegistration] = []
    for position in range(_HEADER_SIZE, len(raw) - _ENTRY_SIZE + 1, _ENTRY_SIZE):
        irq, _flags, func, param = struct.unpack_from(
            byte_order + "iiII", raw, position,
        )
        handler = names.get(func, "")
        if not handler:
            # An address with no symbol is not worth a row: the whole point
            # of reading this is the name.
            continue
        slot = irq - offset
        if not 0 <= slot < declared:
            # A slot outside the table means the header and the entries
            # disagree, and a number that cannot be trusted is worse than
            # no number.
            log.debug("dropping irq %d: slot %d outside 0..%d", irq, slot, declared)
            continue
        out.append(IsrRegistration(
            irq=irq,
            slot=slot,
            handler=handler,
            argument=names.get(param, ""),
        ))
    return tuple(out)


def _dynamic_interrupts(build_dir: Path) -> bool:
    """Report whether the build connects interrupts at run time.

    ``irq_connect_dynamic`` leaves nothing in the section, so this decides
    whether the registrations are the whole story.  Kconfig has to be read
    from the build's own ``.config``: it reaches the compiler through
    ``-imacros autoconf.h`` and never as a ``-D`` flag, so the flags in
    ``compile_commands.json`` do not carry it — measured, zero
    ``-DCONFIG_*`` among them.
    """
    config = build_dir / _KCONFIG
    try:
        for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip() == _DYNAMIC_OPTION:
                return True
    except OSError:
        log.debug("could not read %s", config, exc_info=True)
    return False
