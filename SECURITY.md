# Security

## Reporting a vulnerability

Open a [private security advisory](https://github.com/falco1717/quicklinks/security/advisories/new)
rather than a public issue. Please include the version from `/api/product` and
enough detail to reproduce.

## What this application is

An intranet link portal. It holds no personal data beyond administrator
usernames, and its database is a list of links, locations, and departments.
The assets worth protecting are therefore:

1. **The administrator session.** An administrator can rewrite every link the
   organisation navigates by, which makes the admin session a phishing primitive.
2. **The directory credentials that pass through sign-in.** An Active Directory
   password typed into this application must not be interceptable.
3. **The Entra ID client secret**, which is stored in the database and never
   returned by the API.

The portal itself is designed to be readable without signing in, so
unauthenticated read access to `/api/catalog` for public departments is a
feature and not a finding. `require_login` turns it off.

## Posture

| Area | Where |
| --- | --- |
| Passwords | PBKDF2-SHA256, 310,000 iterations, per-account salt, constant-time compare. An unknown username still runs the KDF so timing does not disclose which accounts exist. |
| Sessions | HMAC-SHA256 over version, source, identity, and both timestamps. `HttpOnly`, `SameSite=Lax`, `Secure` when the request arrived over HTTPS. Revocable per account and per directory-config change. |
| Cross-site writes | `SameSite=Lax` plus an independent `Origin` / `Sec-Fetch-Site` check on every POST and DELETE. |
| Static files | An allowlist of seven paths plus `assets/`, each resolved and confined below the application directory and excluded from the data directory. |
| Directory sign-in | LDAPS with certificate verification on by default. Cleartext binds are refused unless verification is explicitly turned off. |
| Entra ID | OIDC authorization code flow with PKCE. Tenant, issuer, audience, nonce, and validity window are all checked. |
| Login throttling | Per-username (5 in 5 min, 15 min lockout) and per-IP (30 in 5 min). |
| Headers | `Content-Security-Policy` with no `unsafe-inline`, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Cache-Control: no-store` on API responses. |
| Uploads | Logos are identified by their own magic bytes; the type the browser declared is ignored. |
| Container | Runs as uid 1000 with no capabilities and a read-only root filesystem. |

## Checks that run on every push

`.github/workflows/security.yml`, and all of it fails the build:

- **ruff** with the `S` (flake8-bandit) rules — hardcoded credentials, SQL built
  by string formatting, unsafe URL handling. Configured in `ruff.toml`, where
  every suppression carries its reason.
- **bandit** over the application, configured in `bandit.yaml`.
- **pip-audit** against `requirements.txt`, also weekly on a schedule, because
  advisories appear without anyone touching the code.
- **gitleaks** over the full history with `.gitleaks.toml`, which extends the
  default rules with a generic "literal assigned to a credential-shaped name"
  rule. Test fixtures generate their credentials at run time so that rule has
  nothing to match.

`.github/workflows/test.yml` also starts the built image unprivileged, read-only
and with no capabilities, then creates an administrator through it — so a
container that cannot write its own database fails CI rather than production.

## Known limitations, accepted deliberately

**The Entra ID token signature is not verified.** The ID token is read off a TLS
connection this process opened to `login.microsoftonline.com` itself, which
OIDC Core 3.1.3.7 permits in place of signature validation for the
authorization code flow. This holds only because no token is ever accepted from
the browser. If that ever changes, JWKS verification becomes mandatory.

**`http.server` is not hardened for direct internet exposure.** Put a reverse
proxy in front of it. Both deployment guides do.

**The base image tracks `python:3.13-slim` rather than a digest.** A digest is
more reproducible, and scanners prefer it, but nothing here automatically bumps
a pinned digest — so pinning would freeze whatever base CVEs existed on the day
it was pinned. A floating tag means every release build picks up current base
patches. Revisit if automated base-image updates are added.

**GitHub Actions are pinned by major version tag, not commit SHA.** SHA pinning
defends against a tag being moved; it also stops security updates to those
actions from arriving. Same trade-off as above.

**The password minimum is seven characters.** NIST SP 800-63B suggests eight.
Raising it would reject an existing seven-character `ADMIN_PASSWORD` at startup
and stop a running deployment, so it is left where it is; the intended defence
against weak passwords here is the lockout, not the length rule.

**`ADMIN_USERNAME` / `ADMIN_PASSWORD` provisioning passes a password through the
environment**, where `docker inspect` can read it. It exists so an install can
be automated. Leave both blank and use the one-time setup page instead, which is
the default.

**`TRUST_PROXY=1` makes the application believe `X-Forwarded-For`.** Set it only
where a proxy you control overwrites that header, or a client can forge its
address and evade per-IP throttling.
