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

Do not commit `.env`. Keep `data/` persistent and back it up; it contains the SQLite database, generated session secret, and uploaded branding.

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
