# Changelog

## 2026.08.10.001

- Added a one-time first-launch page for creating the initial administrator when environment credentials are blank.
- Removed the built-in default administrator username, password, and session secret.
- Added persistent automatic session-secret generation for zero-configuration launches.
- Preserved unattended provisioning when both `ADMIN_USERNAME` and `ADMIN_PASSWORD` are supplied, including Saltbox installs.
- Added Docker, Compose, automated tests, CI, and project documentation.
