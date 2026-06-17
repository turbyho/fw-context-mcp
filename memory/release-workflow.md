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

## Vytvořit tag

```bash
git tag -a v<verze> <commit> -m "v<verze>: <strucny popis>"
git push origin v<verze> && git push github v<verze>
```

## Vytvořit GitHub Release

```bash
gh release create v<verze> --title "v<verze> — <titulek>" --notes "<markdown popis>"
```

## Kompletní postup

```bash
# 1. bump verze
python3 bump.py <nova-verze>

# 2. commit
git add -A
git commit -m "<typ>: <popis>"

# 3. tag
git tag -a v<verze> -m "v<verze>: <strucny popis>"

# 4. push commity a tagy na oba servery
git push origin main && git push github main
git push origin v<verze> && git push github v<verze>

# 5. GitHub Release
gh release create v<verze> --title "v<verze> — <titulek>" --notes "<markdown>"
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
