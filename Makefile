# fw-context — install, update, uninstall
#
# Quick start:
#   git clone git@github.com:turbyho/fw-context-mcp.git ~/.fw-context/src
#   cd ~/.fw-context/src && make install
#
# Update:
#   cd ~/.fw-context/src && make update
#
# Remove:
#   cd ~/.fw-context/src && make uninstall

# Auto-detect source directory (the directory containing this Makefile)
SRC       := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
VENV      ?= $(HOME)/.fw-context/.venv
PYTHON    ?= python3
# Is uv USABLE — not "is there a file named uv on PATH".
#
# pyenv and asdf put a shim on PATH for every tool ever installed under ANY
# interpreter they manage, so `command -v uv` finds one whether or not the
# ACTIVE interpreter has uv.  Measured here: `command -v uv` gave
# ~/.pyenv/shims/uv, and running it printed "pyenv: uv: command not found"
# and exited 127.  The detection saw a file, took the uv branch, and
# `make install` died at pip-install — with a message about pyenv that named
# neither uv nor this Makefile.
#
# So ask the binary to run.  A shim that dispatches correctly passes; a shim
# that cannot does not.  This is the same rule utils.resolve_real_binary
# applies to compiledb, for the same reason.
UV := $(shell uv --version >/dev/null 2>&1 && command -v uv)

# Every target below installs into $(VENV) and NEVER through a bare `pip`.
# A bare `pip` is a shim too — it resolves to ~/.pyenv/shims/pip here, which
# installs into whichever interpreter pyenv has active.  $(VENV)/bin/python
# -m pip cannot be redirected that way.

# ---- install ----
install: venv pip-install link-add clean-path
	@echo ""
	@echo "fw-context installed."
	@echo "  fw-context init"
	@echo ""

# ---- update ----
# Pull, then run the ONE installer.  This target used to carry its own copy
# of the install command and the copy had drifted three ways: it called a
# bare `pip`, it passed `--reinstall`, which is a flag of uv and not of pip,
# and neither branch passed `-e`.  So a successful `make update` replaced the
# editable install with a copy and put back the trap that e62efc5 removed —
# a test could then pass against code that no longer existed.
update:
	@echo "Updating fw-context..."
	@cd $(SRC) && git pull
	@$(MAKE) --no-print-directory pip-install
	@echo "fw-context updated."

# ---- uninstall ----
uninstall:
	@echo "Removing symlinks from ~/.local/bin ..."
	@rm -f $(HOME)/.local/bin/fw-context $(HOME)/.local/bin/fw-context-mcp
	@echo "Removing ~/.fw-context ..."
	@rm -rf $(HOME)/.fw-context
	@echo "fw-context removed."

# ---- venv ----
venv:
ifeq ($(UV),)
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)
else
	@test -d $(VENV) || $(UV) venv $(VENV) --python $(PYTHON) --seed
endif

# ---- pip install into venv ----
# Editable (-e), and that is not a convenience.  The test and lint targets
# below run out of $(VENV), so a copied install made them measure whatever
# the last `make install` had put there — a source change that was not
# reinstalled was invisible, and a test could pass against code that no
# longer existed.  Editable removes the step and the trap with it.
#
# CI already installs with `pip install -e ".[dev]"` (.github/workflows/
# test.yml), thus this also stops local runs and CI from disagreeing.
#
# The price: `fw-context` on the PATH follows the working tree.  A process
# that already runs keeps the modules it imported, so an edit reaches it on
# the next start, not mid-session.
pip-install: venv
ifeq ($(UV),)
	$(VENV)/bin/python -m pip install -e $(SRC)
else
	$(UV) pip install --python $(VENV)/bin/python -e $(SRC)
endif

# ---- symlink binaries into ~/.local/bin ----
link-add:
	@mkdir -p $(HOME)/.local/bin
	@for bin in $(VENV)/bin/fw-context $(VENV)/bin/fw-context-mcp; do \
		name=$$(basename "$$bin"); \
		link="$(HOME)/.local/bin/$$name"; \
		if [ -L "$$link" ] || [ -e "$$link" ]; then \
			rm -f "$$link"; \
		fi; \
		ln -s "$$bin" "$$link"; \
		echo "  $$link -> $$bin"; \
	done

# ---- remove old PATH entries from shell rc files ----
clean-path:
	@for rc in $(HOME)/.zshrc $(HOME)/.bashrc; do \
		if [ -f "$$rc" ] && grep -q '.fw-context/.venv/bin' "$$rc" 2>/dev/null; then \
			sed -i '\|export PATH="$$HOME/.fw-context/.venv/bin:$$PATH"|d' "$$rc"; \
			echo "  Removed old PATH entry from $$rc"; \
		fi; \
	done


# ---- dev setup (install all deps including pytest, ruff, mypy, bandit) ----
# Adds the dev tools to the SAME venv every other target uses.  It ran
# `uv sync --extra dev` before, and uv sync manages the PROJECT environment
# — <repo>/.venv — while test, lint and lint-security run out of $(VENV).
# So `make dev` equipped one venv and `make test` used another, which is
# what produced the second venv in the first place.
dev: venv
ifeq ($(UV),)
	$(VENV)/bin/python -m pip install -e "$(SRC)[dev]"
else
	$(UV) pip install --python $(VENV)/bin/python -e "$(SRC)[dev]"
endif
	@echo ""
	@echo "Dev environment ready. Run: make test, make lint, make lint-security"

# ---- security scan ----
lint-security:
	$(VENV)/bin/bandit -r src/ -c pyproject.toml

# ---- test targets ----
# fast subset: excludes tests needing ollama, libclang, C compiler, and slow indexing
test:
	$(VENV)/bin/pytest tests/ -q -m "not ollama and not libclang and not slow"

lint:
	$(VENV)/bin/ruff check src/
	$(VENV)/bin/mypy src/

# all tests except system and slow (both require manual invocation)
test-all:
	$(VENV)/bin/pytest tests/ -q

# slow tests only — full indexing runs, takes 30+ minutes
test-slow:
	$(VENV)/bin/pytest tests/ -q -m "slow"

.PHONY: install update uninstall venv pip-install link-add clean-path dev test lint lint-security test-all test-slow
