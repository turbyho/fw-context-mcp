"""The libclang binding of fw-context, with the settings that each parse needs.

Each module of fw-context that parses or creates an ``Index`` imports the
binding from here, thus no parse runs without these settings.

WHY ``libcxx_dir = None``: libclang-ng (the wheel that gives libclang, its
binding and its compiler headers) changes ``Index.parse``.  It adds
``-isystem`` of its bundled libc++ headers to each parse whose arguments
have no ``-isystem``.  A firmware build uses the C++ library of its own
toolchain (libstdc++ of GCC), and the libc++ headers in front of it gave
errors in a C++ parse (``ldiv_t``).  ``parse`` reads the value at each call,
thus the setting applies to each parse after this import.

``Config.resource_dir``: ``parse`` adds ``-resource-dir`` of the wheel to a
parse that has none.  The wheel lacks the generated ARM and RISC-V
intrinsic headers (see ``_clang_resource``), thus ``symbols._get_index``
points it to the headers that ``_clang_resource`` finds, at each call.
"""

from __future__ import annotations

from clang import cindex

cindex.Config.libcxx_dir = None

__all__ = ["cindex"]
