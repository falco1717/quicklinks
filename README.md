# QuickLinks

QuickLinks is a clean, self-hosted link portal with a browser-based admin center. It supports custom branding, locations, grouped links, CSV import/export, local administrators, and optional Active Directory authentication.

## Quick start

```bash
cp example.env .env
docker compose up -d
```

Open `http://localhost:6969/admin`. If `ADMIN_USERNAME` and `ADMIN_PASSWORD` are blank, QuickLinks displays a one-time page for creating the first administrator. The password must contain at least 7 characters.

To provision the initial administrator non-interactively, set both values before the first launch:

```dotenv
ADMIN_USERNAME=your-admin
ADMIN_PASSWORD=your-secure-password
```

Both variables must be supplied together. A partial pair stops startup with a clear error. Existing administrator accounts in `data/links.db` are preserved during upgrades.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ADMIN_USERNAME` | blank | Initial admin username; requires `ADMIN_PASSWORD`. |
| `ADMIN_PASSWORD` | blank | Initial admin password; minimum 7 characters. |
| `SESSION_SECRET` | generated | Session-signing secret. When blank, it is generated once in the data directory. |
| `DATA_DIR` | `/app/data` | Persistent database and uploaded branding location. |
| `HOST` | `0.0.0.0` | HTTP bind address. |
| `PORT` | `6969` | HTTP listening port inside the container. |
| `SESSION_COOKIE_SECURE` | `auto` | `auto` adds the `Secure` cookie flag when `X-Forwarded-Proto` is `https`. Force it with `1`, disable it with `0`. |
| `TRUST_PROXY` | blank | Set to `1` behind a reverse proxy so login throttling uses the client address from `X-Forwarded-For` instead of the proxy's. |
| `LOG_LEVEL` | `INFO` | Logging verbosity. Failed logins, throttled logins, and Active Directory errors are logged here. |

Do not commit `.env`. Keep `data/` persistent and back it up; it contains the SQLite database, generated session secret, and uploaded branding.

## Security notes

- **Only the browser's own files are served from disk.** `index.html`, `admin.html`, the CSS and JS bundles, and `assets/` are reachable; everything else — application source, `VERSION`, the SQLite database, the generated session secret, uploaded branding — returns `404`. Uploaded logos are delivered through `/api/branding/logo`, never as a static file. Pointing `DATA_DIR` at a path outside the application directory is still recommended as a second layer.
- **Sessions are bound to an identity and can be revoked.** The signed cookie names the account and its authentication source. Logging out, changing a password, renaming an account, disabling it, or deleting it invalidates every token already issued to it. Disabling Active Directory invalidates all AD sessions.
- **Logins are rate limited.** Five failures for one username triggers a 15 minute lockout; thirty failures from one address triggers a 5 minute lockout. Lockouts answer `429` with a `Retry-After` header. The per-address limit is deliberately loose so a shared proxy address cannot lock out every administrator — set `TRUST_PROXY=1` to get real client addresses.
- **The first-run page closes permanently.** It is available only while no login is possible at all. Once a local administrator exists *or* Active Directory is enabled, `POST /api/setup` is rejected, including after a restart with no local accounts left.
- **Link URLs may not use script-bearing schemes.** `javascript:`, `data:`, `vbscript:`, `blob:`, `about:`, and `filesystem:` are rejected on both the admin form and CSV import. Ordinary schemes, intranet `host:port` forms, and `smb:`/`rdp:`/`mailto:` links are unaffected.
- Request bodies are capped at 12 MB and rejected from the `Content-Length` header, before any of the body is read.

## Saltbox

The QuickLinks Saltbox Sandbox role supplies the Saltbox inventory username and password as the initial credentials, so unattended installs continue to work. After the role is accepted into the official Sandbox repository, install it with:

```bash
sb install sandbox-quicklinks
```

Until then, use the role from its [Sandbox pull request](https://github.com/saltyorg/Sandbox/pull/551).

## Build and test

```bash
python -m unittest discover -s tests -v
docker build -t quicklinks:local .
```

## Upgrades and backup

Pull the new image and recreate the container. Schema updates are applied at startup without replacing existing links or administrators.

```bash
docker compose pull
docker compose up -d
tar -czf quicklinks-data-backup.tgz data/
```

## License and attribution

QuickLinks was created by Jordan Farmer. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md). The required creator attribution must remain visible.
