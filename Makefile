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
PYTHON    ?= python3.12
UV        := $(shell command -v uv 2>/dev/null)

# ---- install ----
install: venv pip-install link-add clean-path
	@echo ""
	@echo "fw-context installed."
	@echo "  fw-context init"
	@echo ""

# ---- update ----
update:
	@echo "Updating fw-context..."
	@cd $(SRC) && git pull
ifeq ($(UV),)
	pip install --reinstall $(SRC)
else
	$(UV) pip install --reinstall --python $(VENV)/bin/python $(SRC)
endif
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
	@echo "uv not found. Install it first:"
	@echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
	@exit 1
endif
	@test -d $(VENV) || $(UV) venv $(VENV) --python $(PYTHON) --seed

# ---- pip install into venv ----
pip-install: venv
	$(UV) pip install --python $(VENV)/bin/python $(SRC)

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

.PHONY: install update uninstall venv pip-install link-add clean-path
