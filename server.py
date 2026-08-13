import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import sqlite3
import time
import mimetypes
import re
import ssl
import urllib.error
import urllib.request
from contextlib import contextmanager
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", APP_DIR / "data"))
DB_PATH = DATA_DIR / "links.db"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip()
SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip()
SESSION_COOKIE = "quicklinks_admin_session"
SESSION_MAX_AGE = 8 * 60 * 60
SESSION_VERSION = "2"
COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "auto").strip().lower()
TRUST_PROXY = os.environ.get("TRUST_PROXY", "").strip().lower() in ("1", "true", "yes")

LOGGER = logging.getLogger("quicklinks")


def read_version():
    try:
        return (APP_DIR / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


APP_VERSION = read_version()
PRODUCT_NOTICE = {
    "name": "QuickLinks",
    "version": APP_VERSION,
    "creator": "Jordan Farmer",
    "notice": "QuickLinks · Created by Jordan Farmer",
    "header_notice": "QuickLinks - Created by Jordan Farmer",
    "copyright": "Copyright 2026 Jordan Farmer. All rights reserved.",
}
DEFAULT_BRANDING = {
    "company_name": "QuickLinks",
    "department_title": "Link Portal",
    "admin_title": "Admin Center",
    "logo_url": "/assets/quicklinks-logo.png",
}
ALLOWED_LOGO_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
PASSWORD_ITERATIONS = 310_000
CSV_FIELDS = [
    "record_type", "name", "code", "page_type", "location_code", "link_type",
    "url", "description", "group_name", "cluster", "sort_order", "enabled",
]

# Only these paths are served from disk. Everything else -- source code, the
# database, the session secret, uploaded branding -- is unreachable over HTTP.
STATIC_FILES = {
    "/index.html",
    "/admin.html",
    "/app.js",
    "/admin.js",
    "/styles.css",
    "/admin.css",
    "/favicon.ico",
}
STATIC_DIRS = ("assets",)
UNSERVABLE = "__quicklinks_forbidden__"

# Schemes that execute script in the browser when placed in an href. Everything
# else (http, https, smb, rdp, mailto, host:port, relative paths) stays usable.
BLOCKED_URL_SCHEMES = {"javascript", "data", "vbscript", "blob", "about", "filesystem"}

MAX_REQUEST_BODY = 12 * 1024 * 1024
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_LOGO_BYTES = 5 * 1024 * 1024
MAX_TOKEN_RESPONSE = 256 * 1024

# Microsoft Entra ID, via the OIDC authorization code flow with PKCE. The
# redirect flow is what supports MFA and Conditional Access; the older
# username/password grant does not, and many tenants block it outright.
ENTRA_AUTHORITY = "https://login.microsoftonline.com"
ENTRA_SCOPE = "openid profile email"
ENTRA_FLOW_COOKIE = "quicklinks_entra_flow"
ENTRA_FLOW_MAX_AGE = 600
ENTRA_CLOCK_SKEW = 120
ENTRA_SETTING_KEYS = [
    "entra_enabled", "entra_tenant_id", "entra_client_id", "entra_client_secret",
    "entra_redirect_uri", "entra_admin_users", "entra_admin_groups", "entra_admin_roles",
]
GUID_PATTERN = re.compile(r"\A[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")
AUTH_SOURCES = ("local", "ad", "entra")

# Per-username lockout is strict because it protects one account. The per-IP
# limit is loose so that a shared reverse-proxy address cannot lock out every
# administrator at once.
LOGIN_LIMITS = {
    "user": {"max_failures": 5, "window": 300, "lockout": 900},
    "ip": {"max_failures": 30, "window": 300, "lockout": 300},
}
THROTTLE_MAX_KEYS = 4096

LINKS_TABLE_SQL = """
    CREATE TABLE {if_not_exists}links (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      page_type TEXT NOT NULL CHECK(page_type IN ('general','location')),
      location_code TEXT,
      link_type TEXT NOT NULL DEFAULT 'standard',
      name TEXT NOT NULL,
      url TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      group_name TEXT NOT NULL DEFAULT 'General',
      cluster TEXT NOT NULL DEFAULT '',
      sort_order INTEGER NOT NULL DEFAULT 0,
      enabled INTEGER NOT NULL DEFAULT 1,
      department_id INTEGER REFERENCES departments(id),
      FOREIGN KEY(location_code) REFERENCES locations(code)
        ON UPDATE CASCADE ON DELETE CASCADE
    )
"""

LINK_COLUMNS = (
    "id, page_type, location_code, link_type, name, url, description, "
    "group_name, cluster, sort_order, enabled"
)

_throttle = {}
_throttle_lock = Lock()
_setup_complete = False


def reset_runtime_state():
    """Clear process-local caches. Used by the test suite between cases."""
    global _setup_complete
    with _throttle_lock:
        _throttle.clear()
    _setup_complete = False


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db():
    """Open a connection, commit or roll back, and always close it."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def process_identity():
    """Describe the running process well enough to fix a permission problem."""
    try:
        import getpass
        name = getpass.getuser()
    except Exception:
        name = "unknown"
    if hasattr(os, "geteuid"):
        return f"uid {os.geteuid()}, gid {os.getegid()} ({name})"
    return name


def ensure_data_directory():
    """Confirm DATA_DIR exists and is writable, and say so plainly if not.

    Without this the first symptom of an unwritable data directory is
    `sqlite3.OperationalError: attempt to write a readonly database` raised from
    a PRAGMA, which names neither the directory nor the user and sends people
    looking for a database problem they do not have.
    """
    advice = (
        f"Set DATA_DIR to a writable path, or grant write access to {process_identity()}. "
        "In a container, a bind-mounted directory must be writable by the container "
        "user: note that dropping all capabilities removes root's ability to bypass "
        "file permissions, so a root process is still subject to the directory mode."
    )
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimeError(f"Cannot create the data directory {DATA_DIR}: {error}. {advice}") from error
    if not DATA_DIR.is_dir():
        raise RuntimeError(f"The data directory {DATA_DIR} exists but is not a directory. {advice}")
    probe = DATA_DIR / ".quicklinks-write-probe"
    try:
        probe.write_bytes(b"")
    except OSError as error:
        raise RuntimeError(
            f"The data directory {DATA_DIR} is not writable: {error}. {advice}"
        ) from error
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    if DB_PATH.exists() and not os.access(DB_PATH, os.W_OK):
        raise RuntimeError(f"The database file {DB_PATH} is not writable. {advice}")


def ensure_database():
    global SESSION_SECRET
    ensure_data_directory()
    if bool(ADMIN_USERNAME) != bool(ADMIN_PASSWORD):
        raise RuntimeError("ADMIN_USERNAME and ADMIN_PASSWORD must be provided together or both left blank.")
    if not SESSION_SECRET:
        secret_path = DATA_DIR / ".session_secret"
        if secret_path.exists():
            SESSION_SECRET = secret_path.read_text(encoding="utf-8").strip()
        else:
            SESSION_SECRET = secrets.token_urlsafe(48)
            secret_path.write_text(SESSION_SECRET, encoding="utf-8")
            try:
                secret_path.chmod(0o600)
            except OSError:
                pass
    # Schema work runs on a connection with foreign keys left off so that table
    # rebuilds are possible; PRAGMA foreign_keys is also a no-op inside a
    # transaction, which the rebuild needs.
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 15000")
        except sqlite3.OperationalError as error:
            # The preflight above catches the usual causes; anything left is
            # worth reporting against the database path rather than as a bare
            # PRAGMA failure.
            raise RuntimeError(
                f"Could not open the database at {DB_PATH} for writing: {error}. "
                f"Check that {DATA_DIR} and its contents are writable by {process_identity()}."
            ) from error
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS locations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              code TEXT NOT NULL UNIQUE,
              sort_order INTEGER NOT NULL DEFAULT 0,
              enabled INTEGER NOT NULL DEFAULT 1,
              department_id INTEGER REFERENCES departments(id)
            );
            CREATE TABLE IF NOT EXISTS admin_users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL UNIQUE COLLATE NOCASE,
              password_hash TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at INTEGER NOT NULL,
              is_admin INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS session_epochs (
              source TEXT NOT NULL,
              username TEXT NOT NULL COLLATE NOCASE,
              epoch INTEGER NOT NULL,
              PRIMARY KEY (source, username)
            );
            CREATE TABLE IF NOT EXISTS departments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
              description TEXT NOT NULL DEFAULT '',
              public INTEGER NOT NULL DEFAULT 0,
              sort_order INTEGER NOT NULL DEFAULT 0,
              enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS user_departments (
              user_id INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
              department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
              PRIMARY KEY (user_id, department_id)
            );
            -- Maps an AD group, Entra group object ID, or Entra app role to a
            -- department. Resolved at login, because directory group membership
            -- cannot be re-checked without binding again.
            CREATE TABLE IF NOT EXISTS directory_departments (
              source TEXT NOT NULL CHECK(source IN ('ad','entra')),
              group_ref TEXT NOT NULL COLLATE NOCASE,
              department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
              PRIMARY KEY (source, group_ref, department_id)
            );
            -- What a directory login resolved to, so later requests do not need
            -- to re-bind. Refreshed on every successful directory sign-in.
            CREATE TABLE IF NOT EXISTS directory_users (
              source TEXT NOT NULL,
              username TEXT NOT NULL COLLATE NOCASE,
              is_admin INTEGER NOT NULL DEFAULT 0,
              last_login INTEGER NOT NULL,
              PRIMARY KEY (source, username)
            );
            CREATE TABLE IF NOT EXISTS directory_memberships (
              source TEXT NOT NULL,
              username TEXT NOT NULL COLLATE NOCASE,
              department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
              PRIMARY KEY (source, username, department_id)
            );
            """
        )
        conn.execute(LINKS_TABLE_SQL.format(if_not_exists="IF NOT EXISTS "))
        migrate_links_cascade(conn)
        migrate_departments(conn)
        seed_initial_admin(conn)
        conn.commit()
    finally:
        conn.close()


