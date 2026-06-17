---
name: release-language-policy
description: Release notes, tag messages, and public-facing metadata must always be in English
metadata:
  type: feedback
---

# Release language policy

All release-related text must be in **English**:

- Git tag annotations
- GitHub Release titles and notes
- Commit messages for version bumps and public changes
- `pyproject.toml` / `server.json` metadata

Czech is fine for internal docs (MEMORY.md, code comments, test names), but
anything that appears on GitHub (tags, releases, commit history visible to the
public) must be in English.

**Why:** The project is public on GitHub. English is the standard language for open-source releases.
**How to apply:** When creating tags, releases, or version-bump commits, write the descriptions in English. See [[release-workflow]] for the full procedure.
