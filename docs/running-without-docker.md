# Running QuickLinks without Docker

Yes. QuickLinks is a single Python file on the standard library — `http.server` and `sqlite3`, no web framework — so a bare install needs **no packages at all** unless you want on-premises Active Directory login. There is nothing to compile and no build step.

This guide sets it up as a systemd service on Debian or Ubuntu. Adapt paths for other distributions. For Windows, see [running-on-windows.md](running-on-windows.md).

## Requirements

| | |
| --- | --- |
| Python | **3.9 or newer** (`pathlib.Path.is_relative_to` and `str.removeprefix` set that floor). Releases are built and tested on 3.13. |
| SQLite | Whatever ships with Python. 3.24+ is needed for upsert support, which every supported Python already has. |
| Packages | **None**, unless you enable Active Directory — see [Optional: Active Directory](#optional-active-directory). Microsoft Entra ID needs nothing extra. |
| Privileges | root for the install steps only. The service itself runs as an unprivileged user. |

## 1. Pick a free port

The default is `6969`. Check nothing already holds it:

```bash
ss -ltn | grep -w 6969 || echo "6969 is free"
```

If it is taken — for example because you already run QuickLinks in Docker on the same host — pick another and use it consistently below.

## 2. Create a service user

No shell, no home directory, no login:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin quicklinks
```

## 3. Install the application

Download a release rather than the default branch, so you know what you are running. Substitute the current version from the [releases page](https://github.com/falco1717/quicklinks/releases):

```bash
sudo mkdir -p /opt/quicklinks-app
curl -sSL https://github.com/falco1717/quicklinks/archive/refs/tags/2026.08.11.004.tar.gz \
  | sudo tar -xz -C /opt/quicklinks-app --strip-components=1
sudo chown -R root:root /opt/quicklinks-app
```

The application directory stays owned by root and read-only to the service. QuickLinks never writes to it — all state lives in the data directory.

## 4. Create the configuration

```bash
sudo mkdir -p /etc/quicklinks
sudo tee /etc/quicklinks/quicklinks.env >/dev/null <<EOF
HOST=127.0.0.1
PORT=6969
DATA_DIR=/var/lib/quicklinks
SESSION_SECRET=$(openssl rand -base64 48 | tr -d '=\n' | tr '+/' '-_')
LOG_LEVEL=INFO
SESSION_COOKIE_SECURE=auto
TRUST_PROXY=1
EOF
sudo chown root:quicklinks /etc/quicklinks/quicklinks.env
sudo chmod 640 /etc/quicklinks/quicklinks.env
```

Notes on those values:

- **`HOST=127.0.0.1`** binds to loopback only, so the service is reachable solely through the reverse proxy in step 7. Use `0.0.0.0` only if you intend to expose it directly.
- **`SESSION_SECRET`** is generated here so it is stable across restarts. Leave it out and QuickLinks generates one into the data directory instead; either is fine, but never share one between instances.
- **`TRUST_PROXY=1`** makes login rate limiting see real client addresses from `X-Forwarded-For`. Set it **only** when a proxy you control sets that header.
- To provision the first administrator without the setup page, add `ADMIN_USERNAME` and `ADMIN_PASSWORD`. Both or neither — a partial pair deliberately stops startup. Leave them out and QuickLinks shows a one-time create-administrator page on first visit.

Full variable reference is in the [README](../README.md#configuration).

## 5. Install the service

```bash
sudo tee /etc/systemd/system/quicklinks.service >/dev/null <<'EOF'
[Unit]
Description=QuickLinks link portal
Documentation=https://github.com/falco1717/quicklinks
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=quicklinks
Group=quicklinks
WorkingDirectory=/opt/quicklinks-app
ExecStart=/usr/bin/python3 /opt/quicklinks-app/server.py
EnvironmentFile=/etc/quicklinks/quicklinks.env
StateDirectory=quicklinks
StateDirectoryMode=0750
Restart=on-failure
RestartSec=5

# Roughly the container's posture: no capabilities, no privilege escalation,
# read-only filesystem apart from the state directory.
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectProc=invisible
RestrictAddressFamilies=AF_INET AF_INET6
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
SystemCallFilter=@system-service
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

sudo systemd-analyze verify /etc/systemd/system/quicklinks.service
sudo systemctl daemon-reload
sudo systemctl enable --now quicklinks
```

`StateDirectory=quicklinks` creates `/var/lib/quicklinks` owned by the service user on first start, which is why `DATA_DIR` points there. Because it is outside the application directory, the data directory is not even adjacent to anything served over HTTP.

If the service fails to start with a permission or syscall error, remove `SystemCallFilter` first and then `ProtectSystem` — those two are the most likely to interact badly with an unusual Python build. Everything else in that block is safe to keep.

## 6. Check it

```bash
systemctl status quicklinks --no-pager
journalctl -u quicklinks -n 20 --no-pager
curl -s http://127.0.0.1:6969/api/product
curl -s http://127.0.0.1:6969/api/session
```

`/api/product` returns the running version. `/api/session` reports `"setup_required": true` until the first administrator exists.

## 7. Put a reverse proxy in front

Do this rather than exposing the service directly. Python's `http.server` is not hardened for direct internet exposure, and the proxy is what terminates TLS. This is the same posture as the Docker deployment, which also sits behind a proxy.

Minimal nginx server block:

```nginx
server {
    listen 443 ssl;
    server_name links.example.com;

    # ssl_certificate / ssl_certificate_key here

    location / {
        proxy_pass http://127.0.0.1:6969;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`X-Forwarded-Proto` is what makes QuickLinks mark its session cookie `Secure`, and `X-Forwarded-For` is what `TRUST_PROXY=1` reads. Without them you lose the `Secure` flag and every client shares one rate-limit bucket.

If you use Microsoft Entra ID, the redirect URI registered in the Entra portal must be this proxy's public address followed by `/api/auth/entra/callback`, not the loopback port.

## Optional: Active Directory

Only on-premises AD needs packages. Entra ID uses nothing beyond the standard library.

Debian and Ubuntu mark the system Python as externally managed, so install into a virtual environment rather than fighting pip:

```bash
sudo apt install python3-venv
sudo python3 -m venv /opt/quicklinks-venv
sudo /opt/quicklinks-venv/bin/pip install -r /opt/quicklinks-app/requirements.txt
```

Then point the service at that interpreter:

```bash
sudo sed -i 's|ExecStart=/usr/bin/python3|ExecStart=/opt/quicklinks-venv/bin/python|' \
  /etc/systemd/system/quicklinks.service
sudo systemctl daemon-reload && sudo systemctl restart quicklinks
```

`ldap3` and `dnspython` are imported lazily, only when an AD login is attempted, so a missing virtual environment costs you nothing until you enable AD — at which point sign-in fails with a clear message instead of crashing.

## Upgrading

```bash
sudo systemctl stop quicklinks
sudo tar -czf /var/backups/quicklinks-$(date +%F).tgz -C /var/lib quicklinks
curl -sSL https://github.com/falco1717/quicklinks/archive/refs/tags/<new-version>.tar.gz \
  | sudo tar -xz -C /opt/quicklinks-app --strip-components=1
sudo systemctl start quicklinks
```

Schema migrations run at startup and preserve existing links, locations, and administrators. Back up the data directory first anyway — that tar is the whole recovery story.

## Backup and restore

Everything that matters is in `/var/lib/quicklinks`: the SQLite database, the generated session secret if you did not set one, and any uploaded logo.

```bash
# back up
sudo systemctl stop quicklinks
sudo tar -czf quicklinks-backup.tgz -C /var/lib quicklinks
sudo systemctl start quicklinks

# restore
sudo systemctl stop quicklinks
sudo tar -xzf quicklinks-backup.tgz -C /var/lib
sudo chown -R quicklinks:quicklinks /var/lib/quicklinks
sudo systemctl start quicklinks
```

Stopping first avoids copying a database mid-write. QuickLinks uses WAL journalling, so a hot copy of `links.db` alone can be inconsistent.

## Docker or bare metal?

Both run the same `server.py`; nothing is disabled either way.

- **Docker** gives you a pinned, tested image, a one-line upgrade, and the sandbox for free.
- **Bare metal** removes a moving part, uses less memory, and starts faster. You take on the Python version and the process supervision yourself.

Nothing about the bare install is a downgrade in capability — it is the same application with systemd instead of Docker doing the supervising.
