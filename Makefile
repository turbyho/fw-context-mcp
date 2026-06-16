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
install: venv pip-install path-add
	@echo ""
	@echo "fw-context installed."
	@echo "Run 'source ~/.zshrc' or open a new terminal, then:"
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
	@echo "Removing ~/.fw-context ..."
	@rm -rf $(HOME)/.fw-context
	@echo "fw-context removed. The PATH entry in ~/.zshrc / ~/.bashrc was not removed — delete it manually if desired."

# ---- venv ----
venv:
ifeq ($(UV),)
	@echo "uv not found. Install it first:"
	@echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
	@exit 1
endif
	@test -d $(VENV) || $(UV) venv $(VENV) --python $(PYTHON)

# ---- pip install into venv ----
pip-install: venv
	$(UV) pip install --python $(VENV)/bin/python $(SRC)

# ---- install with watch extra ----
install-watch: install
	$(UV) pip install --python $(VENV)/bin/python "$(SRC)[watch]"

# ---- add to PATH ----
path-add:
	@for rc in $(HOME)/.zshrc $(HOME)/.bashrc; do \
		if [ -f "$$rc" ]; then \
			if ! grep -q '.fw-context/.venv/bin' "$$rc" 2>/dev/null; then \
				echo 'export PATH="$$HOME/.fw-context/.venv/bin:$$PATH"' >> "$$rc"; \
				echo "  Added $$rc"; \
			else \
				echo "  $$rc (already present)"; \
			fi; \
		fi; \
	done

.PHONY: install update uninstall venv pip-install path-add install-watch
