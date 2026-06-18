# /release — Commit & Release Automation

Skill for creating commits and releases with strict conformance to project
conventions.  Invoked via `/release`.

## Rules — always enforced

- **English only** — every line of commit message, tag annotation, release
  notes, and GitHub-facing text must be in English
- **No emoji** — no unicode symbols (not in commit messages, not in release
  notes, not in PR descriptions)
- **No AI footer** — no "Co-Authored-By: Claude", no "Generated with Claude
  Code", no AI attribution of any kind
- **Conventional Commits** — type prefix mandatory: `feat:`, `fix:`, `chore:`,
  `docs:`, `refactor:`, `test:`, `perf:`, `ci:`, `build:`
- **Python explicitly** — always use `~/.pyenv/versions/3.11.8/bin/python3`
  for `build` and `twine`; the shim `python3` may resolve to a different
  pyenv version that lacks these packages
- **Both remotes always** — `origin` (git.montyho.com) and `github`
  (github.com/turbyho/fw-context-mcp) must receive every push and tag

## Commit message format

```
<type>: <short summary — imperative, lowercase, max 72 chars>

<Description — what changed and why. 1-3 paragraphs, wrap at 72 chars.>

Files:
  path/to/file — <what changed in this file>
  ...

  New files:
  path/to/new/file — <purpose>

Breaking changes: (omit this entire section if none)
  - <description of what breaks and how to migrate>
```

### Type selection

| Type | When |
|------|------|
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `chore` | Version bump, tooling, maintenance |
| `docs` | Documentation only |
| `refactor` | Code restructure, no behavior change |
| `test` | Tests only |
| `perf` | Performance improvement |
| `ci` | CI/CD changes |
| `build` | Build system changes |

## Release checklist — strict order

```
1. BUMP VERSION
   python3 bump.py <new-version>

2. TESTS
   python3 -m pytest tests/ -x -q

3. COMMIT (format above, no footer)
   git add -A
   git commit -m "<message>"

4. PUSH COMMITS — both remotes
   git push origin main && git push github main

5. TAG (EN, no emoji, no footer)
   git tag -a v<version> -m "v<version>: <brief description in EN>"
   git push origin v<version> && git push github v<version>

6. GITHUB RELEASE
   gh release create v<version> \
     --repo turbyho/fw-context-mcp \
     --title "v<version> — <short title>" \
     --notes "<release notes, format below>"

7. BUILD PACKAGE
   rm -rf dist
   ~/.pyenv/versions/3.11.8/bin/python3 -m build

8. PYPI UPLOAD
   ~/.pyenv/versions/3.11.8/bin/python3 -m twine upload dist/*
```

## Release notes format

```markdown
## Overview
<One paragraph — what this release delivers, who should upgrade.>

## Changes

### Features
- **<name>** — <description>

### Fixes
- **<name>** — <description>

| File | Change |
|------|--------|
| `path/to/file` | <description> |

## Breaking changes
<"None." or describe what breaks and how to migrate>
```

### Release notes rules
- Title is `v<version> — <short title>` (no emoji, no markdown in the `--title` argument)
- Overview section is mandatory
- If no features, omit the `### Features` subsection; same for fixes
- Files table covers every changed file
- Breaking changes section is mandatory (write "None." if none)

## Pre-commit checks

Before committing for release, verify:
- [ ] Version bumped in `pyproject.toml` (and `server.json` if applicable)
- [ ] All tests pass
- [ ] No debugging artifacts, commented-out code, or temporary files
- [ ] `README.md` and `docs/` are up to date with the changes
- [ ] Commit message is EN, has type prefix, has `Files:` section
