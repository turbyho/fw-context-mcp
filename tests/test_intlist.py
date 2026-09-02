"""Reading interrupt registrations out of a build's `.intList` section.

The generated table holds resolved addresses for the slots in use, so the
interrupts a firmware actually services carry no name an index can read.
The registrations are in the build though, and this reads them.

Every ELF here is BUILT BY THE TEST rather than copied from a real build,
so the suite does not depend on a checkout of somebody's firmware.  The
values are the ones measured on zbox-ecb-fw-v5, and a separate check
against the real artifacts is in the commit message.
"""
import struct

import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.indexer.intlist import read_isr_registrations

SECTION_HEADER = 40
SYMBOL = 16


def _elf(intlist: bytes, symbols: list[tuple[str, int, int]], *,
         little: bool = True, elf_class: int = 1,
         section_name: str = ".intList") -> bytes:
    """Build a 32-bit ELF holding one data section and a symbol table.

    *symbols* is a list of (name, address, kind), where kind is the ELF
    symbol type — 2 for a function, 1 for an object.
    """
    order = "<" if little else ">"

    names = b"\x00"
    name_offsets = []
    for name, _address, _kind in symbols:
        name_offsets.append(len(names))
        names += name.encode("ascii") + b"\x00"

    symtab = b""
    for (name, address, kind), name_offset in zip(symbols, name_offsets, strict=True):
        del name
        symtab += struct.pack(order + "IIIBBH", name_offset, address, 0, kind, 0, 1)

    shstr = b"\x00"
    section_names = {}
    for label in (section_name, ".symtab", ".strtab", ".shstrtab"):
        section_names[label] = len(shstr)
        shstr += label.encode("ascii") + b"\x00"

    # Lay the payloads out after the header, then the section table last.
    header_size = 52
    offsets = {}
    cursor = header_size
    for label, payload in (
        (section_name, intlist), (".symtab", symtab),
        (".strtab", names), (".shstrtab", shstr),
    ):
        offsets[label] = cursor
        cursor += len(payload)
    table_offset = cursor

    def header(label: str, payload: bytes, link: int, item: int) -> bytes:
        return struct.pack(
            order + "IIIIIIIIII",
            section_names[label], 1, 0, 0x1000,
            offsets[label], len(payload), link, 0, 4, item,
        )

    table = b"\x00" * SECTION_HEADER                      # [0] the null section
    table += header(section_name, intlist, 0, 0)          # [1]
    table += header(".symtab", symtab, 3, SYMBOL)         # [2] links to [3]
    table += header(".strtab", names, 0, 0)               # [3]
    table += header(".shstrtab", shstr, 0, 0)             # [4]

    elf = bytearray(header_size)
    elf[0:4] = b"\x7fELF"
    elf[4] = elf_class
    elf[5] = 1 if little else 2
    elf[6] = 1
    struct.pack_into(order + "I", elf, 32, table_offset)
    struct.pack_into(order + "HHH", elf, 46, SECTION_HEADER, 5, 4)

    return bytes(elf) + intlist + symtab + names + shstr + table


def _intlist(declared: int, offset: int, entries: list[tuple[int, int, int]],
             *, little: bool = True) -> bytes:
    """Pack a header and (irq, func, param) entries the way Zephyr does."""
    order = "<" if little else ">"
    raw = struct.pack(order + "II", declared, offset)
    for irq, func, param in entries:
        raw += struct.pack(order + "iiII", irq, 0, func, param)
    return raw


def _build(tmp_path, intlist: bytes, symbols, *, config: str | None = None,
           **kwargs):
    """Write an artifact where the reader looks for it, plus optional Kconfig."""
    zephyr = tmp_path / "zephyr"
    zephyr.mkdir(exist_ok=True)
    (zephyr / "zephyr_pre0.elf").write_bytes(_elf(intlist, symbols, **kwargs))
    if config is not None:
        (zephyr / ".config").write_text(config, encoding="utf-8")
    return tmp_path


