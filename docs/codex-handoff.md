# Handoff

- Current objective: Maintain the published QuickLinks source and the `2026.08.11.003` image release.
- Current state: Public repository and Docker Hub release published. Releases are built and pushed by the `Publish` workflow when a tag matching `VERSION` is pushed; no image is ever built from a workstation.
- Next action: None outstanding.
- Blockers: None known.
- Important decisions: blank admin credentials enable one-time setup; a complete credential pair seeds an admin; partial pairs fail startup; generated session secrets persist in `DATA_DIR`. Static serving is an allowlist, so nothing outside the browser's own files is reachable over HTTP. Sessions name their account and authentication source and are checked against a revocation epoch. Entra ID uses the OIDC authorization code flow with PKCE and adds no dependencies.
- Branch/commit/sync: `main`, linear history; public repository `falco1717/quicklinks`.
- Validation: 71 tests (`python -m unittest discover -s tests`), covering first-run bootstrapping and the HTTP surface. CI runs the suite plus a Docker build on every push, and again before any publish.
- Release: Docker Hub tags `2026.08.11.003` and `latest` both resolve to `sha256:3a088cf620ab0e4cc3e2b9ddfdb0d4c8e01c6dd99d942aa72ba6b8a22b1d655d`.
