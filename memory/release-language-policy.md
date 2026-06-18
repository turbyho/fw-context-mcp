---
name: release-language-policy
description: Release notes, tag messages, commit messages, and public-facing metadata must always be in English — no emoji, no AI footers
metadata:
  type: feedback
---

# Release language & style policy

All release-related text must be in **English** and follow strict formatting:

## Language
- Git commit messages
- Git tag annotations
- GitHub Release titles and notes
- `pyproject.toml` / `server.json` metadata
- PR descriptions

Czech is fine for internal docs (MEMORY.md, code comments, test names), but
anything that appears on GitHub (tags, releases, commit history visible to the
public) must be in English.

## Style
- **No emoji** — no unicode symbols anywhere in commit messages, tag
  annotations, or release notes
- **No AI footer** — no "Co-Authored-By: Claude", no "Generated with Claude
  Code", no AI attribution of any kind
- **Conventional Commits** — every commit message starts with a type prefix
  (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`, `perf:`,
  `ci:`, `build:`)

**Why:** The project is public on GitHub. English is the standard language
for open-source releases. Emojis and AI footers are informal and
unprofessional in a public-facing project.
**How to apply:** When creating tags, releases, or commits, write the
descriptions in English, use Conventional Commits, and strip all emoji
and AI attribution. See [[release-workflow]] for the full procedure
and `.claude/skills/release.md` for the authoritative spec.