def column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate_departments(conn):
    """Introduce departments without changing how an existing install behaves.

    Every existing location and link moves into one public department, so an
    upgrade with no further configuration serves exactly the same catalog to
    exactly the same anonymous visitors. Existing administrators stay
    administrators, which is what `is_admin DEFAULT 1` gives the rows already
    in the table.
    """
    if conn.execute("SELECT 1 FROM settings WHERE key = 'schema_departments'").fetchone():
        return

    if "is_admin" not in column_names(conn, "admin_users"):
        conn.execute("ALTER TABLE admin_users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 1")
    for table in ("locations", "links"):
        if "department_id" not in column_names(conn, table):
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN department_id INTEGER REFERENCES departments(id)"
            )

    # A department must always exist, on a fresh install as much as an upgrade:
    # every location and link belongs to exactly one, so with none defined there
    # would be nowhere to put anything and the portal would serve an empty
    # catalog.
    department_id = ensure_default_department(conn)
    orphans = conn.execute(
        "SELECT COUNT(*) FROM locations WHERE department_id IS NULL"
    ).fetchone()[0] + conn.execute(
        "SELECT COUNT(*) FROM links WHERE department_id IS NULL"
    ).fetchone()[0]
    if orphans:
        conn.execute("UPDATE locations SET department_id = ? WHERE department_id IS NULL", (department_id,))
        conn.execute("UPDATE links SET department_id = ? WHERE department_id IS NULL", (department_id,))
        LOGGER.info(
            "Moved %s existing record(s) into the default department. It is public, so "
            "anonymous visitors see what they saw before.",
            orphans,
        )

    conn.execute(
        "INSERT INTO settings(key, value) VALUES('schema_departments', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )


def ensure_default_department(conn):
    """Return the default department's id, creating it when none exist yet.

    Public by default so that a new install, and an upgrade of one that was
    serving anonymous visitors, both behave the way they did before.
    """
    existing = conn.execute(
        "SELECT id FROM departments ORDER BY sort_order, id LIMIT 1"
    ).fetchone()
    if existing:
        return existing["id"]
    return conn.execute(
        "INSERT INTO departments(name, slug, description, public, sort_order) "
        "VALUES('General', 'general', ?, 1, 10)",
        ("Visible to everyone, including visitors who are not signed in.",),
    ).lastrowid


def default_department_id(conn):
    """Where a record goes when no department was named."""
    row = conn.execute(
        "SELECT id FROM departments WHERE enabled = 1 ORDER BY sort_order, id LIMIT 1"
    ).fetchone()
    return row["id"] if row else ensure_default_department(conn)


def resolve_department_id(conn, value):
    """Validate a submitted department, falling back to the default."""
    department_id = clean_int(value, 0)
    if not department_id:
        return default_department_id(conn)
    if not conn.execute("SELECT 1 FROM departments WHERE id = ?", (department_id,)).fetchone():
        raise ValueError("That department does not exist.")
    return department_id


def migrate_links_cascade(conn):
    """Rebuild `links` so its location foreign key is enforced and cascades.

    The original schema declared the key but never enabled `PRAGMA
    foreign_keys`, so it was inert. Enforcing it requires ON UPDATE CASCADE,
    otherwise renaming a location code would violate the constraint.
    """
    if conn.execute("SELECT 1 FROM settings WHERE key = 'schema_links_cascade'").fetchone():
        return
    definition = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'links'"
    ).fetchone()
    if definition and "ON UPDATE CASCADE" not in definition["sql"]:
        conn.execute("UPDATE links SET location_code = NULL WHERE page_type = 'general'")
        orphans = conn.execute(
            """
            UPDATE links SET location_code = NULL
            WHERE location_code IS NOT NULL
              AND location_code NOT IN (SELECT code FROM locations)
            """
        ).rowcount
        if orphans:
            LOGGER.warning(
                "Cleared the location code on %s link(s) that pointed at a missing location. "
                "Reassign them in the admin center.",
                orphans,
            )
        conn.execute("ALTER TABLE links RENAME TO links_pre_cascade")
        conn.execute(LINKS_TABLE_SQL.format(if_not_exists=""))
        conn.execute(
            f"INSERT INTO links({LINK_COLUMNS}) SELECT {LINK_COLUMNS} FROM links_pre_cascade"
        )
        conn.execute("DROP TABLE links_pre_cascade")
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('schema_links_cascade', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )


def password_hash(password):
    if len(password) < 7:
        raise ValueError("Password must be at least 7 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def password_matches(password, encoded):
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), base64.b64decode(salt), int(iterations)
        )
        return hmac.compare_digest(actual, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def unusable_password_hash():
    """A well-formed hash no password can match.

    Verifying against this costs the same as verifying a real one, so an unknown
    username and a wrong password take equally long to reject.
    """
    salt = base64.b64encode(secrets.token_bytes(16)).decode()
    digest = base64.b64encode(secrets.token_bytes(32)).decode()
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest}"


UNUSABLE_PASSWORD_HASH = unusable_password_hash()


def seed_initial_admin(conn):
    if conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone():
        return
    if not ADMIN_USERNAME and not ADMIN_PASSWORD:
        return
    conn.execute(
        "INSERT INTO admin_users(username, password_hash, created_at) VALUES(?, ?, ?)",
        (ADMIN_USERNAME, password_hash(ADMIN_PASSWORD), int(time.time())),
    )


def setup_required():
    """True only while nobody can possibly log in yet.

    Active Directory counts as a usable login path, so an AD-only deployment
    must never reopen the unauthenticated first-run page.
    """
    global _setup_complete
    if _setup_complete:
        return False
    with db() as conn:
        required = (
            conn.execute("SELECT 1 FROM admin_users WHERE is_admin = 1 LIMIT 1").fetchone() is None
            and not external_auth_enabled(conn)
        )
    if not required:
        _setup_complete = True
    return required


def create_initial_admin(username, password):
    global _setup_complete
    username = clean_required(username, "Username")
    encoded_password = password_hash(password or "")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        already_usable = (
            conn.execute("SELECT 1 FROM admin_users WHERE is_admin = 1 LIMIT 1").fetchone() is not None
            or external_auth_enabled(conn)
        )
        if already_usable:
            raise ValueError("Initial setup has already been completed.")
        conn.execute(
            "INSERT INTO admin_users(username, password_hash, created_at) VALUES(?, ?, ?)",
            (username, encoded_password, int(time.time())),
        )
    _setup_complete = True
    return username


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def branding_payload(conn):
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key IN ('company_name', 'department_title', 'admin_title', 'logo_filename')"
    ).fetchall()
    saved = {row["key"]: row["value"] for row in rows}
    branding = {
        "company_name": saved.get("company_name", DEFAULT_BRANDING["company_name"]),
        "department_title": saved.get("department_title", DEFAULT_BRANDING["department_title"]),
        "admin_title": saved.get("admin_title", DEFAULT_BRANDING["admin_title"]),
        "logo_url": DEFAULT_BRANDING["logo_url"],
    }
    if saved.get("logo_filename"):
        logo_path = DATA_DIR / saved["logo_filename"]
        try:
            branding["logo_url"] = f"/api/branding/logo?v={int(logo_path.stat().st_mtime)}"
        except OSError:
            LOGGER.warning("Branding logo %s is missing; falling back to the default logo.", logo_path)
    return branding


def anonymous_access_allowed(conn):
    """False when the portal has been switched to login-only."""
    return setting_values(conn, ["require_login"]).get("require_login") != "1"


def department_rows(conn):
    return conn.execute(
        "SELECT id, name, slug, description, public, sort_order, enabled "
        "FROM departments ORDER BY sort_order, name"
    ).fetchall()


def readable_department_ids(conn, identity):
    """Which departments this caller may read.

    Administrators see every enabled department. A signed-in viewer sees the
    ones assigned to them. An anonymous visitor sees the departments marked
    public, and only while anonymous access is allowed at all.
    """
    rows = department_rows(conn)
    enabled = {row["id"] for row in rows if row["enabled"]}
    # "Public" means visible to everyone, signed in or not. Without this a
    # viewer assigned to one department would lose access to the company-wide
    # department they could see while signed out.
    public = {row["id"] for row in rows if row["enabled"] and row["public"]}
    if identity is None:
        return public if anonymous_access_allowed(conn) else set()
    if identity.get("is_admin"):
        return enabled
    if identity["source"] == "local":
        assigned = conn.execute(
            "SELECT d.department_id FROM user_departments d "
            "JOIN admin_users u ON u.id = d.user_id WHERE u.username = ?",
            (identity["username"],),
        ).fetchall()
    else:
        assigned = conn.execute(
            "SELECT department_id FROM directory_memberships WHERE source = ? AND username = ?",
            (identity["source"], identity["username"]),
        ).fetchall()
    return ({row["department_id"] for row in assigned} | public) & enabled


