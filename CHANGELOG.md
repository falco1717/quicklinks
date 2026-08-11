# Changelog

## Unreleased

- Removed the Saltbox integration documentation and the external deployment role it referenced. Unattended provisioning through `ADMIN_USERNAME` and `ADMIN_PASSWORD` is unchanged and still supported; only the Saltbox-specific packaging and instructions are gone.
- Added `docs/running-on-windows.md`: running QuickLinks natively on Windows as a boot-time scheduled task, with a wrapper script that keeps the session secret out of machine-wide environment variables, data-directory ACLs, firewall and reverse-proxy notes, and NSSM as an alternative.
- Added `docs/running-without-docker.md`: a tested step-by-step guide for running QuickLinks under systemd with no packages installed, including a hardened unit file, reverse proxy configuration, and backup and upgrade steps.
- The error raised when Active Directory is enabled without `ldap3` installed no longer claims the application is running in a container.
- Corrected the security note about the first-run page: it stays closed once *either* directory is enabled, not Active Directory alone.

## 2026.08.11.003

### Changed

- Reworked the Authentication tab. Active Directory and Microsoft Entra ID no longer render their full forms side by side; each is now a compact row showing its status and a one-line summary, with a Configure button opening the settings in a dialog. Both providers' state stays visible at a glance, which a show-one-at-a-time toggle would have hidden. The tab is about 4x shorter as a result.
- Tightened the Authentication tab's vertical rhythm. `.tool-section` is a grid whose rows stretched to fill the column, so beside a taller neighbour the heading's row grew to 90px for 24px of text and the help text's row to 81px for 17px, spreading the content apart. Rows are now packed to the top, taking the section's content block from 431px to 207px.
- The dialogs are native `<dialog>` elements, so Escape and focus trapping come from the browser rather than from script. Closing is wired to a plain button: a `<form method="dialog">` inside the provider forms would have nested one form in another and silently truncated the outer one.

### Fixed

- `tests/test_http.py` no longer writes to `data/links.db` and `data/.session_secret` inside the application directory. That path is the default `DATA_DIR`, so running the suite while a local server was up overwrote and then deleted the developer's own database. The fixtures use dedicated canary names, and the helper that creates them now refuses to touch a path that already exists.

## 2026.08.11.002

### Added

- Microsoft Entra ID authentication, using the OIDC authorization code flow with PKCE. Sign-in happens on Microsoft's page, so multi-factor authentication and Conditional Access apply. The older username/password grant was not used: it does not support either, and many tenants block it.
- A "Sign in with Microsoft" button on the admin sign-in page, shown only when Entra is enabled and fully configured. Local accounts and on-premises Active Directory continue to work alongside it, so a local break-glass login remains available.
- An Entra section under Admin → Authentication for the tenant ID, client ID, client secret, redirect URI, and the allowed users, group object IDs, and app role values. Any one match grants access.
- Sign-in failures now appear on the page itself rather than as a browser dialog, from a fixed set of messages the page owns; the reason code in the URL is never rendered directly.

### Changed

- Editing which users, groups, or roles a directory authorizes now signs out that directory's existing sessions, so removing someone takes effect immediately instead of at the end of their session. Local administrator sessions are unaffected, so configuring a directory does not sign out the administrator doing it.
- The last enabled local administrator may now be removed once *either* Active Directory or Entra ID is enabled, and the first-run setup page stays closed in both cases.
- `GET /api/auth-config` reports whether an Entra client secret is stored, never the secret itself.

### Fixed

- The local administrator form claimed a 10 character minimum where the server enforces 7.

## 2026.08.11.001

Security release. Upgrading is strongly recommended for any reachable deployment.

### Fixed - critical

