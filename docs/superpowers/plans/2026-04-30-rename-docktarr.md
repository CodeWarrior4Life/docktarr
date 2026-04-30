---
type: plan
project: docktarr
created: 2026-04-30T17:05:00-04:00
status: completed
---

> **Source of truth:** `D:\Vaults\Mainframe\02_Projects\Media Library\Plans\Doctarr Rename to Docktarr.md` (vault).
> This repo copy is a reference; the vault note is canonical and may be updated independently.

# Docktarr Rename Implementation Plan (repo copy)

For the full plan, see the vault note. The plan covers:

1. Branch creation + version bump (0.4.1 → 0.5.0)
2. `git mv src/doctarr src/docktarr`
3. Sweep Python imports across 45 .py files
4. Update `pyproject.toml` package name
5. Update Dockerfile + docker-compose.yml
6. Update `.github/workflows/ci.yml` GHCR image tags
7. Update default config path `/config/doctarr.yaml` → `/config/docktarr.yaml` + migration script
8. Update README, CLAUDE.md, CHANGELOG (add 0.5.0 entry)
9. Final test sweep + push branch
10. Open PR via API + merge after CI green
11. `gh repo rename` via API + update local origin
12. Wait for CI build of new GHCR image
13. Cutover Portainer stack 39: stop old container → `mv /share/Container/Data/config/doctarr docktarr` → redeploy with new image + service name + bind mount path
14. Archive old GHCR doctarr package (default: leave alone with last 0.4.1 image)
15. Update active vault references (memory file, active-work NEXT SESSION DIRECTIVE, etc.)

Pure mechanical rename. No behavior change. Tests must stay 153/153 green throughout.

Read the vault note for full task-by-task code/commands.
