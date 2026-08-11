# Changelog

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
- Preserved unattended provisioning when both `ADMIN_USERNAME` and `ADMIN_PASSWORD` are supplied, including Saltbox installs.
- Added Docker, Compose, automated tests, CI, and project documentation.