- Restricted static file serving to an allowlist. The data directory sits inside the application directory in the shipped container, so `GET /data/.session_secret` and `GET /data/links.db` previously returned the session-signing secret and the administrator password hashes to any unauthenticated caller. The leaked secret was sufficient to mint a valid admin cookie, because sessions were signed from the expiry timestamp alone. Application source and directory listings were exposed the same way.
- Restored path containment in the static handler. The `translate_path` override replaced the base handler's sanitizer without reimplementing it, so a request for `/../file` escaped the application directory. Resolved paths are now checked for containment, and unquoting — dropped by the same override, which broke every asset name containing an encoded character — is back.

### Fixed - high

- Session cookies now name the account and its authentication source, and are checked against a per-account revocation epoch. Logout, password change, rename, disable, and delete all invalidate tokens already issued; previously logout only cleared the cookie and the token stayed valid for its full 8 hours.
- Added login rate limiting: a per-username lockout and a looser per-address lockout, both answering `429` with `Retry-After`. Unlimited guessing against a 7-character minimum was also a CPU exhaustion vector at 310,000 KDF iterations per attempt.
- Oversized request bodies are rejected from the `Content-Length` header before being read. The 5 MB CSV limit was enforced only after the entire body was already in memory.
- Closed a privilege escalation in the first-run page. Deleting the last local administrator is permitted once Active Directory is enabled, but after a restart the unauthenticated `POST /api/setup` endpoint became available again and would create a new administrator. Setup now also requires that Active Directory be disabled.

### Fixed - medium

- Removed the login timing oracle. An unknown username skipped the KDF entirely and returned roughly 3.5x faster than a wrong password for a real account. Unknown usernames are now verified against an unusable hash so both paths cost the same.
- The session cookie carries `Secure` when the request arrives over HTTPS, configurable with `SESSION_COOKIE_SECURE`.
- `sqlite3.OperationalError` is handled and answered as `503` instead of escaping as a connection reset. Enabled WAL journalling and a busy timeout, which is what made "database is locked" reachable across threads in the first place.
- Active Directory failures are logged. Every bind error, DNS failure, and TLS problem was previously swallowed and reported to the user as "Invalid username or password".
- The location foreign key is now enforced. It was declared but inert because `PRAGMA foreign_keys` was never enabled. Existing databases are migrated to add `ON UPDATE CASCADE` and `ON DELETE CASCADE`; any link referencing a missing location has its code cleared and logged for reassignment.
- Unauthenticated `POST` bodies are drained before the `401` is sent, so the error response is not lost to a connection reset.
- Added `object-src 'none'` and `form-action 'self'` to the content security policy, and `Cache-Control: no-store` to API responses. The `Server` header no longer reports the release or the Python version.

### Fixed - low

- Branding no longer resets to defaults when only the uploaded logo file is missing; the company name and titles survive.
- Invalid `page_type` values are rejected instead of silently becoming `general`, and general links no longer retain a stale `location_code`.
- The release is read from `VERSION` instead of being duplicated in source, and the HTML cache-busting query strings are gone in favour of conditional requests.
- Replaced the mixed connection handling with a single context manager that always commits or rolls back and always closes.
- `setup_required()` no longer queries the database on every homepage request.
- Removed roughly 65 lines of seeding code that could not run: the location, link, and vhost source lists it read from were all empty.

### Added

- `tests/test_http.py` — 40 request-level tests. The suite previously exercised only first-run bootstrapping, which is why none of the above was caught: every critical and high finding here is invisible from inside the module.
- `SESSION_COOKIE_SECURE`, `TRUST_PROXY`, and `LOG_LEVEL` settings, plus a security section in the README.

## 2026.08.10.001

- Added a one-time first-launch page for creating the initial administrator when environment credentials are blank.
- Removed the built-in default administrator username, password, and session secret.
- Added persistent automatic session-secret generation for zero-configuration launches.
- Preserved unattended provisioning when both `ADMIN_USERNAME` and `ADMIN_PASSWORD` are supplied.
- Added Docker, Compose, automated tests, CI, and project documentation.