def test_the_measured_shape_reads_back(tmp_path):
    """The nRF52840 case, with the addresses and names it really had.

    ``nrfx_isr`` serves two interrupts and its ARGUMENT differs, which is
    the shape that makes the argument worth reporting at all.
    """
    _build(
        tmp_path,
        _intlist(48, 0, [
            (0, 0x2798F, 0x27A57),
            (41, 0x2798F, 0x22DD5),
            (2, 0x27773, 0x28BE4),
        ]),
        [
            ("nrfx_isr", 0x2798F, 2),
            ("nrfx_power_clock_irq_handler", 0x27A57, 2),
            ("nrfx_qspi_irq_handler", 0x22DD5, 2),
            ("uarte_nrfx_isr_int", 0x27773, 2),
            ("__device_dts_ord_116", 0x28BE4, 1),
        ],
    )

    table = read_isr_registrations(tmp_path)
    assert table is not None
    assert table.declared == 48
    assert [(r.irq, r.handler, r.argument) for r in table.registrations] == [
        (0, "nrfx_isr", "nrfx_power_clock_irq_handler"),
        (41, "nrfx_isr", "nrfx_qspi_irq_handler"),
        (2, "uarte_nrfx_isr_int", "__device_dts_ord_116"),
    ]


def test_the_slot_is_the_interrupt_less_the_start_vector(tmp_path):
    """``CONFIG_GEN_IRQ_START_VECTOR`` is 0 on the builds measured here.

    It need not be, and the offset is in the header for that reason.  Code
    that assumed zero would be wrong for every entry of a build that sets
    it, silently.
    """
    _build(tmp_path, _intlist(64, 16, [(20, 0x1001, 0)]),
           [("handler", 0x1001, 2)])

    table = read_isr_registrations(tmp_path)
    assert table is not None
    assert table.offset == 16
    assert (table.registrations[0].irq, table.registrations[0].slot) == (20, 4)


def test_an_argument_that_is_not_a_symbol_is_left_empty(tmp_path):
    """``rtc_nrf_isr`` was measured with a plain value as its argument."""
    _build(tmp_path, _intlist(48, 0, [(17, 0x21D21, 0)]),
           [("rtc_nrf_isr", 0x21D21, 2)])

    table = read_isr_registrations(tmp_path)
    assert table is not None
    assert table.registrations[0].handler == "rtc_nrf_isr"
    assert table.registrations[0].argument == ""


def test_an_entry_whose_handler_has_no_name_is_dropped(tmp_path):
    """A row without a name is not worth having — the name is the point."""
    _build(tmp_path, _intlist(48, 0, [(3, 0xDEAD, 0), (4, 0x1001, 0)]),
           [("known", 0x1001, 2)])

    table = read_isr_registrations(tmp_path)
    assert table is not None
    assert [r.irq for r in table.registrations] == [4]


def test_a_slot_outside_the_table_is_dropped(tmp_path):
    """Header and entries disagreeing means the number cannot be trusted."""
    _build(tmp_path, _intlist(8, 0, [(99, 0x1001, 0), (5, 0x1001, 0)]),
           [("handler", 0x1001, 2)])

    table = read_isr_registrations(tmp_path)
    assert table is not None
    assert [r.irq for r in table.registrations] == [5]


def test_a_truncated_entry_is_ignored(tmp_path):
    """A short tail must not be read as a whole entry."""
    complete = _intlist(48, 0, [(7, 0x1001, 0)])
    _build(tmp_path, complete + b"\x08\x00\x00\x00", [("handler", 0x1001, 2)])

    table = read_isr_registrations(tmp_path)
    assert table is not None
    assert [r.irq for r in table.registrations] == [7]


def test_big_endian_is_read_too(tmp_path):
    """The byte order comes from the file, not from the host."""
    _build(
        tmp_path,
        _intlist(48, 0, [(9, 0x1001, 0)], little=False),
        [("handler", 0x1001, 2)],
        little=False,
    )

    table = read_isr_registrations(tmp_path)
    assert table is not None
    assert [(r.irq, r.handler) for r in table.registrations] == [(9, "handler")]


