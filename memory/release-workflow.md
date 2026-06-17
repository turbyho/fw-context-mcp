---
name: release-workflow
description: Release procedure — bump, tag, push to both remotes, create GitHub Release
metadata:
  type: project
---

# Release workflow

**All release notes and tag messages must be in English.**

## Bump version

```bash
python3 bump.py <new-version>
# e.g. python3 bump.py 0.2.1
```

Updates `pyproject.toml` and `server.json`.

## Create tag (English message)

```bash
git tag -a v<version> <commit> -m "v<version>: <brief description in EN>"
```

## Create GitHub Release

```bash
gh release create v<version> --title "v<version> — <short title in EN>" --notes "<markdown in EN>"
```

## Full procedure

```bash
# 1. bump version
python3 bump.py <new-version>

# 2. run tests
python3 -m pytest tests/ -x -q
python3 tests/test_comprehensive_quality.py

# 3. commit
git add -A
git commit -m "<type>: <description in EN>"

# 4. create tag (EN message)
git tag -a v<version> -m "v<version>: <brief description in EN>"

# 5. push commits and tags to both remotes
git push origin main && git push github main
git push origin v<version> && git push github v<version>

# 6. GitHub Release (EN notes)
gh release create v<version> --title "v<version> — <title>" --notes "<markdown>"
```

## Remotes

- `origin` — `git@git.montyho.com:turbyho/fw-context-mcp.git` (private)
- `github` — `git@github.com:turbyho/fw-context-mcp.git` (public)

**Why:** Standardized release procedure — always bump version before commit, push to both remotes, create proper GitHub Release.
**How to apply:** On every release: 1) bump, 2) test, 3) commit, 4) tag (EN), 5) push origin + github, 6) gh release create (EN).
