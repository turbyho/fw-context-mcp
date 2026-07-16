"""SDK/vendor path detection — low-level helpers used by both indexer and MCP layers."""

from __future__ import annotations

from pathlib import Path


def _path_matches(file_path: str, pattern: str) -> bool:
    """Check if *file_path* matches a SQL LIKE pattern (supports % wildcard).

    ``%`` matchuje jakoukoliv sekvenci znaků **včetně ``/``** (na rozdíl od
    fnmatch ``*``).  Používá regex pro korektní matchování vnořených adresářů:
    ``mbed-os/%`` matchuje ``mbed-os/targets/TARGET_NORDIC/foo.cpp``.

    Pro absolutni cesty (``project_paths`` mimo project root) funguje prvni
    vetev — druha vetev (prefixovy match ``*/pattern`` proti ``/file_path``)
    zachovava kompatibilitu s ``detect_sdk_exclude_like`` prefixovanymi patterny
    (``%mbed-os/%``).
    """
    import re

    # Escape regex special chars.  Use a placeholder for % before escaping
    # since re.escape() does NOT escape % (it's not a regex metachar).
    # Without a placeholder, a literal % in a file name would be treated as
    # a wildcard — acceptable risk for embedded firmware projects where %
    # in file names is essentially nonexistent.
    placeholder = "\x00"
    escaped = re.escape(pattern.replace("%", placeholder))
    # Replace placeholder with .* (match anything incl /)
    regex = escaped.replace(placeholder, ".*")
    # Anchor: full match from start to end
    regex = "^" + regex + "$"
    if re.match(regex, file_path):
        return True
    # Taky matchuj "*/pattern" proti "/file_path" — zachovava puvodni
    # prefixove chovani pro absolutni cesty a detect_sdk_exclude_like
    # prefixovane patterny (napr. %mbed-os/%)
    return bool(re.match("^.*/" + regex.lstrip("^"), "/" + file_path))


def _normalize_path_pattern(pattern: str) -> str:
    """Doplni '/%' k patternu bez wildcard, aby _path_matches (regex) matchoval podadresare.

    ``"third_party"`` → ``"third_party/%"`` (matchuje vsechny soubory v adresari)
    ``"mbed-os/%"`` → ``"mbed-os/%"`` (uz ma wildcard, beze zmeny)
    ``"/home/user/esp/components/muj_fork"`` → ``"/home/user/esp/components/muj_fork/%"``
    """
    if "%" not in pattern:
        pattern = pattern.rstrip("/") + "/%"
    return pattern


def _normalize_patterns(patterns: list[str]) -> list[str]:
    """Normalizuje seznam patternů pro _path_matches — každý pattern bez wildcard dostane '/%'.

    Centralizovaný wrapper volající _normalize_path_pattern pro každý pattern.
    Používaný z runner.py i maintenance.py.

    ``"third_party"`` → ``"third_party/%"``
    ``"/home/user/esp/components/muj_fork"`` → ``"/home/user/esp/components/muj_fork/%"``
    ``"mbed-os/%"`` → ``"mbed-os/%"`` (už má wildcard, beze změny)
    """
    return [_normalize_path_pattern(p) for p in patterns]


def _build_sdk_excludes(root: Path) -> list[str]:
    """Build default SDK exclude patterns from build system type.

    Returns LIKE patterns (with ``%`` wildcard) for vendor/SDK directories.
    Only true vendor/SDK source code — NOT build output (build/ is not SDK).
    """
    from .build import detect_build_system

    build_system = detect_build_system(root)
    if build_system is None:
        return []
    excludes: list[str] = []
    if build_system == "mbed-os":
        excludes.append("mbed-os/%")
    elif build_system == "platformio":
        excludes.extend([".pio/%", "%.platformio/%"])
    elif build_system == "zephyr":
        excludes.extend(["zephyr/%", "modules/%"])
    return excludes
