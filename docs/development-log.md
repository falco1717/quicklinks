# Development Log

Keep entries public-ready: completed work, decisions and rationale, useful failed approaches, validation, and durable lessons.

## 2026-08-10

- Recovered the source from the published `jordanmfarmer/quicklinks:2026.08.09.002` image so the repository matches the deployed application rather than an older local copy.
- Replaced fallback credentials with an atomic, one-time administrator setup flow. Environment provisioning remains available only when username and password are both set.
- Persisted an automatically generated session secret in the data volume when one is not supplied.
- An initial handler method name collided with the Python HTTP server lifecycle. Container smoke testing caught it; the method was renamed and the complete container matrix then passed.
- Verified five unit tests, clean byte compilation/diff checks, absence of prior insecure defaults, an isolated Docker build, browser-route redirect behavior, setup replay rejection, and environment-seeded login.
- Reviewed and fingerprint-allowlisted the Compose environment-variable pass-through false positive and the two known product-logo PNG files. Allowances are file-content-specific and invalidate automatically if those files change.
