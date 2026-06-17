---
name: release-workflow
description: Postup pro vydání nové verze — bump, commit, push na oba servery
metadata:
  type: project
---

# Release workflow

## Bump verze

```bash
python3 bump.py <nova-verze>
# např. python3 bump.py 0.2.1
```

Updatuje `pyproject.toml` a `server.json`.

## Commit a push na oba servery

```bash
git add -A
git commit -m "fix: ..."
git push origin main && git push github main
```

**Remoty:**
- `origin` — `git@git.montyho.com:turbyho/fw-context-mcp.git` (privátní)
- `github` — `git@github.com:turbyho/fw-context-mcp.git` (veřejný)

## Před releasem ověřit

```bash
python3 -m pytest tests/ -x -q
python3 tests/test_comprehensive_quality.py
```

**Why:** Standardizovaný postup — vždy bump verzi před commitem, push na oba remotes.
**How to apply:** Při každém releasu: 1) bump verze, 2) pytest + comprehensive testy, 3) commit, 4) push origin + github.