class TestDynamicInterrupts:
    """A run-time registration leaves nothing here, so it has to be flagged.

    Kconfig is read from the build's own ``.config``, because it reaches the
    compiler through ``-imacros autoconf.h`` and never as a ``-D`` — so the
    flags in compile_commands.json do not carry it.
    """

    INTLIST = _intlist(48, 0, [(1, 0x1001, 0)])
    SYMBOLS = [("handler", 0x1001, 2)]

    def test_the_option_is_seen(self, tmp_path):
        _build(tmp_path, self.INTLIST, self.SYMBOLS,
               config="CONFIG_FOO=y\nCONFIG_DYNAMIC_INTERRUPTS=y\n")

        table = read_isr_registrations(tmp_path)
        assert table is not None
        assert table.dynamic is True

    @pytest.mark.parametrize("line", [
        # What Kconfig itself writes for an option that is off.
        "# CONFIG_DYNAMIC_INTERRUPTS is not set",
        # A commented-out assignment, which a hand-edited fragment can hold.
        # This is the case that makes the match have to be the WHOLE line:
        # a substring test reads it as switched on.
        "# CONFIG_DYNAMIC_INTERRUPTS=y",
        "  # CONFIG_DYNAMIC_INTERRUPTS=y",
        # A different option that merely starts the same way.
        "CONFIG_DYNAMIC_INTERRUPTS_EXTRA=y",
        "CONFIG_DYNAMIC_INTERRUPTS=n",
    ])
    def test_the_option_not_actually_set_is_not_a_match(self, tmp_path, line):
        """Only the exact assignment counts.

        Reporting the list as incomplete when it is not would push a reader
        to distrust a complete answer, which is its own kind of wrong.
        """
        _build(tmp_path, self.INTLIST, self.SYMBOLS, config=line + "\n")

        table = read_isr_registrations(tmp_path)
        assert table is not None
        assert table.dynamic is False

    def test_no_kconfig_at_all(self, tmp_path):
        _build(tmp_path, self.INTLIST, self.SYMBOLS)

        table = read_isr_registrations(tmp_path)
        assert table is not None
        assert table.dynamic is False


class TestNothingToRead:
    """Every quiet path, because a build that records nothing is normal.

    Measured: not one artifact of 60 on Mbed, CMSIS and Arduino projects
    carried the section.
    """

    def test_no_artifact(self, tmp_path):
        assert read_isr_registrations(tmp_path) is None

    def test_a_file_that_is_not_an_elf(self, tmp_path):
        zephyr = tmp_path / "zephyr"
        zephyr.mkdir()
        (zephyr / "zephyr_pre0.elf").write_bytes(b"not an elf at all")
        assert read_isr_registrations(tmp_path) is None

    def test_a_64_bit_elf_is_refused_not_guessed(self, tmp_path):
        """The entry layout grows with the pointer.

        There was no 64-bit build to verify against, so decoding one would
        be a guess — and a guess here produces wrong interrupt numbers.
        """
        _build(tmp_path, _intlist(48, 0, [(1, 0x1001, 0)]),
               [("handler", 0x1001, 2)], elf_class=2)
        assert read_isr_registrations(tmp_path) is None

    def test_no_intlist_section(self, tmp_path):
        _build(tmp_path, b"", [("handler", 0x1001, 2)],
               section_name=".text")
        assert read_isr_registrations(tmp_path) is None

    def test_a_header_with_no_entries(self, tmp_path):
        _build(tmp_path, _intlist(48, 0, []), [("handler", 0x1001, 2)])

        table = read_isr_registrations(tmp_path)
        assert table is not None
        assert table.registrations == ()

    @pytest.mark.parametrize("size", [0, 1, 7])
    def test_a_section_too_short_for_a_header(self, tmp_path, size):
        _build(tmp_path, b"\x00" * size, [("handler", 0x1001, 2)])
        assert read_isr_registrations(tmp_path) is None