def catalog_payload(identity=None):
    """The portal's view, restricted to the departments the caller may read."""
    with db() as conn:
        allowed = readable_department_ids(conn, identity)
        departments = [
            {"id": row["id"], "name": row["name"], "slug": row["slug"], "description": row["description"]}
            for row in department_rows(conn)
            if row["id"] in allowed
        ]
        if not allowed:
            locations, links = [], []
        else:
            placeholders = ",".join("?" for _ in allowed)
            ids = list(allowed)
            locations = rows_to_dicts(
                conn.execute(
                    "SELECT name, code, department_id FROM locations "
                    f"WHERE enabled = 1 AND department_id IN ({placeholders}) "
                    "ORDER BY sort_order, name",
                    ids,
                ).fetchall()
            )
            links = rows_to_dicts(
                conn.execute(
                    "SELECT id, page_type, location_code, link_type, name, url, description, "
                    "group_name, cluster, sort_order, department_id "
                    f"FROM links WHERE enabled = 1 AND department_id IN ({placeholders}) "
                    "ORDER BY sort_order, id",
                    ids,
                ).fetchall()
            )
        branding = branding_payload(conn)
        requires_login = not anonymous_access_allowed(conn)
    return {
        "locations": locations,
        "links": links,
        "departments": departments,
        "branding": branding,
        "product": PRODUCT_NOTICE,
        "viewer": {
            "authenticated": identity is not None,
            "username": identity["username"] if identity else None,
            "is_admin": bool(identity and identity.get("is_admin")),
            "requires_login": requires_login,
        },
    }


def admin_payload():
    with db() as conn:
        locations = rows_to_dicts(
            conn.execute(
                "SELECT id, name, code, sort_order, enabled, department_id FROM locations ORDER BY sort_order, name"
            ).fetchall()
        )
        links = rows_to_dicts(
            conn.execute(
                """
                SELECT id, page_type, location_code, link_type, name, url, description,
                       group_name, cluster, sort_order, enabled, department_id
                FROM links
                ORDER BY page_type, location_code, sort_order, id
                """
            ).fetchall()
        )
        departments = rows_to_dicts(department_rows(conn))
        require_login = not anonymous_access_allowed(conn)
        branding = branding_payload(conn)
    return {
        "locations": locations,
        "links": links,
        "departments": departments,
        "require_login": require_login,
        "branding": branding,
        "product": PRODUCT_NOTICE,
    }


def setting_values(conn, keys):
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT key, value FROM settings WHERE key IN ({placeholders})", keys
    ).fetchall()
    return {row["key"]: row["value"] for row in rows}


def save_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def external_auth_enabled(conn):
    """True when a directory can authenticate administrators without a local account."""
    values = setting_values(conn, ["ad_enabled", "entra_enabled"])
    return values.get("ad_enabled") == "1" or values.get("entra_enabled") == "1"


def auth_payload():
    keys = [
        "ad_enabled", "ad_server", "ad_port", "ad_ssl", "ad_domain",
        "ad_base_dn", "ad_group_dn", "ad_admin_users", "ad_admin_groups",
    ]
    with db() as conn:
        settings = setting_values(conn, keys)
        entra = entra_config(conn)
        users = rows_to_dicts(
            conn.execute(
                "SELECT id, username, enabled, created_at, is_admin FROM admin_users ORDER BY username"
            ).fetchall()
        )
        grouped = {}
        for row in conn.execute("SELECT user_id, department_id FROM user_departments").fetchall():
            grouped.setdefault(row["user_id"], []).append(row["department_id"])
        for user in users:
            user["department_ids"] = sorted(grouped.get(user["id"], []))
        departments = rows_to_dicts(department_rows(conn))
    return {
        "users": users,
        "departments": departments,
        # The client secret is never returned, only whether one is stored.
        "entra": {
            "enabled": entra["enabled"],
            "tenant_id": entra["tenant_id"],
            "client_id": entra["client_id"],
            "redirect_uri": entra["redirect_uri"],
            "admin_users": entra["admin_users"],
            "admin_groups": entra["admin_groups"],
            "admin_roles": entra["admin_roles"],
            "client_secret_set": bool(entra["client_secret"]),
        },
        "ad": {
            "enabled": settings.get("ad_enabled") == "1",
            "server": settings.get("ad_server", ""),
            "port": int(settings.get("ad_port", "636")),
            "ssl": settings.get("ad_ssl", "1") == "1",
            "domain": settings.get("ad_domain", ""),
            "admin_users": settings.get("ad_admin_users", ""),
            "admin_groups": settings.get("ad_admin_groups", settings.get("ad_group_dn", "")),
        },
    }


def authenticate(username, password):
    """Return (source, canonical_username) on success, or None.

    A successful directory sign-in also records what it resolved to, because
    group membership cannot be re-checked on later requests without binding
    again. Local accounts need no such snapshot -- their departments are read
    live from `user_departments`.
    """
    local_username = authenticate_local(username, password)
    if local_username:
        return ("local", local_username)
    if authenticate_ad(username, password):
        canonical = normalize_account_name(username)
        # A directory sign-in has always meant administrator, and still does.
        # Mapping directory groups to departments is a later stage; the tables
        # exist so that change needs no migration.
        record_directory_login("ad", canonical, True, [])
        return ("ad", canonical)
    return None


def record_directory_login(source, username, is_admin, group_refs):
    """Snapshot a directory user's admin flag and department membership."""
    with db() as conn:
        save_directory_user(conn, source, username, is_admin, group_refs)


def save_directory_user(conn, source, username, is_admin, group_refs):
    conn.execute(
        "INSERT INTO directory_users(source, username, is_admin, last_login) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(source, username) DO UPDATE SET is_admin = excluded.is_admin, "
        "last_login = excluded.last_login",
        (source, username, 1 if is_admin else 0, int(time.time())),
    )
    conn.execute(
        "DELETE FROM directory_memberships WHERE source = ? AND username = ?", (source, username)
    )
    if not group_refs:
        return
    placeholders = ",".join("?" for _ in group_refs)
    mapped = conn.execute(
        f"SELECT DISTINCT department_id FROM directory_departments "
        f"WHERE source = ? AND group_ref IN ({placeholders})",
        [source, *group_refs],
    ).fetchall()
    for row in mapped:
        conn.execute(
            "INSERT OR IGNORE INTO directory_memberships(source, username, department_id) VALUES(?, ?, ?)",
            (source, username, row["department_id"]),
        )
    LOGGER.info(
        "%s login for %r resolved to %s department(s) from %s group reference(s).",
        source, username, len(mapped), len(group_refs),
    )


def authenticate_local(username, password):
    with db() as conn:
        user = conn.execute(
            "SELECT username, password_hash FROM admin_users WHERE username = ? AND enabled = 1",
            (username,),
        ).fetchone()
    # Always run the KDF, even for an unknown username, so response time does
    # not reveal which accounts exist.
    encoded = user["password_hash"] if user else UNUSABLE_PASSWORD_HASH
    matched = password_matches(password, encoded)
    return user["username"] if user and matched else None


def authenticate_ad(username, password):
    with db() as conn:
        config = setting_values(
            conn, [
                "ad_enabled", "ad_server", "ad_port", "ad_ssl", "ad_domain",
                "ad_admin_users", "ad_admin_groups", "ad_group_dn",
            ]
        )
    if config.get("ad_enabled") != "1" or not username or not password:
        return False
    try:
        from ldap3 import Connection, Server, SIMPLE, SUBTREE
        from ldap3.utils.conv import escape_filter_chars
    except ImportError:
        raise ValueError("Active Directory support needs the ldap3 package, which is not installed.")

    domain = config.get("ad_domain", "").strip()
    if not domain:
        raise ValueError("Active Directory settings are incomplete.")
    server_name = config.get("ad_server", "").strip() or discover_domain_controller(domain)
    base_dn = domain_to_base_dn(domain)
    bind_name = username if "@" in username or "\\" in username else f"{username}@{domain}"
    server = Server(
        server_name,
        port=clean_int(config.get("ad_port"), 636),
        use_ssl=config.get("ad_ssl", "1") == "1",
        connect_timeout=6,
    )
    try:
        connection = Connection(
            server, user=bind_name, password=password, authentication=SIMPLE, auto_bind=True, receive_timeout=8
        )
        account_name = normalize_account_name(username)
        allowed_users = {
            normalize_account_name(value)
            for value in split_setting_lines(config.get("ad_admin_users", ""))
        }
        allowed_groups = split_setting_lines(
            config.get("ad_admin_groups", "") or config.get("ad_group_dn", "")
        )
        authorized = account_name in allowed_users
        if not authorized:
            for group in allowed_groups:
                group_dn = resolve_group_dn(connection, base_dn, group, SUBTREE, escape_filter_chars)
                if group_dn and user_in_group(
                    connection, base_dn, account_name, group_dn, SUBTREE, escape_filter_chars
                ):
                    authorized = True
                    break
        connection.unbind()
        if not authorized:
            LOGGER.info(
                "Active Directory bind succeeded for %r but the account is not an allowed admin user or group member.",
                account_name,
            )
        return authorized
    except Exception:
        # Bad credentials look the same as a misconfigured server to the client,
        # so log the real cause here instead of silently reporting "invalid
        # username or password" forever.
        LOGGER.warning(
            "Active Directory authentication failed for %r against %s.",
            username, server_name, exc_info=True,
        )
        return False


def split_setting_lines(value):
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def normalize_account_name(value):
    return (value or "").strip().lower().split("\\")[-1].split("@")[0]


def domain_to_base_dn(domain):
    parts = [part.strip() for part in domain.split(".") if part.strip()]
    if len(parts) < 2:
        raise ValueError("Enter the full AD DNS domain, such as example.com.")
    return ",".join(f"DC={part}" for part in parts)


def discover_domain_controller(domain):
    try:
        import dns.resolver
        records = dns.resolver.resolve(f"_ldap._tcp.dc._msdcs.{domain}", "SRV", lifetime=5)
        ordered = sorted(records, key=lambda record: (record.priority, -record.weight))
        if ordered:
            return str(ordered[0].target).rstrip(".")
    except Exception:
        LOGGER.info("Could not discover a domain controller for %s via DNS SRV.", domain, exc_info=True)
    return domain


