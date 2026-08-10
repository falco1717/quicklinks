# Codex Handoff

- Current objective: Maintain the published QuickLinks source and image release `2026.08.10.001`.
- Current state: The public repository and Docker Hub release are published; the live Saltbox container remains unchanged by design.
- Next action: Monitor GitHub Actions and update the Saltbox deployment only if separately requested.
- Blockers: None known.
- Important decisions: Blank admin credentials enable one-time setup; a complete credential pair seeds an admin; partial pairs fail startup; generated session secrets persist in `DATA_DIR`.
- Branch/commit/sync: `main`; public repository `falco1717/quicklinks`; audited baseline `3fde7e62f29b6768a2d114851faea060bd41ef62` pushed.
- Validation complete: Five Python tests with resource warnings as errors; byte compilation; diff checks; insecure-default scan; isolated Docker build; first-run redirect/setup/replay rejection; environment-seeded login.
- Validation remaining: GitHub Actions result.
- Release: Docker Hub tags `2026.08.10.001` and `latest` both resolve to `sha256:9799242362aa9e07d0d782068e800ea07530e14387f62b17258d7efed9639704`.
