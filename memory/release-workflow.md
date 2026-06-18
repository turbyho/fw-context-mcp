---
name: release-workflow
description: Release procedure — bump, test, commit, tag, push to both remotes, GitHub Release, build package, PyPI upload
metadata:
  type: project
---

# Release workflow

All release notes, tag messages, and commit messages must be in English.
No emoji. No AI attribution footers. See [[release-language-policy]].

Authoritative spec: `.claude/skills/release.md` — invoke with `/release`.

## Procedure — strict order

```bash
# 1. bump version
python3 bump.py <new-version>

# 2. run tests
python3 -m pytest tests/ -x -q

# 3. commit (Conventional Commits, EN, no footer)
git add -A
git commit -m "<type>: <short summary>

<description>

Files:
  path/to/file — <what changed>
  ..."

# 4. push commits — both remotes
git push origin main && git push github main

# 5. tag (EN, no emoji)
git tag -a v<version> -m "v<version>: <brief description in EN>"
git push origin v<version> && git push github v<version>

# 6. GitHub Release (EN, no emoji, no AI footer)
gh release create v<version> \
  --repo turbyho/fw-context-mcp \
  --title "v<version> — <short title>" \
  --notes "<markdown — see release notes format in release.md>"

# 7. build package (use explicit pyenv Python 3.11)
rm -rf dist
~/.pyenv/versions/3.11.8/bin/python3 -m build

# 8. PyPI upload
~/.pyenv/versions/3.11.8/bin/python3 -m twine upload dist/*
```

## Python note

Always use the explicit pyenv Python path (`~/.pyenv/versions/3.11.8/bin/python3`),
not the shim `python3` from PATH. The shim may resolve to a different pyenv
version (e.g. 3.14.5 global) that lacks `build` and `twine`.

## Remotes

- `origin` — `git@git.montyho.com:turbyho/fw-context-mcp.git` (private)
- `github` — `git@github.com:turbyho/fw-context-mcp.git` (public)

Both must receive every push and every tag.

**Why:** Standardized release procedure — bump, test, commit, tag, push
both remotes, GitHub Release, build, PyPI upload. Every step mandatory.
**How to apply:** Follow the 8 steps in order. Use `/release` skill for
enforcement of format and language rules.
