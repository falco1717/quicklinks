import base64
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
import mimetypes
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", APP_DIR / "data"))
DB_PATH = DATA_DIR / "links.db"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip()
SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip()
SESSION_COOKIE = "quicklinks_admin_session"
SESSION_MAX_AGE = 8 * 60 * 60
APP_VERSION = "2026.08.10.001"
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


LOCATIONS = []
STANDARD_LINKS = []
GENERAL_LINKS = []
VHOSTS = []


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_database():
    global SESSION_SECRET
    DATA_DIR.mkdir(parents=True, exist_ok=True)
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
    conn = connect()
    try:
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
              enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS links (
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
              FOREIGN KEY(location_code) REFERENCES locations(code)
            );
            CREATE TABLE IF NOT EXISTS admin_users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL UNIQUE COLLATE NOCASE,
              password_hash TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at INTEGER NOT NULL
            );
            """
        )
        seeded = conn.execute("SELECT value FROM settings WHERE key = 'seeded'").fetchone()
        if not seeded:
            seed_database(conn)
            conn.execute("INSERT INTO settings(key, value) VALUES('seeded', '1')")
        apply_seed_updates(conn)
        seed_initial_admin(conn)
        conn.commit()
    finally:
        conn.close()


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
    conn = connect()
    try:
        return conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone() is None
    finally:
        conn.close()


def create_initial_admin(username, password):
    username = clean_required(username, "Username")
    encoded_password = password_hash(password or "")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone():
            raise ValueError("Initial setup has already been completed.")
        conn.execute(
            "INSERT INTO admin_users(username, password_hash, created_at) VALUES(?, ?, ?)",
            (username, encoded_password, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def seed_database(conn):
    for index, (name, code) in enumerate(LOCATIONS, start=1):
        conn.execute(
            "INSERT INTO locations(name, code, sort_order) VALUES(?, ?, ?)",
            (name, code, index),
        )
    for index, (name, url, group, description) in enumerate(GENERAL_LINKS, start=1):
        conn.execute(
            """
            INSERT INTO links(page_type, link_type, name, url, description, group_name, sort_order)
            VALUES('general', 'general', ?, ?, ?, ?, ?)
            """,
            (name, url, description, group, index),
        )
    order = 1
    for location_name, code in LOCATIONS:
        for name, host_template, description in STANDARD_LINKS:
            host = host_template.replace("{code}", code)
            conn.execute(
                """
                INSERT INTO links(page_type, location_code, link_type, name, url, description, group_name, sort_order)
                VALUES('location', ?, 'standard', ?, ?, ?, 'Standard Services', ?)
                """,
                (code, name, url_for_host(host), description, order),
            )
            order += 1
    for code, host, cluster in VHOSTS:
        conn.execute(
            """
            INSERT INTO links(page_type, location_code, link_type, name, url, description, group_name, cluster, sort_order)
            VALUES('location', ?, 'vhost', ?, ?, ?, ?, ?, ?)
            """,
            (code, host.split(".")[0], url_for_host(host), f"Virtual host in {cluster}.", cluster, cluster, order),
        )
        order += 1


def apply_seed_updates(conn):
    update_key = "general_links_2026_07_02"
    applied = conn.execute("SELECT value FROM settings WHERE key = ?", (update_key,)).fetchone()
    if applied:
        return
    for index, (name, url, group, description) in enumerate(GENERAL_LINKS, start=1):
        existing = conn.execute(
            "SELECT id FROM links WHERE page_type = 'general' AND name = ?",
            (name,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE links
                SET url = ?, group_name = ?, description = ?, link_type = 'general'
                WHERE id = ?
                """,
                (url, group, description, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO links(page_type, link_type, name, url, description, group_name, sort_order)
                VALUES('general', 'general', ?, ?, ?, ?, ?)
                """,
                (name, url, description, group, index * 10),
            )
    conn.execute("INSERT INTO settings(key, value) VALUES(?, '1')", (update_key,))


def url_for_host(host):
    return host if host.startswith(("http://", "https://")) else f"https://{host}"


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def branding_payload(conn=None):
    owns_connection = conn is None
    conn = conn or connect()
    try:
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
            branding["logo_url"] = f"/api/branding/logo?v={int((DATA_DIR / saved['logo_filename']).stat().st_mtime)}"
        return branding
    except FileNotFoundError:
        return dict(DEFAULT_BRANDING)
    finally:
        if owns_connection:
            conn.close()


def catalog_payload():
    with connect() as conn:
        locations = rows_to_dicts(
            conn.execute(
                "SELECT name, code FROM locations WHERE enabled = 1 ORDER BY sort_order, name"
            ).fetchall()
        )
        links = rows_to_dicts(
            conn.execute(
                """
                SELECT id, page_type, location_code, link_type, name, url, description, group_name, cluster, sort_order
                FROM links
                WHERE enabled = 1
                ORDER BY sort_order, id
                """
            ).fetchall()
        )
        branding = branding_payload(conn)
    return {"locations": locations, "links": links, "branding": branding, "product": PRODUCT_NOTICE}


def admin_payload():
    with connect() as conn:
        locations = rows_to_dicts(
            conn.execute(
                "SELECT id, name, code, sort_order, enabled FROM locations ORDER BY sort_order, name"
            ).fetchall()
        )
        links = rows_to_dicts(
            conn.execute(
                """
                SELECT id, page_type, location_code, link_type, name, url, description,
                       group_name, cluster, sort_order, enabled
                FROM links
                ORDER BY page_type, location_code, sort_order, id
                """
            ).fetchall()
        )
        branding = branding_payload(conn)
    return {"locations": locations, "links": links, "branding": branding, "product": PRODUCT_NOTICE}


def setting_values(conn, keys):
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT key, value FROM settings WHERE key IN ({placeholders})", keys
    ).fetchall()
    return {row["key"]: row["value"] for row in rows}


def auth_payload():
    keys = [
        "ad_enabled", "ad_server", "ad_port", "ad_ssl", "ad_domain",
        "ad_base_dn", "ad_group_dn", "ad_admin_users", "ad_admin_groups",
    ]
    with connect() as conn:
        settings = setting_values(conn, keys)
        users = rows_to_dicts(
            conn.execute(
                "SELECT id, username, enabled, created_at FROM admin_users ORDER BY username"
            ).fetchall()
        )
    return {
        "users": users,
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


def authenticate_local(username, password):
    conn = connect()
    try:
        user = conn.execute(
            "SELECT password_hash FROM admin_users WHERE username = ? AND enabled = 1",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    return bool(user and password_matches(password, user["password_hash"]))


def authenticate_ad(username, password):
    with connect() as conn:
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
        raise ValueError("Active Directory support is not installed in this container.")

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
        return authorized
    except Exception:
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
        pass
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


def csv_text(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
    return output.getvalue()


def export_csv():
    rows = []
    with connect() as conn:
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


def sign_session(expires):
    message = str(expires).encode()
    signature = hmac.new(SESSION_SECRET.encode(), message, hashlib.sha256).digest()
    return f"{expires}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def valid_session(token):
    if not token or "." not in token:
        return False
    expires_text, signature = token.split(".", 1)
    try:
        expires = int(expires_text)
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    expected = sign_session(expires).split(".", 1)[1]
    return hmac.compare_digest(signature, expected)


def cookie_value(header, name):
    if not header:
        return None
    cookie = SimpleCookie()
    cookie.load(header)
    morsel = cookie.get(name)
    return morsel.value if morsel else None


class AppHandler(SimpleHTTPRequestHandler):
    server_version = f"QuickLinks/{APP_VERSION}"

    def translate_path(self, path):
        parsed_path = urlparse(path).path
        if parsed_path == "/":
            parsed_path = "/index.html"
        if parsed_path == "/admin":
            parsed_path = "/admin.html"
        return str(APP_DIR / parsed_path.lstrip("/"))

    def end_headers(self):
        self.send_header("X-QuickLinks-Creator", PRODUCT_NOTICE["creator"])
        self.send_header("X-QuickLinks-Notice", PRODUCT_NOTICE["header_notice"])
        self.send_header("X-QuickLinks-Version", APP_VERSION)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'self'; frame-ancestors 'self'",
        )
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" and setup_required():
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/admin")
            self.end_headers()
            return
        if parsed.path == "/api/catalog":
            self.send_json(catalog_payload())
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
            self.send_json({"authenticated": self.is_admin(), "setup_required": setup_required()})
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/login":
            self.login()
            return
        if parsed.path == "/api/setup":
            self.safe_write(self.initial_setup)
            return
        if parsed.path == "/api/logout":
            self.logout()
            return
        if parsed.path == "/api/locations":
            if not self.require_admin():
                return
            self.safe_write(self.save_location)
            return
        if parsed.path == "/api/links":
            if not self.require_admin():
                return
            self.safe_write(self.save_link)
            return
        if parsed.path == "/api/branding":
            if not self.require_admin():
                return
            self.safe_write(self.save_branding)
            return
        if parsed.path == "/api/import":
            if not self.require_admin():
                return
            self.safe_write(self.import_catalog)
            return
        if parsed.path == "/api/admin-users":
            if not self.require_admin():
                return
            self.safe_write(self.save_admin_user)
            return
        if parsed.path == "/api/auth-config":
            if not self.require_admin():
                return
            self.safe_write(self.save_auth_config)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

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
        self.send_error(HTTPStatus.NOT_FOUND)

    def login(self):
        body = self.read_json()
        username = (body.get("username") or "").strip()
        password = body.get("password", "")
        if not (authenticate_local(username, password) or authenticate_ad(username, password)):
            self.send_json({"error": "Invalid username or password."}, HTTPStatus.UNAUTHORIZED)
            return
        expires = int(time.time()) + SESSION_MAX_AGE
        token = sign_session(expires)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_MAX_AGE}",
        )
        self.end_headers()
        self.wfile.write(json.dumps({"authenticated": True}).encode())

    def initial_setup(self):
        body = self.read_json()
        create_initial_admin(body.get("username"), body.get("password"))
        expires = int(time.time()) + SESSION_MAX_AGE
        token = sign_session(expires)
        response = json.dumps({"authenticated": True, "setup_required": False}).encode()
        self.send_response(HTTPStatus.CREATED)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_MAX_AGE}",
        )
        self.end_headers()
        self.wfile.write(response)

    def logout(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(json.dumps({"authenticated": False}).encode())

    def save_admin_user(self):
        body = self.read_json()
        user_id = clean_int(body.get("id"), 0)
        username = clean_required(body.get("username"), "Username")
        password = body.get("password") or ""
        enabled = 1 if body.get("enabled", True) else 0
        with connect() as conn:
            if user_id:
                existing = conn.execute("SELECT id FROM admin_users WHERE id = ?", (user_id,)).fetchone()
                if not existing:
                    raise ValueError("Local administrator not found.")
                if password:
                    conn.execute(
                        "UPDATE admin_users SET username = ?, password_hash = ?, enabled = ? WHERE id = ?",
                        (username, password_hash(password), enabled, user_id),
                    )
                else:
                    conn.execute(
                        "UPDATE admin_users SET username = ?, enabled = ? WHERE id = ?",
                        (username, enabled, user_id),
                    )
            else:
                conn.execute(
                    "INSERT INTO admin_users(username, password_hash, enabled, created_at) VALUES(?, ?, ?, ?)",
                    (username, password_hash(clean_required(password, "Password")), enabled, int(time.time())),
                )
        self.send_json(auth_payload())

    def delete_admin_user(self, raw_id):
        user_id = int(raw_id)
        with connect() as conn:
            enabled_count = conn.execute(
                "SELECT COUNT(*) AS count FROM admin_users WHERE enabled = 1"
            ).fetchone()["count"]
            target = conn.execute(
                "SELECT enabled FROM admin_users WHERE id = ?", (user_id,)
            ).fetchone()
            ad_enabled = setting_values(conn, ["ad_enabled"]).get("ad_enabled") == "1"
            if target and target["enabled"] and enabled_count <= 1 and not ad_enabled:
                raise ValueError("Keep one enabled local administrator until Active Directory is enabled.")
            conn.execute("DELETE FROM admin_users WHERE id = ?", (user_id,))
        self.send_json(auth_payload())

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
        with connect() as conn:
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
        self.send_json(auth_payload())

    def import_catalog(self):
        body = self.read_json()
        raw_csv = body.get("csv") or ""
        mode = body.get("mode") if body.get("mode") in ("merge", "replace") else "merge"
        if len(raw_csv.encode()) > 5 * 1024 * 1024:
            raise ValueError("CSV files must be smaller than 5 MB.")
        reader = csv.DictReader(io.StringIO(raw_csv.lstrip("\ufeff")))
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
                if page_type == "location" and not location_code:
                    raise ValueError(f"Row {line_number}: location links require a location_code.")
                links.append({
                    "page_type": page_type,
                    "location_code": location_code,
                    "link_type": (row.get("link_type") or ("general" if page_type == "general" else "standard")).strip(),
                    "name": clean_required(row.get("name"), f"Link name on row {line_number}"),
                    "url": clean_required(row.get("url"), f"URL on row {line_number}"),
                    "description": (row.get("description") or "").strip(),
                    "group_name": clean_required(row.get("group_name"), f"Group on row {line_number}"),
                    "cluster": (row.get("cluster") or "").strip(),
                    "sort_order": clean_int(row.get("sort_order"), 0),
                    "enabled": csv_bool(row.get("enabled"), line_number),
                })
            else:
                raise ValueError(f"Row {line_number}: record_type must be location or link.")

        available_codes = {location["code"] for location in locations}
        with connect() as conn:
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
                    INSERT INTO locations(name, code, sort_order, enabled) VALUES(:name, :code, :sort_order, :enabled)
                    ON CONFLICT(code) DO UPDATE SET name=excluded.name, sort_order=excluded.sort_order, enabled=excluded.enabled
                    """,
                    location,
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
                          group_name, cluster, sort_order, enabled)
                        VALUES(:page_type, :location_code, :link_type, :name, :url, :description,
                          :group_name, :cluster, :sort_order, :enabled)
                        """,
                        link,
                    )
        self.send_json({
            **admin_payload(),
            "imported": {"locations": len(locations), "links": len(links), "mode": mode},
        })

    def save_location(self):
        body = self.read_json()
        location_id = int(body.get("id") or 0)
        name = clean_required(body.get("name"), "Location name")
        code = clean_required(body.get("code"), "Location code").lower()
        sort_order = clean_int(body.get("sort_order"), 0)
        enabled = 1 if body.get("enabled", True) else 0
        with connect() as conn:
            if location_id:
                old = conn.execute("SELECT code FROM locations WHERE id = ?", (location_id,)).fetchone()
                conn.execute(
                    "UPDATE locations SET name = ?, code = ?, sort_order = ?, enabled = ? WHERE id = ?",
                    (name, code, sort_order, enabled, location_id),
                )
                if old and old["code"] != code:
                    conn.execute("UPDATE links SET location_code = ? WHERE location_code = ?", (code, old["code"]))
            else:
                conn.execute(
                    "INSERT INTO locations(name, code, sort_order, enabled) VALUES(?, ?, ?, ?)",
                    (name, code, sort_order, enabled),
                )
        self.send_json(admin_payload())

    def save_link(self):
        body = self.read_json()
        link_id = int(body.get("id") or 0)
        page_type = body.get("page_type") if body.get("page_type") in ("general", "location") else "general"
        location_code = (body.get("location_code") or "").strip() or None
        if page_type == "location" and not location_code:
            self.send_json({"error": "Choose a location for this link."}, HTTPStatus.BAD_REQUEST)
            return
        link_type = (body.get("link_type") or "standard").strip()
        name = clean_required(body.get("name"), "Link name")
        url = clean_required(body.get("url"), "URL")
        description = (body.get("description") or "").strip()
        group_name = clean_required(body.get("group_name"), "Group")
        cluster = (body.get("cluster") or "").strip()
        sort_order = clean_int(body.get("sort_order"), 0)
        enabled = 1 if body.get("enabled", True) else 0
        with connect() as conn:
            if link_id:
                conn.execute(
                    """
                    UPDATE links
                    SET page_type = ?, location_code = ?, link_type = ?, name = ?, url = ?, description = ?,
                        group_name = ?, cluster = ?, sort_order = ?, enabled = ?
                    WHERE id = ?
                    """,
                    (page_type, location_code, link_type, name, url, description, group_name, cluster, sort_order, enabled, link_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO links(page_type, location_code, link_type, name, url, description, group_name, cluster, sort_order, enabled)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (page_type, location_code, link_type, name, url, description, group_name, cluster, sort_order, enabled),
                )
        self.send_json(admin_payload())

    def save_branding(self):
        body = self.read_json()
        company_name = clean_required(body.get("company_name"), "Company name")
        department_title = clean_required(body.get("department_title"), "Homepage title")
        admin_title = clean_required(body.get("admin_title"), "Admin title")
        logo_data = body.get("logo_data")
        remove_logo = bool(body.get("remove_logo"))

        with connect() as conn:
            for key, value in (
                ("company_name", company_name),
                ("department_title", department_title),
                ("admin_title", admin_title),
            ):
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

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
                if len(image_bytes) > 5 * 1024 * 1024:
                    raise ValueError("The logo must be smaller than 5 MB.")
                if existing:
                    (DATA_DIR / existing["value"]).unlink(missing_ok=True)
                filename = f"branding-logo{extension}"
                (DATA_DIR / filename).write_bytes(image_bytes)
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES('logo_filename', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (filename,),
                )
        self.send_json(admin_payload())

    def send_branding_logo(self):
        with connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = 'logo_filename'").fetchone()
        if not row:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        logo_path = DATA_DIR / row["value"]
        if not logo_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = logo_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(logo_path.name)[0] or "application/octet-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def delete_location(self, raw_id):
        location_id = int(raw_id)
        with connect() as conn:
            row = conn.execute("SELECT code FROM locations WHERE id = ?", (location_id,)).fetchone()
            if not row:
                self.send_json({"error": "Location not found."}, HTTPStatus.NOT_FOUND)
                return
            conn.execute("DELETE FROM links WHERE location_code = ?", (row["code"],))
            conn.execute("DELETE FROM locations WHERE id = ?", (location_id,))
        self.send_json(admin_payload())

    def delete_link(self, raw_id):
        with connect() as conn:
            conn.execute("DELETE FROM links WHERE id = ?", (int(raw_id),))
        self.send_json(admin_payload())

    def is_admin(self):
        token = cookie_value(self.headers.get("Cookie"), SESSION_COOKIE)
        return valid_session(token)

    def require_admin(self):
        if self.is_admin():
            return True
        self.send_json({"error": "Admin login required."}, HTTPStatus.UNAUTHORIZED)
        return False

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode())
        except json.JSONDecodeError:
            return {}

    def safe_write(self, action):
        try:
            action()
        except ValueError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except sqlite3.IntegrityError as error:
            message = "That code or value already exists." if "UNIQUE" in str(error) else str(error)
            self.send_json({"error": message}, HTTPStatus.BAD_REQUEST)

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_csv(self, text, filename):
        body = text.encode("utf-8-sig")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def clean_required(value, label):
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{label} is required.")
    return cleaned


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
    ensure_database()
    port = int(os.environ.get("PORT", "6969"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"QuickLinks listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), AppHandler).serve_forever()


if __name__ == "__main__":
    main()
