"""Platform-specific dependency fix instructions."""

from __future__ import annotations

import platform
import sys


def get_platform_info() -> dict:
    """Return platform metadata for branching fix instructions."""
    system = sys.platform
    machine = platform.machine()
    python_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    is_pyenv = _detect_pyenv()
    # Detect package manager
    pkg_manager = _detect_pkg_manager()
    pip_cmd = _detect_pip_cmd()

    return {
        "system": system,
        "machine": machine,
        "python_version": python_ver,
        "is_pyenv": is_pyenv,
        "pkg_manager": pkg_manager,
        "pip_cmd": pip_cmd,
    }


def _detect_pyenv() -> bool:
    """Return True when Python is managed by pyenv."""
    import os
    # pyenv sets PYENV_VERSION or PYENV_ROOT during installation
    if "PYENV_ROOT" in os.environ or "PYENV_VERSION" in os.environ:
        return True
    # Check if the Python executable lives under ~/.pyenv
    exe = sys.executable
    return "/.pyenv/" in exe


def _detect_pkg_manager() -> str:
    """Return the system package manager name or 'unknown'."""
    from shutil import which

    for manager, binary in [("apt", "apt"), ("dnf", "dnf"), ("pacman", "pacman"), ("brew", "brew")]:
        if which(binary):
            return manager
    return "unknown"


def _detect_pip_cmd() -> str:
    """Return the pip installer command."""
    from shutil import which

    if which("uv"):
        return "uv pip install"
    return f'"{sys.executable}" -m pip install'


# ── Per-dependency instructions ─────────────────────────────────────────


def pysqlite3_instructions(ctx: dict) -> str:
    """How to install pysqlite3."""
    pip = ctx.get("pip_cmd", "pip install")
    return f"{pip} pysqlite3"


def sqlite_ext_instructions(ctx: dict) -> str:
    """How to enable sqlite3 extension support."""
    system = ctx.get("system", sys.platform)
    is_pyenv = ctx.get("is_pyenv", False)

    if is_pyenv:
        return (
            'Rebuild Python with loadable extension support:\n'
            '  PYTHON_CONFIGURE_OPTS="--enable-loadable-sqlite-extensions" pyenv install '
            f'{ctx.get("python_version", "3.12")} --force\n'
            'Then reinstall fw-context.'
        )
    if system == "darwin":
        return (
            "macOS system Python lacks --enable-loadable-sqlite-extensions.\n"
            "  brew install python@3.12    # or\n"
            "  PYTHON_CONFIGURE_OPTS=\"--enable-loadable-sqlite-extensions\" pyenv install 3.12.x"
        )
    if system == "win32":
        return (
            "Windows Python from python.org includes extension support by default.\n"
            f"  {ctx.get('pip_cmd', 'pip install')} pysqlite3\n"
            "If the issue persists, reinstall Python from https://www.python.org/downloads/"
        )
    if system == "linux":
        return (
            "The Python installation was built without --enable-loadable-sqlite-extensions.\n"
            "  apt install libsqlite3-dev && PYTHON_CONFIGURE_OPTS=\"--enable-loadable-sqlite-extensions\" pyenv install "
            f'{ctx.get("python_version", "3.12")} --force\n'
            "Or install pysqlite3 which bundles a SQLite with extension support:\n"
            f"  {ctx.get('pip_cmd', 'pip install')} pysqlite3"
        )
    return "Reinstall Python with --enable-loadable-sqlite-extensions, or install pysqlite3."


def libclang_instructions(ctx: dict) -> str:
    """How to install libclang."""
    pkg = ctx.get("pkg_manager", "unknown")
    if pkg == "apt":
        return "apt install libclang-18-dev"
    elif pkg == "dnf":
        return "dnf install clang-devel"
    elif pkg == "pacman":
        return "pacman -S clang"
    elif pkg == "brew":
        return "brew install llvm@18"
    return "Install libclang (system package) and clang Python bindings (pip install libclang)"


def libclang_so_instructions(ctx: dict) -> str:
    """How to make libclang.so discoverable."""
    system = ctx.get("system", sys.platform)
    if system == "darwin":
        return "brew install llvm && ln -sf $(brew --prefix llvm)/lib/libclang.dylib /usr/local/lib/"
    if system == "win32":
        return (
            "Install LLVM from https://github.com/llvm/llvm-project/releases\n"
            "or via scoop:  scoop install llvm\n"
            "Ensure the LLVM bin directory is on PATH."
        )
    return (
        "Create a symlink so clang.cindex can find the library:\n"
        "  ln -sf /usr/lib/llvm-18/lib/libclang.so.1 /usr/lib/libclang.so\n"
        "Or set LD_LIBRARY_PATH to the directory containing libclang.so"
    )


def ollama_instructions(ctx: dict) -> str:
    """How to install Ollama."""
    system = ctx.get("system", sys.platform)
    if system == "darwin":
        return "brew install ollama && ollama serve"
    if system == "win32":
        return "Download Ollama from https://ollama.com/download/windows  &&  ollama serve"
    return "curl -fsSL https://ollama.com/install.sh | sh  &&  ollama serve"


def _vec_check_cmd(ctx: dict) -> str:
    """Platform-specific command to inspect shared library dependencies."""
    system = ctx.get("system", sys.platform)
    so_path = "sqlite_vec/vec0"
    if system == "darwin":
        return (
            'otool -L $(python -c "import sqlite_vec; '
            "print(sqlite_vec.__file__.replace('__init__.py', 'vec0.dylib'))"
            '")'
        )
    if system == "win32":
        return (
            f"dumpbin /dependents <site-packages>\\{so_path}.dll"
        )
    return (
        'ldd $(python -c "import sqlite_vec; '
        "print(sqlite_vec.__file__.replace('__init__.py', 'vec0.so'))"
        '")'
    )


def vec0_load_error_instructions(error_msg: str, ctx: dict) -> str:
    """Diagnose sqlite-vec load failures (missing .so dependencies)."""
    if "cannot open shared object file" in error_msg:
        return (
            f"sqlite-vec native extension failed to load: {error_msg}\n"
            "The .so file may be missing system dependencies.  Check with:\n"
            '  {check_cmd}\n'
            "Missing libraries (e.g. libgcc_s.so.1) must be installed via the system package manager."
        ).format(
            check_cmd=_vec_check_cmd(ctx),
        )
    if "undefined symbol" in error_msg:
        return (
            f"sqlite-vec has an unresolved symbol: {error_msg}\n"
            "This usually means the SQLite version is too old or incompatible.\n"
            f"  {ctx.get('pip_cmd', 'pip install')} --upgrade sqlite-vec pysqlite3"
        )
    return f"sqlite-vec load error: {error_msg}\n  pip install --upgrade sqlite-vec pysqlite3"


def watchfiles_instructions(ctx: dict) -> str:
    """How to install watchfiles."""
    return f"{ctx.get('pip_cmd', 'pip install')} watchfiles"


def db_integrity_instructions(ctx: dict) -> str:
    """How to recover from database corruption."""
    return (
        "The index database is corrupt.  Rebuild:\n"
        "  fw-context reset_index\n"
        "  fw-context index --build"
    )


def disk_space_instructions(ctx: dict) -> str:
    """How to free disk space."""
    return "Free up disk space.  The index and embedding cache need headroom."