def sid_filter_value(sid):
    parts = sid.upper().split("-")
    if len(parts) < 4 or parts[0] != "S":
        return None
    try:
        revision = int(parts[1])
        authority = int(parts[2])
        subauthorities = [int(part) for part in parts[3:]]
        raw = bytes([revision, len(subauthorities)])
        raw += authority.to_bytes(6, "big")
        raw += b"".join(value.to_bytes(4, "little") for value in subauthorities)
        return "".join(f"\\{byte:02x}" for byte in raw)
    except (ValueError, OverflowError):
        return None


def resolve_group_dn(connection, base_dn, group, subtree, escape_filter):
    if "=" in group and "," in group:
        return group
    sid_value = sid_filter_value(group)
    if sid_value:
        search_filter = f"(&(objectClass=group)(objectSid={sid_value}))"
    else:
        value = escape_filter(group)
        search_filter = f"(&(objectClass=group)(|(sAMAccountName={value})(cn={value})(name={value})))"
    if not connection.search(base_dn, search_filter, search_scope=subtree, attributes=["distinguishedName"]):
        return None
    return str(connection.entries[0].entry_dn) if connection.entries else None


def user_in_group(connection, base_dn, account_name, group_dn, subtree, escape_filter):
    search_filter = (
        f"(&(objectCategory=person)(objectClass=user)"
        f"(sAMAccountName={escape_filter(account_name)})"
        f"(memberOf:1.2.840.113556.1.4.1941:={escape_filter(group_dn)}))"
    )
    return connection.search(base_dn, search_filter, search_scope=subtree, attributes=["distinguishedName"])


def entra_config(conn):
    values = setting_values(conn, ENTRA_SETTING_KEYS)
    return {
        "enabled": values.get("entra_enabled") == "1",
        "tenant_id": values.get("entra_tenant_id", ""),
        "client_id": values.get("entra_client_id", ""),
        "client_secret": values.get("entra_client_secret", ""),
        "redirect_uri": values.get("entra_redirect_uri", ""),
        "admin_users": values.get("entra_admin_users", ""),
        "admin_groups": values.get("entra_admin_groups", ""),
        "admin_roles": values.get("entra_admin_roles", ""),
    }


def entra_ready(config):
    """True when Entra login is switched on and completely configured."""
    return bool(
        config["enabled"] and config["tenant_id"] and config["client_id"]
        and config["client_secret"] and config["redirect_uri"]
    )


def entra_login_available():
    with db() as conn:
        return entra_ready(entra_config(conn))


def validate_entra_settings(values):
    if not GUID_PATTERN.match(values["entra_tenant_id"]):
        raise ValueError("Directory (tenant) ID must be the GUID shown in the Entra portal.")
    if not GUID_PATTERN.match(values["entra_client_id"]):
        raise ValueError("Application (client) ID must be the GUID shown in the Entra portal.")
    if not values["entra_client_secret"]:
        raise ValueError("A client secret is required to enable Microsoft Entra ID login.")
    parsed = urlparse(values["entra_redirect_uri"])
    on_loopback = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
    if parsed.scheme != "https" and not on_loopback:
        raise ValueError("The redirect URI must use https, or http on localhost for testing.")
    if not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Enter the redirect URI exactly as registered, with no query string.")
    if not parsed.path.endswith("/api/auth/entra/callback"):
        raise ValueError("The redirect URI must end with /api/auth/entra/callback.")
    if not any(
        values[key] for key in ("entra_admin_users", "entra_admin_groups", "entra_admin_roles")
    ):
        raise ValueError("Add at least one Entra admin user, group, or app role.")


def sign_payload(payload):
    """Sign a short-lived cookie payload with the session secret."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(body).decode().rstrip("=")
    signature = hmac.new(SESSION_SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def verify_payload(token):
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    expected = base64.urlsafe_b64encode(
        hmac.new(SESSION_SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or clean_int(payload.get("exp"), 0) <= int(time.time()):
        return None
    return payload


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.decode().rstrip("=")


def entra_authorize_url(config, state, nonce, challenge):
    query = urlencode({
        "client_id": config["client_id"],
        "response_type": "code",
        "redirect_uri": config["redirect_uri"],
        "response_mode": "query",
        "scope": ENTRA_SCOPE,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{ENTRA_AUTHORITY}/{quote(config['tenant_id'])}/oauth2/v2.0/authorize?{query}"


def exchange_entra_code(config, code, verifier):
    """Redeem an authorization code at the tenant's token endpoint."""
    body = urlencode({
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": config["redirect_uri"],
        "code_verifier": verifier,
        "scope": ENTRA_SCOPE,
    }).encode()
    url = f"{ENTRA_AUTHORITY}/{quote(config['tenant_id'])}/oauth2/v2.0/token"
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=15, context=ssl.create_default_context()
        ) as response:
            return json.loads(response.read(MAX_TOKEN_RESPONSE).decode())
    except urllib.error.HTTPError as error:
        detail = error.read(MAX_TOKEN_RESPONSE).decode("utf-8", "replace")[:500]
        LOGGER.warning("Entra token exchange rejected with %s: %s", error.code, detail)
        raise ValueError("Microsoft rejected the sign-in. Check the client secret and redirect URI.")
    except (urllib.error.URLError, OSError, ValueError) as error:
        LOGGER.warning("Entra token exchange failed: %s", error)
        raise ValueError("Could not reach Microsoft to complete the sign-in.")


def decode_jwt_claims(token):
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise ValueError("Microsoft did not return a readable ID token.")
    try:
        return json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Microsoft did not return a readable ID token.")


def validate_entra_claims(claims, config, nonce, now=None):
    """Check an ID token that came straight back from the token endpoint.

    The signature is deliberately not verified, and does not need to be: this
    process opened the TLS connection to login.microsoftonline.com itself and
    read the token off that channel, which OIDC Core 3.1.3.7 allows to stand in
    for signature validation on the authorization code flow. No token is ever
    accepted from the browser -- that is the case that would require fetching
    and checking against the tenant's JWKS.
    """
    if not isinstance(claims, dict):
        raise ValueError("Microsoft did not return a readable ID token.")
    now = now if now is not None else int(time.time())
    tenant = str(claims.get("tid", ""))
    if not GUID_PATTERN.match(tenant) or tenant.lower() != config["tenant_id"].lower():
        raise ValueError("The sign-in came from a different Microsoft tenant.")
    if claims.get("iss") != f"{ENTRA_AUTHORITY}/{tenant}/v2.0":
        raise ValueError("The ID token issuer was not recognised.")
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if config["client_id"] not in audiences:
        raise ValueError("The ID token was issued for a different application.")
    token_nonce = str(claims.get("nonce", ""))
    if not nonce or not hmac.compare_digest(token_nonce, nonce):
        raise ValueError("The sign-in could not be matched to this browser.")
    if clean_int(claims.get("exp"), 0) + ENTRA_CLOCK_SKEW <= now:
        raise ValueError("The Microsoft sign-in expired before it completed.")
    if clean_int(claims.get("nbf"), 0) - ENTRA_CLOCK_SKEW > now:
        raise ValueError("The Microsoft sign-in is not valid yet.")
    return claims


def entra_identity(claims):
    for key in ("preferred_username", "upn", "email", "unique_name", "oid"):
        value = str(claims.get(key) or "").strip().lower()
        if value:
            return value
    return ""


def entra_authorized(claims, config):
    """Match an Entra sign-in against the allowed users, groups, and app roles."""
    allowed_users = {value.lower() for value in split_setting_lines(config["admin_users"])}
    if allowed_users:
        names = {
            str(claims.get(key) or "").strip().lower()
            for key in ("preferred_username", "upn", "email", "unique_name")
        }
        if names & allowed_users:
            return True

    allowed_groups = {value.lower() for value in split_setting_lines(config["admin_groups"])}
    if allowed_groups:
        groups = {str(value).strip().lower() for value in claims.get("groups") or []}
        if groups & allowed_groups:
            return True
        if not groups and claims.get("_claim_names"):
            # Past ~200 groups Entra sends a Graph pointer instead of the list.
            LOGGER.warning(
                "Entra sent a group overage claim rather than group IDs for %r, so group "
                "membership could not be checked. Authorize this tenant with app roles instead.",
                entra_identity(claims),
            )

    allowed_roles = {value.lower() for value in split_setting_lines(config["admin_roles"])}
    if allowed_roles:
        roles = {str(value).strip().lower() for value in claims.get("roles") or []}
        if roles & allowed_roles:
            return True
    return False


