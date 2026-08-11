# QuickLinks

QuickLinks is a clean, self-hosted link portal with a browser-based admin center. It supports custom branding, locations, grouped links, CSV import/export, local administrators, and optional Active Directory or Microsoft Entra ID authentication.

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

## Running without Docker

QuickLinks is a standard-library Python application, so it runs directly under systemd with no packages installed. See [docs/running-without-docker.md](docs/running-without-docker.md).

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
| `LOG_LEVEL` | `INFO` | Logging verbosity. Failed logins, throttled logins, and directory errors are logged here. |

Do not commit `.env`. Keep `data/` persistent and back it up; it contains the SQLite database, generated session secret, and uploaded branding.

## Microsoft Entra ID

QuickLinks can authenticate administrators against Microsoft Entra ID using the OIDC authorization code flow with PKCE. Sign-in happens on Microsoft's own page, so multi-factor authentication and Conditional Access policies apply. This sits alongside local accounts and on-premises Active Directory — enabling it does not disable either, and a local account remains available as a break-glass login.

Register an application in the Entra portal first:

1. **Entra ID → App registrations → New registration.** Give it a name and leave it single-tenant.
2. Add a **Web** platform redirect URI of `https://your-quicklinks-host/api/auth/entra/callback`. It must be the public address users reach, which is the reverse proxy address rather than the container port.
3. **Certificates & secrets → New client secret.** Copy the value; it is shown only once.
4. Copy the **Directory (tenant) ID** and **Application (client) ID** from the app's Overview page.
5. To authorize by group, set **Token configuration → Add groups claim → Security groups** so the ID token carries group object IDs. To authorize by app role instead, define roles under **App roles** and assign them under **Enterprise applications → Users and groups**.

Then open **Admin → Authentication → Microsoft Entra ID**, paste those values, and list the administrators:

| Field | Accepts |
| --- | --- |
| Admin users | User principal names, one per line. Matched against `preferred_username`, `upn`, `email`, and `unique_name`, case-insensitively. |
| Admin groups | Security group **object IDs** (GUIDs), one per line. Requires the groups claim above. |
| Admin app roles | App role **values**, one per line. |

Any one match grants access. Prefer app roles when your users belong to many groups: past roughly 200 groups Entra replaces the groups claim with a Graph pointer, and QuickLinks logs a warning and falls back to users and roles rather than calling Graph.

Changing any Entra setting signs out current Entra sessions, so removing someone from the list takes effect immediately. Local administrator sessions are unaffected.

The client secret is stored in the settings table of `links.db`. Keep the data directory backed up and readable only by the container, and rotate the secret in the Entra portal if the database is ever exposed. QuickLinks never returns the secret through the API, only whether one is stored.

## Security notes

- **Only the browser's own files are served from disk.** `index.html`, `admin.html`, the CSS and JS bundles, and `assets/` are reachable; everything else — application source, `VERSION`, the SQLite database, the generated session secret, uploaded branding — returns `404`. Uploaded logos are delivered through `/api/branding/logo`, never as a static file. Pointing `DATA_DIR` at a path outside the application directory is still recommended as a second layer.
- **Sessions are bound to an identity and can be revoked.** The signed cookie names the account and its authentication source. Logging out, changing a password, renaming an account, disabling it, or deleting it invalidates every token already issued to it. Disabling a directory, or editing which users, groups, or roles it authorizes, invalidates that directory's sessions.
- **Entra sign-ins are verified server-side.** State and nonce are carried in a short-lived signed cookie, the PKCE verifier never leaves the server, and the authorization code is redeemed over a TLS connection this process opens to `login.microsoftonline.com`. Tenant, audience, nonce, and validity window are all checked on the returned ID token. No token is ever accepted from the browser.
- **Logins are rate limited.** Five failures for one username triggers a 15 minute lockout; thirty failures from one address triggers a 5 minute lockout. Lockouts answer `429` with a `Retry-After` header. The per-address limit is deliberately loose so a shared proxy address cannot lock out every administrator — set `TRUST_PROXY=1` to get real client addresses.
- **The first-run page closes permanently.** It is available only while no login is possible at all. Once a local administrator exists *or* a directory (Active Directory or Microsoft Entra ID) is enabled, `POST /api/setup` is rejected, including after a restart with no local accounts left.
- **Link URLs may not use script-bearing schemes.** `javascript:`, `data:`, `vbscript:`, `blob:`, `about:`, and `filesystem:` are rejected on both the admin form and CSV import. Ordinary schemes, intranet `host:port` forms, and `smb:`/`rdp:`/`mailto:` links are unaffected.
- Request bodies are capped at 12 MB and rejected from the `Content-Length` header, before any of the body is read.

## Build and test

```bash
python -m unittest discover -s tests -v
docker build -t quicklinks:local .
```

## Releasing

Images are built and pushed by the `Publish` workflow, not from a workstation, so the published image always corresponds to a tagged commit and always had its tests run first.

One-time setup: add two repository secrets, `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access token with Read/Write scope, created under Docker Hub → Account settings → Personal access tokens).

To cut a release, bump `VERSION` using the `YYYY.MM.DD.NNN` scheme, add a changelog entry, then tag the commit with exactly the same value:

```bash
git tag "$(cat VERSION)" && git push origin "$(cat VERSION)"
```

The workflow refuses to publish when the tag and `VERSION` disagree. It pushes `jordanmfarmer/quicklinks:<version>` and moves `:latest`. A failed publish can be re-run from the Actions tab without retagging.

## Upgrades and backup

Pull the new image and recreate the container. Schema updates are applied at startup without replacing existing links or administrators.

```bash
docker compose pull
docker compose up -d
tar -czf quicklinks-data-backup.tgz data/
```

## License and attribution

QuickLinks was created by Jordan Farmer. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md). The required creator attribution must remain visible.