def csv_text(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
    return output.getvalue()


def export_csv():
    rows = []
    with db() as conn:
        for location in conn.execute(
            "SELECT name, code, sort_order, enabled FROM locations ORDER BY name"
        ).fetchall():
            rows.append({"record_type": "location", **dict(location)})
        for link in conn.execute(
            """
            SELECT name, page_type, location_code, link_type, url, description,
                   group_name, cluster, sort_order, enabled
            FROM links ORDER BY page_type, location_code, group_name, name
            """
        ).fetchall():
            rows.append({"record_type": "link", **dict(link)})
    return csv_text(rows)


def template_csv():
    return csv_text([
        {
            "record_type": "location", "name": "Example Facility", "code": "ex",
            "sort_order": 10, "enabled": 1,
        },
        {
            "record_type": "link", "name": "Example Service", "page_type": "location",
            "location_code": "ex", "link_type": "standard", "url": "https://service.example.com",
            "description": "Short description shown on the card.", "group_name": "Standard Services",
            "cluster": "", "sort_order": 10, "enabled": 1,
        },
        {
            "record_type": "link", "name": "Example General Link", "page_type": "general",
            "location_code": "", "link_type": "general", "url": "https://portal.example.com",
            "description": "Short description shown on the card.", "group_name": "Operations",
            "cluster": "", "sort_order": 20, "enabled": 1,
        },
    ])


def sign_session(source, username, issued, expires):
    """Bind a session to one identity so it can be validated and revoked."""
    payload = f"{SESSION_VERSION}|{source}|{issued}|{expires}|{username}"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    signature = hmac.new(SESSION_SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def parse_session(token):
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    expected = hmac.new(SESSION_SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
    expected_text = base64.urlsafe_b64encode(expected).decode().rstrip("=")
    if not hmac.compare_digest(signature, expected_text):
        return None
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        version, source, issued_text, expires_text, username = payload.split("|", 4)
        issued, expires = int(issued_text), int(expires_text)
    except (ValueError, UnicodeDecodeError):
        return None
    if version != SESSION_VERSION or source not in AUTH_SOURCES or not username:
        return None
    return {"source": source, "username": username, "issued": issued, "expires": expires}


def resolve_session(token):
    """Return the identity a token belongs to, or None if it is not usable."""
    session = parse_session(token)
    if not session:
        return None
    now = int(time.time())
    if session["expires"] <= now or session["issued"] > now + 60:
        return None
    with db() as conn:
        if session["issued"] < session_epoch(conn, session["source"], session["username"]):
            return None
        if session["source"] == "local":
            active = conn.execute(
                "SELECT is_admin FROM admin_users WHERE username = ? AND enabled = 1",
                (session["username"],),
            ).fetchone()
            if not active:
                return None
            is_admin = bool(active["is_admin"])
        else:
            enabled_key = f"{session['source']}_enabled"
            if setting_values(conn, [enabled_key]).get(enabled_key) != "1":
                return None
            # Editing the allowed users, groups, or roles for a directory takes
            # effect immediately rather than at the end of each session.
            if session["issued"] < source_config_epoch(conn, session["source"]):
                return None
            recorded = conn.execute(
                "SELECT is_admin FROM directory_users WHERE source = ? AND username = ?",
                (session["source"], session["username"]),
            ).fetchone()
            # A session issued before this table existed has no row. Directory
            # sign-in has always granted administrator, so treat it as one
            # rather than silently demoting a live session on upgrade.
            is_admin = bool(recorded["is_admin"]) if recorded else True
    return {
        "source": session["source"],
        "username": session["username"],
        "is_admin": is_admin,
    }


def source_config_epoch(conn, source):
    key = f"{source}_config_epoch"
    return clean_int(setting_values(conn, [key]).get(key), 0)


def touch_source_config_epoch(conn, source):
    """Stamp a directory's settings so sessions predating the change are dropped.

    Unlike per-account revocation this uses the current second rather than the
    next one, because a sign-in that lands in the same second as the save has to
    survive -- an administrator enabling a directory would otherwise invalidate
    the session they are about to create.
    """
    save_setting(conn, f"{source}_config_epoch", str(int(time.time())))


def session_epoch(conn, source, username):
    row = conn.execute(
        "SELECT epoch FROM session_epochs WHERE source = ? AND username = ?",
        (source, username),
    ).fetchone()
    return row["epoch"] if row else 0


def revoke_sessions(conn, source, username):
    """Invalidate every token already issued to this identity."""
    conn.execute(
        "INSERT INTO session_epochs(source, username, epoch) VALUES(?, ?, ?) "
        "ON CONFLICT(source, username) DO UPDATE SET epoch = excluded.epoch",
        (source, username, int(time.time()) + 1),
    )


def throttle_key(kind, value):
    return (kind, (value or "").strip().lower())


def login_retry_after(username, client_ip):
    """Seconds the caller must wait, or 0 when the attempt may proceed."""
    now = int(time.time())
    with _throttle_lock:
        prune_throttle(now)
        waits = [0]
        for key in (throttle_key("user", username), throttle_key("ip", client_ip)):
            entry = _throttle.get(key)
            if entry:
                waits.append(entry["locked_until"] - now)
    return max(waits)


def record_login_failure(username, client_ip):
    now = int(time.time())
    with _throttle_lock:
        for kind, value in (("user", username), ("ip", client_ip)):
            limits = LOGIN_LIMITS[kind]
            key = throttle_key(kind, value)
            entry = _throttle.setdefault(key, {"failures": [], "locked_until": 0})
            entry["seen"] = now
            entry["failures"] = [
                stamp for stamp in entry["failures"] if stamp + limits["window"] > now
            ]
            entry["failures"].append(now)
            if len(entry["failures"]) >= limits["max_failures"]:
                entry["locked_until"] = now + limits["lockout"]
                entry["failures"].clear()
        prune_throttle(now)


def record_login_success(username, client_ip):
    with _throttle_lock:
        _throttle.pop(throttle_key("user", username), None)
        _throttle.pop(throttle_key("ip", client_ip), None)


def prune_throttle(now):
    longest_window = max(limits["window"] for limits in LOGIN_LIMITS.values())
    stale = [
        key for key, entry in _throttle.items()
        if entry["locked_until"] <= now and entry.get("seen", 0) + longest_window <= now
    ]
    for key in stale:
        del _throttle[key]
    if len(_throttle) > THROTTLE_MAX_KEYS:
        oldest = sorted(_throttle, key=lambda key: _throttle[key].get("seen", 0))
        for key in oldest[: len(_throttle) - THROTTLE_MAX_KEYS]:
            del _throttle[key]


def cookie_value(header, name):
    if not header:
        return None
    cookie = SimpleCookie()
    cookie.load(header)
    morsel = cookie.get(name)
    return morsel.value if morsel else None


def served_file(requested):
    """Resolve a request path to a file on disk, or None if it is not served.

    `SimpleHTTPRequestHandler.translate_path` is replaced rather than extended,
    so this has to do its own containment checking: resolve the candidate, keep
    it under the app directory, keep it out of the data directory, and require
    it to be one of the handful of paths the browser actually needs.
    """
    root = APP_DIR.resolve()
    try:
        candidate = (root / requested.lstrip("/")).resolve()
        data_dir = DATA_DIR.resolve()
    except OSError:
        return None
    if not candidate.is_relative_to(root) or candidate.is_relative_to(data_dir):
        return None
    if requested in STATIC_FILES and candidate.is_file():
        return candidate
    if any(candidate.is_relative_to(root / name) for name in STATIC_DIRS) and candidate.is_file():
        return candidate
    return None


class AppHandler(SimpleHTTPRequestHandler):
    server_version = "QuickLinks"
    sys_version = ""

    def translate_path(self, path):
        requested = unquote(urlparse(path).path)
        if requested in ("", "/"):
            requested = "/index.html"
        elif requested == "/admin":
            requested = "/admin.html"
        allowed = served_file(requested)
        # Returning a path that cannot exist makes the base handler answer 404
        # without disclosing whether the real target is there.
        return str(allowed) if allowed else str(APP_DIR / UNSERVABLE)

    def end_headers(self):
        self.send_header("X-QuickLinks-Creator", PRODUCT_NOTICE["creator"])
        self.send_header("X-QuickLinks-Notice", PRODUCT_NOTICE["header_notice"])
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; base-uri 'self'; "
            "object-src 'none'; form-action 'self'; frame-ancestors 'self'",
        )
        super().end_headers()

    def version_string(self):
        # Deliberately omits the release and the Python version.
        return self.server_version

    def client_ip(self):
        if TRUST_PROXY:
            headers = getattr(self, "headers", None)
            forwarded = (headers.get("X-Forwarded-For", "") if headers else "").strip()
            if forwarded:
                return forwarded.split(",")[0].strip()
        address = getattr(self, "client_address", None)
        return address[0] if address else "unknown"

    def cookie_is_secure(self):
        if COOKIE_SECURE in ("1", "true", "yes"):
            return True
        if COOKIE_SECURE in ("0", "false", "no"):
            return False
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https"

    def session_cookie(self, token, max_age):
        parts = [f"{SESSION_COOKIE}={token}", "HttpOnly", "SameSite=Lax", "Path=/", f"Max-Age={max_age}"]
        if self.cookie_is_secure():
            parts.append("Secure")
        return "; ".join(parts)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" and setup_required():
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/admin")
            self.end_headers()
            return
        if parsed.path == "/api/catalog":
            self.send_json(catalog_payload(self.identity()))
            return
        if parsed.path == "/api/product":
            self.send_json(PRODUCT_NOTICE)
            return
        if parsed.path == "/api/branding/logo":
            self.send_branding_logo()
            return
        if parsed.path == "/api/admin":
            if not self.require_admin():
                return
            self.send_json(admin_payload())
            return
        if parsed.path == "/api/auth-config":
            if not self.require_admin():
                return
            self.send_json(auth_payload())
            return
        if parsed.path == "/api/export.csv":
            if not self.require_admin():
                return
            self.send_csv(export_csv(), "quicklinks-export.csv")
            return
        if parsed.path == "/api/import-template.csv":
            if not self.require_admin():
                return
            self.send_csv(template_csv(), "quicklinks-import-template.csv")
            return
        if parsed.path == "/api/session":
            self.send_json({
                "authenticated": self.is_admin(),
                "setup_required": setup_required(),
                "entra_available": entra_login_available(),
            })
            return
        if parsed.path == "/api/auth/entra/start":
            self.entra_start()
            return
        if parsed.path == "/api/auth/entra/callback":
            self.entra_callback(parsed.query)
            return
        if parsed.path.startswith("/api/"):
            self.send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self.body_within_limit():
            return
        if parsed.path == "/api/login":
            self.safe_write(self.login)
            return
        if parsed.path == "/api/setup":
            self.safe_write(self.initial_setup)
            return
        if parsed.path == "/api/logout":
            self.logout()
            return
        routes = {
            "/api/locations": self.save_location,
            "/api/links": self.save_link,
            "/api/branding": self.save_branding,
            "/api/import": self.import_catalog,
            "/api/admin-users": self.save_admin_user,
            "/api/auth-config": self.save_auth_config,
            "/api/entra-config": self.save_entra_config,
            "/api/departments": self.save_department,
            "/api/portal-settings": self.save_portal_settings,
        }
        action = routes.get(parsed.path)
        if not action:
            self.drain_body()
            self.send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
            return
        if not self.is_admin():
            self.drain_body()
            self.send_json({"error": "Admin login required."}, HTTPStatus.UNAUTHORIZED)
            return
        self.safe_write(action)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if not self.require_admin():
            return
        if parsed.path.startswith("/api/locations/"):
            self.safe_write(lambda: self.delete_location(parsed.path.rsplit("/", 1)[-1]))
            return
        if parsed.path.startswith("/api/links/"):
            self.safe_write(lambda: self.delete_link(parsed.path.rsplit("/", 1)[-1]))
            return
        if parsed.path.startswith("/api/admin-users/"):
            self.safe_write(lambda: self.delete_admin_user(parsed.path.rsplit("/", 1)[-1]))
            return
        if parsed.path.startswith("/api/departments/"):
            self.safe_write(lambda: self.delete_department(parsed.path.rsplit("/", 1)[-1]))
            return
        self.send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def login(self):
        body = self.read_json()
        username = (body.get("username") or "").strip()
        password = body.get("password", "")
        client = self.client_ip()

        wait = login_retry_after(username, client)
        if wait > 0:
            LOGGER.warning("Rejected a throttled login for %r from %s.", username, client)
            self.send_json(
                {"error": f"Too many failed attempts. Try again in {wait} seconds."},
                HTTPStatus.TOO_MANY_REQUESTS,
                extra_headers=(("Retry-After", str(wait)),),
            )
            return

        identity = authenticate(username, password)
        if not identity:
            record_login_failure(username, client)
            LOGGER.info("Failed login for %r from %s.", username, client)
            self.send_json({"error": "Invalid username or password."}, HTTPStatus.UNAUTHORIZED)
            return

        record_login_success(username, client)
        source, canonical = identity
        LOGGER.info("Successful %s login for %r from %s.", source, canonical, client)
        self.send_session(HTTPStatus.OK, {"authenticated": True}, source, canonical)

    def initial_setup(self):
        body = self.read_json()
        username = create_initial_admin(body.get("username"), body.get("password"))
        self.send_session(
            HTTPStatus.CREATED,
            {"authenticated": True, "setup_required": False},
            "local",
            username,
        )

    def send_session(self, status, payload, source, username):
        issued = int(time.time())
        token = sign_session(source, username, issued, issued + SESSION_MAX_AGE)
        self.send_json(
            payload,
            status,
            extra_headers=(("Set-Cookie", self.session_cookie(token, SESSION_MAX_AGE)),),
        )

    def logout(self):
        identity = self.identity()
        if identity:
            with db() as conn:
                revoke_sessions(conn, identity["source"], identity["username"])
        self.send_json(
            {"authenticated": False},
            extra_headers=(("Set-Cookie", self.session_cookie("", 0)),),
        )

    def save_admin_user(self):
        body = self.read_json()
        user_id = clean_int(body.get("id"), 0)
        username = clean_required(body.get("username"), "Username")
        password = body.get("password") or ""
        enabled = 1 if body.get("enabled", True) else 0
        is_admin = 1 if body.get("is_admin", True) else 0
        department_ids = [clean_int(value, 0) for value in (body.get("department_ids") or [])]
        department_ids = [value for value in department_ids if value]
        if not is_admin and not department_ids:
            raise ValueError(
                "A viewer with no departments could not see anything. Assign at least one, "
                "or make the account an administrator."
            )
        with db() as conn:
            if user_id:
                existing = conn.execute(
                    "SELECT username, is_admin FROM admin_users WHERE id = ?", (user_id,)
                ).fetchone()
                if not existing:
                    raise ValueError("Account not found.")
                if password:
                    conn.execute(
                        "UPDATE admin_users SET username = ?, password_hash = ?, enabled = ?, "
                        "is_admin = ? WHERE id = ?",
                        (username, password_hash(password), enabled, is_admin, user_id),
                    )
                else:
                    conn.execute(
                        "UPDATE admin_users SET username = ?, enabled = ?, is_admin = ? WHERE id = ?",
                        (username, enabled, is_admin, user_id),
                    )
                assign_user_departments(conn, user_id, department_ids)
                if existing["is_admin"] and (not is_admin or not enabled):
                    remaining = conn.execute(
                        "SELECT COUNT(*) AS count FROM admin_users "
                        "WHERE enabled = 1 AND is_admin = 1 AND id != ?",
                        (user_id,),
                    ).fetchone()["count"]
                    if not remaining and not external_auth_enabled(conn):
                        raise ValueError(
                            "This is the only administrator. Promote another account first, "
                            "or enable a directory."
                        )
                renamed = username.lower() != existing["username"].lower()
                if password or renamed or not enabled:
                    revoke_sessions(conn, "local", existing["username"])
                    if renamed:
                        revoke_sessions(conn, "local", username)
            else:
                new_id = conn.execute(
                    "INSERT INTO admin_users(username, password_hash, enabled, created_at, is_admin) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (username, password_hash(clean_required(password, "Password")), enabled,
                     int(time.time()), is_admin),
                ).lastrowid
                assign_user_departments(conn, new_id, department_ids)
        self.send_json(auth_payload())

    def delete_admin_user(self, raw_id):
        user_id = clean_int(raw_id, 0)
        with db() as conn:
            enabled_count = conn.execute(
                "SELECT COUNT(*) AS count FROM admin_users WHERE enabled = 1 AND is_admin = 1"
            ).fetchone()["count"]
            target = conn.execute(
                "SELECT username, enabled, is_admin FROM admin_users WHERE id = ?", (user_id,)
            ).fetchone()
            if not target:
                raise ValueError("Account not found.")
            if target["enabled"] and target["is_admin"] and enabled_count <= 1 and not external_auth_enabled(conn):
                raise ValueError(
                    "Keep one enabled local administrator until Active Directory "
                    "or Microsoft Entra ID is enabled. Viewer accounts do not count."
                )
            conn.execute("DELETE FROM admin_users WHERE id = ?", (user_id,))
            revoke_sessions(conn, "local", target["username"])
        self.send_json(auth_payload())

    def flow_cookie(self, token, max_age):
        parts = [
            f"{ENTRA_FLOW_COOKIE}={token}", "HttpOnly", "SameSite=Lax",
            "Path=/api/auth/entra", f"Max-Age={max_age}",
        ]
        if self.cookie_is_secure():
            parts.append("Secure")
        return "; ".join(parts)

    def redirect(self, location, cookies=()):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def entra_failed(self, reason):
        """Send the browser back to the sign-in page with a short reason code."""
        self.redirect(f"/admin?entra_error={quote(reason)}", cookies=(self.flow_cookie("", 0),))

    def entra_start(self):
        with db() as conn:
            config = entra_config(conn)
        if not entra_ready(config):
            LOGGER.warning("Entra sign-in was requested but is not fully configured.")
            self.entra_failed("config")
            return
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        flow = sign_payload({
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "exp": int(time.time()) + ENTRA_FLOW_MAX_AGE,
        })
        self.redirect(
            entra_authorize_url(config, state, nonce, challenge),
            cookies=(self.flow_cookie(flow, ENTRA_FLOW_MAX_AGE),),
        )

    def entra_callback(self, query):
        params = parse_qs(query)
        if params.get("error"):
            LOGGER.warning(
                "Entra returned %s: %s",
                params["error"][0][:100],
                (params.get("error_description") or [""])[0][:300],
            )
            self.entra_failed("denied")
            return

        flow = verify_payload(cookie_value(self.headers.get("Cookie"), ENTRA_FLOW_COOKIE))
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]
        if not flow or not code or not hmac.compare_digest(state, str(flow.get("state", ""))):
            LOGGER.warning("Entra callback did not match a pending sign-in from this browser.")
            self.entra_failed("state")
            return

        with db() as conn:
            config = entra_config(conn)
        if not entra_ready(config):
            self.entra_failed("config")
            return

        try:
            tokens = exchange_entra_code(config, code, str(flow.get("verifier", "")))
            claims = decode_jwt_claims(tokens.get("id_token"))
            validate_entra_claims(claims, config, str(flow.get("nonce", "")))
        except ValueError as error:
            LOGGER.warning("Entra sign-in could not be verified: %s", error)
            self.entra_failed("token")
            return

        username = entra_identity(claims)
        if not username:
            LOGGER.warning("Entra ID token carried no usable account name.")
            self.entra_failed("token")
            return
        if not entra_authorized(claims, config):
            LOGGER.warning("Entra account %r is not an allowed QuickLinks administrator.", username)
            self.entra_failed("forbidden")
            return

        LOGGER.info("Successful entra login for %r from %s.", username, self.client_ip())
        # As with AD, an Entra sign-in currently implies administrator.
        record_directory_login("entra", username, True, [])
        issued = int(time.time())
        token = sign_session("entra", username, issued, issued + SESSION_MAX_AGE)
        self.redirect("/admin", cookies=(
            self.session_cookie(token, SESSION_MAX_AGE),
            self.flow_cookie("", 0),
        ))

    def save_entra_config(self):
        body = self.read_json()
        enabled = bool(body.get("enabled"))
        submitted_secret = (body.get("client_secret") or "").strip()
        with db() as conn:
            current = entra_config(conn)
            values = {
                "entra_enabled": "1" if enabled else "0",
                "entra_tenant_id": (body.get("tenant_id") or "").strip(),
                "entra_client_id": (body.get("client_id") or "").strip(),
                # Blank means "keep the stored secret", so rotating other
                # settings does not require re-entering it.
                "entra_client_secret": submitted_secret or current["client_secret"],
                "entra_redirect_uri": (body.get("redirect_uri") or "").strip(),
                "entra_admin_users": normalize_multiline(body.get("admin_users")),
                "entra_admin_groups": normalize_multiline(body.get("admin_groups")),
                "entra_admin_roles": normalize_multiline(body.get("admin_roles")),
            }
            if enabled:
                validate_entra_settings(values)
            for key, value in values.items():
                save_setting(conn, key, value)
            touch_source_config_epoch(conn, "entra")
        self.send_json(auth_payload())

    def save_portal_settings(self):
        body = self.read_json()
        require_login = "1" if body.get("require_login") else "0"
        with db() as conn:
            if require_login == "1" and not conn.execute(
                "SELECT 1 FROM admin_users WHERE enabled = 1 LIMIT 1"
            ).fetchone() and not external_auth_enabled(conn):
                raise ValueError(
                    "Enable a login method before requiring one, or nobody will be able to sign in."
                )
            save_setting(conn, "require_login", require_login)
        LOGGER.info("Anonymous portal access %s.", "disabled" if require_login == "1" else "enabled")
        self.send_json(admin_payload())

    def save_department(self):
        body = self.read_json()
        department_id = clean_int(body.get("id"), 0)
        name = clean_required(body.get("name"), "Department name")
        slug = department_slug(body.get("slug") or name)
        description = (body.get("description") or "").strip()
        public = 1 if body.get("public") else 0
        sort_order = clean_int(body.get("sort_order"), 0)
        enabled = 1 if body.get("enabled", True) else 0
        with db() as conn:
            if department_id:
                if not conn.execute(
                    "SELECT 1 FROM departments WHERE id = ?", (department_id,)
                ).fetchone():
                    raise ValueError("Department not found.")
                conn.execute(
                    "UPDATE departments SET name = ?, slug = ?, description = ?, public = ?, "
                    "sort_order = ?, enabled = ? WHERE id = ?",
                    (name, slug, description, public, sort_order, enabled, department_id),
                )
            else:
                conn.execute(
                    "INSERT INTO departments(name, slug, description, public, sort_order, enabled) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (name, slug, description, public, sort_order, enabled),
                )
        self.send_json(admin_payload())

    def delete_department(self, raw_id):
        department_id = clean_int(raw_id, 0)
        with db() as conn:
            if not conn.execute(
                "SELECT 1 FROM departments WHERE id = ?", (department_id,)
            ).fetchone():
                raise ValueError("Department not found.")
            if conn.execute("SELECT COUNT(*) FROM departments").fetchone()[0] <= 1:
                raise ValueError("Keep at least one department; every link and location needs one.")
            # Refuse rather than cascade. Deleting a department that still holds
            # content would take the content with it, which is not what someone
            # tidying up a department list expects.
            holdings = conn.execute(
                "SELECT (SELECT COUNT(*) FROM locations WHERE department_id = ?) AS locations, "
                "(SELECT COUNT(*) FROM links WHERE department_id = ?) AS links",
                (department_id, department_id),
            ).fetchone()
            if holdings["locations"] or holdings["links"]:
                raise ValueError(
                    f"That department still holds {holdings['locations']} location(s) and "
                    f"{holdings['links']} link(s). Move them to another department first."
                )
            conn.execute("DELETE FROM departments WHERE id = ?", (department_id,))
        self.send_json(admin_payload())

    def save_auth_config(self):
        body = self.read_json()
        enabled = bool(body.get("enabled"))
        values = {
            "ad_enabled": "1" if enabled else "0",
            "ad_server": (body.get("server") or "").strip(),
            "ad_port": str(clean_int(body.get("port"), 636)),
            "ad_ssl": "1" if body.get("ssl", True) else "0",
            "ad_domain": (body.get("domain") or "").strip(),
            "ad_admin_users": normalize_multiline(body.get("admin_users")),
            "ad_admin_groups": normalize_multiline(body.get("admin_groups")),
        }
        if enabled:
            if not values["ad_domain"]:
                raise ValueError("AD domain is required when Active Directory is enabled.")
            domain_to_base_dn(values["ad_domain"])
            if not values["ad_admin_users"] and not values["ad_admin_groups"]:
                raise ValueError("Add at least one AD admin user or admin group.")
        with db() as conn:
            for key, value in values.items():
                save_setting(conn, key, value)
            touch_source_config_epoch(conn, "ad")
        self.send_json(auth_payload())

    def import_catalog(self):
        body = self.read_json()
        raw_csv = body.get("csv") or ""
        mode = body.get("mode") if body.get("mode") in ("merge", "replace") else "merge"
        if len(raw_csv.encode()) > MAX_CSV_BYTES:
            raise ValueError("CSV files must be smaller than 5 MB.")
        reader = csv.DictReader(io.StringIO(raw_csv.lstrip(chr(0xFEFF))))
        if not reader.fieldnames or any(field not in reader.fieldnames for field in CSV_FIELDS):
            raise ValueError("The CSV columns do not match the provided template.")
        locations, links = [], []
        for line_number, row in enumerate(reader, start=2):
            record_type = (row.get("record_type") or "").strip().lower()
            if not any((value or "").strip() for value in row.values()):
                continue
            if record_type == "location":
                locations.append({
                    "name": clean_required(row.get("name"), f"Location name on row {line_number}"),
                    "code": clean_required(row.get("code"), f"Location code on row {line_number}").lower(),
                    "sort_order": clean_int(row.get("sort_order"), 0),
                    "enabled": csv_bool(row.get("enabled"), line_number),
                })
            elif record_type == "link":
                page_type = (row.get("page_type") or "").strip().lower()
                if page_type not in ("general", "location"):
                    raise ValueError(f"Row {line_number}: page_type must be general or location.")
                location_code = (row.get("location_code") or "").strip().lower() or None
                if page_type == "general":
                    location_code = None
                elif not location_code:
                    raise ValueError(f"Row {line_number}: location links require a location_code.")
                links.append({
                    "page_type": page_type,
                    "location_code": location_code,
                    "link_type": (row.get("link_type") or ("general" if page_type == "general" else "standard")).strip(),
                    "name": clean_required(row.get("name"), f"Link name on row {line_number}"),
                    "url": clean_url(row.get("url"), f"URL on row {line_number}"),
                    "description": (row.get("description") or "").strip(),
                    "group_name": clean_required(row.get("group_name"), f"Group on row {line_number}"),
                    "cluster": (row.get("cluster") or "").strip(),
                    "sort_order": clean_int(row.get("sort_order"), 0),
                    "enabled": csv_bool(row.get("enabled"), line_number),
                })
            else:
                raise ValueError(f"Row {line_number}: record_type must be location or link.")

        available_codes = {location["code"] for location in locations}
        with db() as conn:
            import_department = default_department_id(conn)
            if mode == "merge":
                available_codes.update(
                    row["code"] for row in conn.execute("SELECT code FROM locations").fetchall()
                )
            missing = sorted({
                link["location_code"] for link in links
                if link["location_code"] and link["location_code"] not in available_codes
            })
            if missing:
                raise ValueError(f"Unknown location code(s): {', '.join(missing)}.")
            if mode == "replace":
                conn.execute("DELETE FROM links")
                conn.execute("DELETE FROM locations")
            for location in locations:
                conn.execute(
                    """
                    INSERT INTO locations(name, code, sort_order, enabled, department_id)
                    VALUES(:name, :code, :sort_order, :enabled, :department_id)
                    ON CONFLICT(code) DO UPDATE SET name=excluded.name, sort_order=excluded.sort_order, enabled=excluded.enabled
                    """,
                    {**location, "department_id": import_department},
                )
            for link in links:
                existing = conn.execute(
                    """
                    SELECT id FROM links WHERE page_type = ? AND COALESCE(location_code, '') = COALESCE(?, '') AND name = ?
                    """,
                    (link["page_type"], link["location_code"], link["name"]),
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE links SET link_type=:link_type, url=:url, description=:description,
                          group_name=:group_name, cluster=:cluster, sort_order=:sort_order, enabled=:enabled
                        WHERE id=:id
                        """,
                        {**link, "id": existing["id"]},
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO links(page_type, location_code, link_type, name, url, description,
                          group_name, cluster, sort_order, enabled, department_id)
                        VALUES(:page_type, :location_code, :link_type, :name, :url, :description,
                          :group_name, :cluster, :sort_order, :enabled, :department_id)
                        """,
                        {**link, "department_id": import_department},
                    )
        self.send_json({
            **admin_payload(),
            "imported": {"locations": len(locations), "links": len(links), "mode": mode},
        })

    def save_location(self):
        body = self.read_json()
        location_id = clean_int(body.get("id"), 0)
        name = clean_required(body.get("name"), "Location name")
        code = clean_required(body.get("code"), "Location code").lower()
        sort_order = clean_int(body.get("sort_order"), 0)
        enabled = 1 if body.get("enabled", True) else 0
        with db() as conn:
            department_id = resolve_department_id(conn, body.get("department_id"))
            if location_id:
                if not conn.execute(
                    "SELECT 1 FROM locations WHERE id = ?", (location_id,)
                ).fetchone():
                    raise ValueError("Location not found.")
                # The location foreign key cascades, so renaming a code carries
                # its links along automatically.
                conn.execute(
                    "UPDATE locations SET name = ?, code = ?, sort_order = ?, enabled = ?, "
                    "department_id = ? WHERE id = ?",
                    (name, code, sort_order, enabled, department_id, location_id),
                )
                # A location link belongs to whatever department its location
                # does, so moving a location moves its links with it. Otherwise
                # the two could disagree and a link would be visible to a
                # department that cannot see the location it sits under.
                conn.execute(
                    "UPDATE links SET department_id = ? WHERE page_type = 'location' AND location_code = ?",
                    (department_id, code),
                )
            else:
                conn.execute(
                    "INSERT INTO locations(name, code, sort_order, enabled, department_id) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (name, code, sort_order, enabled, department_id),
                )
        self.send_json(admin_payload())

    def save_link(self):
        body = self.read_json()
        link_id = clean_int(body.get("id"), 0)
        page_type = (body.get("page_type") or "").strip().lower()
        if page_type not in ("general", "location"):
            raise ValueError("Page type must be general or location.")
        location_code = (body.get("location_code") or "").strip().lower() or None
        if page_type == "general":
            location_code = None
        elif not location_code:
            raise ValueError("Choose a location for this link.")
        link_type = (body.get("link_type") or "standard").strip()
        name = clean_required(body.get("name"), "Link name")
        url = clean_url(body.get("url"), "URL")
        description = (body.get("description") or "").strip()
        group_name = clean_required(body.get("group_name"), "Group")
        cluster = (body.get("cluster") or "").strip()
        sort_order = clean_int(body.get("sort_order"), 0)
        enabled = 1 if body.get("enabled", True) else 0
        with db() as conn:
            if page_type == "location":
                owner = conn.execute(
                    "SELECT department_id FROM locations WHERE code = ?", (location_code,)
                ).fetchone()
                if not owner:
                    raise ValueError("That location code does not exist.")
                # Inherited, not chosen: see save_location.
                department_id = owner["department_id"] or default_department_id(conn)
            else:
                department_id = resolve_department_id(conn, body.get("department_id"))
            if link_id:
                if not conn.execute("SELECT 1 FROM links WHERE id = ?", (link_id,)).fetchone():
                    raise ValueError("Link not found.")
                conn.execute(
                    """
                    UPDATE links
                    SET page_type = ?, location_code = ?, link_type = ?, name = ?, url = ?, description = ?,
                        group_name = ?, cluster = ?, sort_order = ?, enabled = ?, department_id = ?
                    WHERE id = ?
                    """,
                    (page_type, location_code, link_type, name, url, description, group_name, cluster, sort_order, enabled, department_id, link_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO links(page_type, location_code, link_type, name, url, description, group_name, cluster, sort_order, enabled, department_id)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (page_type, location_code, link_type, name, url, description, group_name, cluster, sort_order, enabled, department_id),
                )
        self.send_json(admin_payload())

    def save_branding(self):
        body = self.read_json()
        company_name = clean_required(body.get("company_name"), "Company name")
        department_title = clean_required(body.get("department_title"), "Homepage title")
        admin_title = clean_required(body.get("admin_title"), "Admin title")
        logo_data = body.get("logo_data")
        remove_logo = bool(body.get("remove_logo"))

        with db() as conn:
            for key, value in (
                ("company_name", company_name),
                ("department_title", department_title),
                ("admin_title", admin_title),
            ):
                save_setting(conn, key, value)

            existing = conn.execute("SELECT value FROM settings WHERE key = 'logo_filename'").fetchone()
            if remove_logo and existing:
                (DATA_DIR / existing["value"]).unlink(missing_ok=True)
                conn.execute("DELETE FROM settings WHERE key = 'logo_filename'")

            if logo_data:
                try:
                    header, encoded = logo_data.split(",", 1)
                    mime_type = header.split(";", 1)[0].removeprefix("data:")
                    extension = ALLOWED_LOGO_TYPES[mime_type]
                    image_bytes = base64.b64decode(encoded, validate=True)
                except (ValueError, KeyError):
                    raise ValueError("Choose a PNG, JPG, or WebP logo.")
                if len(image_bytes) > MAX_LOGO_BYTES:
                    raise ValueError("The logo must be smaller than 5 MB.")
                if existing:
                    (DATA_DIR / existing["value"]).unlink(missing_ok=True)
                filename = f"branding-logo{extension}"
                (DATA_DIR / filename).write_bytes(image_bytes)
                save_setting(conn, "logo_filename", filename)
        self.send_json(admin_payload())

    def send_branding_logo(self):
        with db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = 'logo_filename'").fetchone()
        if not row:
            self.send_json({"error": "No logo has been uploaded."}, HTTPStatus.NOT_FOUND)
            return
        # The stored name is always one this server wrote, but resolve it anyway
        # so a tampered settings row cannot read outside the data directory.
        logo_path = (DATA_DIR / row["value"]).resolve()
        if not logo_path.is_relative_to(DATA_DIR.resolve()) or not logo_path.is_file():
            self.send_json({"error": "No logo has been uploaded."}, HTTPStatus.NOT_FOUND)
            return
        body = logo_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(logo_path.name)[0] or "application/octet-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def delete_location(self, raw_id):
        location_id = clean_int(raw_id, 0)
        with db() as conn:
            row = conn.execute("SELECT code FROM locations WHERE id = ?", (location_id,)).fetchone()
            if not row:
                raise ValueError("Location not found.")
            # Redundant while the foreign key cascades, but keeps the intent
            # explicit and correct even if enforcement is ever disabled.
            conn.execute("DELETE FROM links WHERE location_code = ?", (row["code"],))
            conn.execute("DELETE FROM locations WHERE id = ?", (location_id,))
        self.send_json(admin_payload())

    def delete_link(self, raw_id):
        with db() as conn:
            conn.execute("DELETE FROM links WHERE id = ?", (clean_int(raw_id, 0),))
        self.send_json(admin_payload())

    def identity(self):
        return resolve_session(cookie_value(self.headers.get("Cookie"), SESSION_COOKIE))

    def is_admin(self):
        identity = self.identity()
        return bool(identity and identity.get("is_admin"))

    def require_admin(self):
        if self.is_admin():
            return True
        self.send_json({"error": "Admin login required."}, HTTPStatus.UNAUTHORIZED)
        return False

    def body_within_limit(self):
        """Reject an oversized body before any of it is read into memory."""
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            length = -1
        if length < 0:
            self.send_json(
                {"error": "A valid Content-Length header is required."},
                HTTPStatus.BAD_REQUEST,
            )
            return False
        if length > MAX_REQUEST_BODY:
            self.close_connection = True
            self.send_json(
                {"error": "Request body is too large."},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return False
        return True

    def drain_body(self):
        """Read and discard a request body this handler will not parse.

        Answering a POST without consuming its body leaves unread bytes in the
        socket. Closing in that state sends an RST rather than a FIN, which can
        destroy the response before the client has read it -- so an expired
        session would surface as a connection reset instead of a clean 401.
        """
        remaining = max(0, min(clean_int(self.headers.get("Content-Length", "0"), 0), MAX_REQUEST_BODY))
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def read_json(self):
        length = clean_int(self.headers.get("Content-Length", "0"), 0)
        length = max(0, min(length, MAX_REQUEST_BODY))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def safe_write(self, action):
        try:
            action()
        except ValueError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except sqlite3.IntegrityError as error:
            text = str(error)
            if "FOREIGN KEY" in text:
                message = "That location code does not exist."
            elif "UNIQUE" in text:
                message = "That code or value already exists."
            else:
                message = text
            self.send_json({"error": message}, HTTPStatus.BAD_REQUEST)
        except sqlite3.OperationalError as error:
            LOGGER.warning("Database unavailable for %s %s: %s", self.command, self.path, error)
            self.send_json(
                {"error": "The database is busy. Try that again in a moment."},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )

    def send_json(self, payload, status=HTTPStatus.OK, extra_headers=()):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_csv(self, text, filename):
        body = text.encode("utf-8-sig")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        LOGGER.info("%s - %s", self.client_ip(), format % args)


def clean_required(value, label):
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{label} is required.")
    return cleaned


def assign_user_departments(conn, user_id, department_ids):
    """Replace a local user's department assignments."""
    conn.execute("DELETE FROM user_departments WHERE user_id = ?", (user_id,))
    for department_id in dict.fromkeys(department_ids):
        if not conn.execute("SELECT 1 FROM departments WHERE id = ?", (department_id,)).fetchone():
            raise ValueError("That department does not exist.")
        conn.execute(
            "INSERT OR IGNORE INTO user_departments(user_id, department_id) VALUES(?, ?)",
            (user_id, department_id),
        )


def department_slug(value):
    """A short, URL-safe handle for a department."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", clean_required(value, "Department slug").lower()).strip("-")
    if not cleaned:
        raise ValueError("Department slug must contain at least one letter or number.")
    return cleaned[:48]


def clean_url(value, label="URL"):
    """Reject URL schemes that would run script from a link card."""
    url = clean_required(value, label)
    probe = "".join(character for character in url if character.isprintable() and not character.isspace())
    scheme = probe.partition(":")[0].lower() if ":" in probe else ""
    if scheme in BLOCKED_URL_SCHEMES:
        raise ValueError(f"{label} may not use the {scheme}: scheme.")
    return url


def clean_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def csv_bool(value, line_number):
    normalized = (value or "1").strip().lower()
    if normalized in ("1", "true", "yes", "y"):
        return 1
    if normalized in ("0", "false", "no", "n"):
        return 0
    raise ValueError(f"Row {line_number}: enabled must be 1/0, true/false, or yes/no.")


def normalize_multiline(value):
    return "\n".join(split_setting_lines(value))


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ensure_database()
    port = int(os.environ.get("PORT", "6969"))
    host = os.environ.get("HOST", "0.0.0.0")
    LOGGER.info("QuickLinks %s listening on http://%s:%s", APP_VERSION, host, port)
    ThreadingHTTPServer((host, port), AppHandler).serve_forever()


if __name__ == "__main__":
    main()
