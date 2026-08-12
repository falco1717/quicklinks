"""Request-level tests.

These exercise the HTTP surface rather than the helper functions, because the
serious defects this suite was written for -- a downloadable session secret, a
traversable static handler, sessions that outlived logout -- were all invisible
from inside the module.
"""

import base64
import gc
import hashlib
import http.client
import json
import socket
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import server


PASSWORD = "correct-horse"
PATCHED = (
    "DATA_DIR", "DB_PATH", "ADMIN_USERNAME", "ADMIN_PASSWORD",
    "SESSION_SECRET", "PASSWORD_ITERATIONS",
)
# The iteration count is stored inside each hash, so lowering it here only
# affects hashes these tests create. It keeps the suite from spending most of
# its runtime deliberately burning CPU in the KDF.
TEST_PASSWORD_ITERATIONS = 1_000


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.originals = {name: getattr(server, name) for name in PATCHED}
        self.created_paths = []
        server.DATA_DIR = Path(self.temp_dir.name)
        server.DB_PATH = server.DATA_DIR / "links.db"
        server.ADMIN_USERNAME = ""
        server.ADMIN_PASSWORD = ""
        server.SESSION_SECRET = ""
        server.PASSWORD_ITERATIONS = TEST_PASSWORD_ITERATIONS
        server.reset_runtime_state()
        server.ensure_database()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=10)
        for path in reversed(self.created_paths):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        for name, value in self.originals.items():
            setattr(server, name, value)
        server.reset_runtime_state()
        gc.collect()
        self.temp_dir.cleanup()

    # -- helpers ---------------------------------------------------------

    def request(self, method, path, body=None, token=None, headers=None, cookies=None):
        """Send `path` verbatim, without client-side normalization."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            head = dict(headers or {})
            payload = None
            if body is not None:
                payload = json.dumps(body).encode()
                head["Content-Type"] = "application/json"
            jar = dict(cookies or {})
            if token:
                jar[server.SESSION_COOKIE] = token
            if jar:
                head["Cookie"] = "; ".join(f"{name}={value}" for name, value in jar.items())
            connection.request(method, path, body=payload, headers=head)
            response = connection.getresponse()
            return {
                "status": response.status,
                "body": response.read(),
                "set_cookie": response.getheader("Set-Cookie"),
                "cookies": response.headers.get_all("Set-Cookie") or [],
                "location": response.getheader("Location"),
                "headers": dict(response.getheaders()),
            }
        finally:
            connection.close()

    def json_body(self, response):
        return json.loads(response["body"].decode() or "{}")

    def token_from(self, response):
        cookie = response["set_cookie"]
        self.assertIsNotNone(cookie, "expected a session cookie")
        return cookie.split(";", 1)[0].split("=", 1)[1]

    def create_admin(self, username="owner", password=PASSWORD):
        response = self.request(
            "POST", "/api/setup", {"username": username, "password": password}
        )
        self.assertEqual(response["status"], 201, response["body"])
        return self.token_from(response)

    def login(self, username="owner", password=PASSWORD):
        return self.request("POST", "/api/login", {"username": username, "password": password})

    def temp_file(self, path, content=b"canary"):
        """Create a file (and any missing parent) and remove it in tearDown.

        Refuses to touch an existing path. These fixtures live in the real
        application directory, so silently overwriting one would destroy a
        developer's own file -- and tearDown would then delete it.
        """
        path = Path(path)
        self.assertFalse(
            path.exists(),
            f"{path} already exists; a test fixture must never overwrite real data",
        )
        if not path.parent.exists():
            path.parent.mkdir(parents=True)
            self.created_paths.append(path.parent)
        path.write_bytes(content)
        self.created_paths.append(path)
        return path


class StaticFileTests(ServerTestCase):
    def test_application_files_are_served(self):
        self.create_admin()
        for path in ("/", "/index.html", "/admin", "/admin.html", "/app.js", "/styles.css"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)["status"], 200)
        self.assertEqual(self.request("GET", "/assets/quicklinks-logo.png")["status"], 200)

    def test_first_run_sends_the_homepage_to_the_setup_page(self):
        response = self.request("GET", "/")
        self.assertEqual(response["status"], 303)
        self.assertEqual(response["headers"]["Location"], "/admin")

    def test_data_directory_is_never_served(self):
        # Recreate the shipped Docker layout, where DATA_DIR sits inside the
        # application directory, and confirm nothing in it is reachable.
        # Canary names deliberately avoid links.db and .session_secret: that
        # directory is the default DATA_DIR, so a fixture using the real names
        # would clobber a locally running instance's database.
        self.temp_file(server.APP_DIR / "data" / ".quicklinks-secret-canary", b"super-secret")
        self.temp_file(server.APP_DIR / "data" / "quicklinks-canary.db", b"sqlite-canary")
        for path in (
            "/data/.quicklinks-secret-canary",
            "/data/quicklinks-canary.db",
            "/data/.session_secret",
            "/data/links.db",
            "/data/",
            "/data",
        ):
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertEqual(response["status"], 404)
                self.assertNotIn(b"super-secret", response["body"])
                self.assertNotIn(b"sqlite-canary", response["body"])

    def test_source_and_config_are_not_served(self):
        for path in (
            "/server.py",
            "/requirements.txt",
            "/Dockerfile",
            "/docker-compose.yml",
            "/VERSION",
            "/tests/test_http.py",
            "/scripts/github-sync.ps1",
            "/.dockerignore",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)["status"], 404)

    def test_directory_listings_are_not_served(self):
        for path in ("/assets/", "/scripts/", "/tests/"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)["status"], 404)

    def test_traversal_outside_the_app_directory_is_rejected(self):
        canary = self.temp_file(server.APP_DIR.parent / "quicklinks-traversal-canary.txt")
        for path in (
            f"/../{canary.name}",
            f"/..%2f{canary.name}",
            f"/%2e%2e/{canary.name}",
            f"/assets/../../{canary.name}",
            f"/index.html/../../{canary.name}",
        ):
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertEqual(response["status"], 404)
                self.assertNotIn(b"canary", response["body"])

    def test_percent_encoded_names_still_resolve(self):
        # The traversal fix must not drop unquoting the way the old override did.
        self.temp_file(server.APP_DIR / "assets" / "quicklinks test asset.txt", b"encoded-ok")
        response = self.request("GET", "/assets/quicklinks%20test%20asset.txt")
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["body"], b"encoded-ok")

    def test_security_headers_are_present(self):
        headers = self.request("GET", "/index.html")["headers"]
        policy = headers["Content-Security-Policy"]
        self.assertIn("object-src 'none'", policy)
        self.assertIn("form-action 'self'", policy)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Python", headers.get("Server", ""))


class SessionTests(ServerTestCase):
    def test_admin_endpoints_reject_anonymous_callers(self):
        self.create_admin()
        reads = ("/api/admin", "/api/auth-config", "/api/export.csv", "/api/import-template.csv")
        for path in reads:
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)["status"], 401)
        writes = (
            "/api/locations", "/api/links", "/api/branding",
            "/api/import", "/api/admin-users", "/api/auth-config",
        )
        for path in writes:
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, {})["status"], 401)
        self.assertEqual(self.request("DELETE", "/api/links/1")["status"], 401)

    def test_token_signed_with_another_secret_is_rejected(self):
        self.create_admin()
        real_secret = server.SESSION_SECRET
        try:
            server.SESSION_SECRET = "an-attacker-guess"
            forged = server.sign_session("local", "owner", int(server.time.time()), int(server.time.time()) + 3600)
        finally:
            server.SESSION_SECRET = real_secret
        self.assertEqual(self.request("GET", "/api/admin", token=forged)["status"], 401)

    def test_tampered_payload_is_rejected(self):
        token = self.create_admin()
        payload, signature = token.rsplit(".", 1)
        self.assertEqual(
            self.request("GET", "/api/admin", token=f"{payload}x.{signature}")["status"], 401
        )

    def test_session_for_an_unknown_account_is_rejected(self):
        self.create_admin()
        issued = int(server.time.time())
        ghost = server.sign_session("local", "not-a-user", issued, issued + 3600)
        self.assertEqual(self.request("GET", "/api/admin", token=ghost)["status"], 401)

    def test_ad_session_is_rejected_while_ad_is_disabled(self):
        self.create_admin()
        issued = int(server.time.time())
        token = server.sign_session("ad", "someone", issued, issued + 3600)
        self.assertEqual(self.request("GET", "/api/admin", token=token)["status"], 401)

    def test_logout_revokes_the_token_server_side(self):
        token = self.create_admin()
        self.assertEqual(self.request("GET", "/api/admin", token=token)["status"], 200)
        self.assertEqual(self.request("POST", "/api/logout", {}, token=token)["status"], 200)
        self.assertEqual(self.request("GET", "/api/admin", token=token)["status"], 401)

    def test_password_change_revokes_existing_sessions(self):
        token = self.create_admin()
        user_id = self.json_body(self.request("GET", "/api/auth-config", token=token))["users"][0]["id"]
        response = self.request(
            "POST", "/api/admin-users",
            {"id": user_id, "username": "owner", "password": "brand-new-secret", "enabled": True},
            token=token,
        )
        self.assertEqual(response["status"], 200, response["body"])
        self.assertEqual(self.request("GET", "/api/admin", token=token)["status"], 401)
        self.assertEqual(self.login(password="brand-new-secret")["status"], 200)

    def test_disabling_an_admin_revokes_their_session(self):
        owner = self.create_admin()
        self.request(
            "POST", "/api/admin-users",
            {"username": "second", "password": PASSWORD, "enabled": True},
            token=owner,
        )
        second = self.token_from(self.login("second"))
        self.assertEqual(self.request("GET", "/api/admin", token=second)["status"], 200)

        users = self.json_body(self.request("GET", "/api/auth-config", token=owner))["users"]
        second_id = next(user["id"] for user in users if user["username"] == "second")
        self.request(
            "POST", "/api/admin-users",
            {"id": second_id, "username": "second", "enabled": False},
            token=owner,
        )
        self.assertEqual(self.request("GET", "/api/admin", token=second)["status"], 401)
        self.assertEqual(self.request("GET", "/api/admin", token=owner)["status"], 200)

    def test_deleting_an_admin_revokes_their_session(self):
        owner = self.create_admin()
        self.request(
            "POST", "/api/admin-users",
            {"username": "second", "password": PASSWORD, "enabled": True},
            token=owner,
        )
        second = self.token_from(self.login("second"))
        users = self.json_body(self.request("GET", "/api/auth-config", token=owner))["users"]
        second_id = next(user["id"] for user in users if user["username"] == "second")
        self.assertEqual(
            self.request("DELETE", f"/api/admin-users/{second_id}", token=owner)["status"], 200
        )
        self.assertEqual(self.request("GET", "/api/admin", token=second)["status"], 401)

    def test_expired_token_is_rejected(self):
        self.create_admin()
        issued = int(server.time.time()) - 7200
        token = server.sign_session("local", "owner", issued, issued + 60)
        self.assertEqual(self.request("GET", "/api/admin", token=token)["status"], 401)

    def test_api_responses_are_not_cacheable(self):
        token = self.create_admin()
        for path in ("/api/session", "/api/admin", "/api/catalog"):
            with self.subTest(path=path):
                response = self.request("GET", path, token=token)
                self.assertEqual(response["headers"]["Cache-Control"], "no-store")


class SetupTests(ServerTestCase):
    def test_setup_runs_once(self):
        self.create_admin()
        response = self.request("POST", "/api/setup", {"username": "attacker", "password": PASSWORD})
        self.assertEqual(response["status"], 400)
        self.assertIn("already been completed", self.json_body(response)["error"])

    def test_setup_is_closed_once_ad_is_the_only_login_path(self):
        # Deleting the last local admin is allowed while AD is enabled. The
        # first-run page must not reopen and hand out a fresh admin account.
        token = self.create_admin()
        self.request(
            "POST", "/api/auth-config",
            {
                "enabled": True, "domain": "example.com", "ssl": True,
                "port": 636, "admin_users": "someone", "admin_groups": "",
            },
            token=token,
        )
        users = self.json_body(self.request("GET", "/api/auth-config", token=token))["users"]
        self.request("DELETE", f"/api/admin-users/{users[0]['id']}", token=token)

        server.reset_runtime_state()  # simulate a container restart
        self.assertFalse(server.setup_required())
        self.assertFalse(self.json_body(self.request("GET", "/api/session"))["setup_required"])
        response = self.request("POST", "/api/setup", {"username": "attacker", "password": PASSWORD})
        self.assertEqual(response["status"], 400)


class LoginThrottleTests(ServerTestCase):
    def test_repeated_failures_are_locked_out(self):
        self.create_admin()
        limit = server.LOGIN_LIMITS["user"]["max_failures"]
        for attempt in range(limit):
            self.assertEqual(self.login(password="wrong")["status"], 401, f"attempt {attempt}")
        response = self.login(password="wrong")
        self.assertEqual(response["status"], 429)
        self.assertTrue(int(response["headers"]["Retry-After"]) > 0)
        # The lockout holds even once the correct password is supplied.
        self.assertEqual(self.login()["status"], 429)

    def test_successful_login_clears_the_counter(self):
        self.create_admin()
        for _ in range(server.LOGIN_LIMITS["user"]["max_failures"] - 1):
            self.assertEqual(self.login(password="wrong")["status"], 401)
        self.assertEqual(self.login()["status"], 200)
        for _ in range(server.LOGIN_LIMITS["user"]["max_failures"] - 1):
            self.assertEqual(self.login(password="wrong")["status"], 401)

    def test_lockout_is_scoped_to_the_username(self):
        owner = self.create_admin()
        self.request(
            "POST", "/api/admin-users",
            {"username": "second", "password": PASSWORD, "enabled": True},
            token=owner,
        )
        for _ in range(server.LOGIN_LIMITS["user"]["max_failures"]):
            self.login(password="wrong")
        self.assertEqual(self.login()["status"], 429)
        self.assertEqual(self.login("second")["status"], 200)


class RequestLimitTests(ServerTestCase):
    def test_oversized_body_is_rejected_without_being_read(self):
        # Only the headers are sent. A server that sized the request from the
        # header would answer immediately; one that read first would block here
        # until the test timed out.
        declared = server.MAX_REQUEST_BODY + 1
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(
                f"POST /api/import HTTP/1.1\r\nHost: localhost\r\n"
                f"Content-Type: application/json\r\nContent-Length: {declared}\r\n\r\n".encode()
            )
            sock.settimeout(10)
            reply = b""
            while b"\r\n\r\n" not in reply:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                reply += chunk
        self.assertIn(b"413", reply.split(b"\r\n", 1)[0])

    def test_unauthenticated_post_with_a_body_gets_a_clean_401(self):
        # The body has to be drained before the error response, otherwise the
        # close resets the connection and the client loses the 401.
        for _ in range(5):
            response = self.request("POST", "/api/links", {"filler": "x" * 200_000})
            self.assertEqual(response["status"], 401)

    def test_malformed_content_length_is_rejected(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(
                b"POST /api/login HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nContent-Length: not-a-number\r\n\r\n"
            )
            sock.settimeout(10)
            reply = sock.recv(4096)
        self.assertIn(b"400", reply.split(b"\r\n", 1)[0])


class LinkValidationTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.create_admin()
        self.request(
            "POST", "/api/locations",
            {"name": "Example Facility", "code": "ex", "sort_order": 10, "enabled": True},
            token=self.token,
        )

    def link(self, **overrides):
        payload = {
            "page_type": "general", "link_type": "general", "name": "A link",
            "url": "https://example.com", "group_name": "Operations", "enabled": True,
        }
        payload.update(overrides)
        return self.request("POST", "/api/links", payload, token=self.token)

    def test_script_bearing_url_schemes_are_rejected(self):
        for url in (
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "java\tscript:alert(1)",
            "  javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "vbscript:msgbox(1)",
        ):
            with self.subTest(url=url):
                response = self.link(url=url)
                self.assertEqual(response["status"], 400, response["body"])

    def test_ordinary_urls_are_accepted(self):
        for url in (
            "https://example.com/path",
            "http://intranet.example.com:8080/app",
            "smb://fileserver/share",
            "mailto:helpdesk@example.com",
            "intranet.example.com:8080/app",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.link(url=url, name=url)["status"], 200)

    def test_invalid_page_type_is_rejected(self):
        response = self.link(page_type="wherever")
        self.assertEqual(response["status"], 400)
        self.assertIn("general or location", self.json_body(response)["error"])

    def test_general_links_do_not_keep_a_location_code(self):
        self.assertEqual(self.link(page_type="general", location_code="ex")["status"], 200)
        links = self.json_body(self.request("GET", "/api/admin", token=self.token))["links"]
        self.assertEqual([link["location_code"] for link in links], [None])

    def test_unknown_location_code_is_rejected(self):
        response = self.link(page_type="location", location_code="nope")
        self.assertEqual(response["status"], 400)
        self.assertIn("does not exist", self.json_body(response)["error"])

    def test_renaming_a_location_carries_its_links(self):
        self.assertEqual(self.link(page_type="location", location_code="ex")["status"], 200)
        payload = self.json_body(self.request("GET", "/api/admin", token=self.token))
        location_id = payload["locations"][0]["id"]
        response = self.request(
            "POST", "/api/locations",
            {"id": location_id, "name": "Example Facility", "code": "exx", "sort_order": 10, "enabled": True},
            token=self.token,
        )
        self.assertEqual(response["status"], 200, response["body"])
        self.assertEqual([link["location_code"] for link in self.json_body(response)["links"]], ["exx"])

    def test_deleting_a_location_removes_its_links(self):
        self.assertEqual(self.link(page_type="location", location_code="ex")["status"], 200)
        payload = self.json_body(self.request("GET", "/api/admin", token=self.token))
        response = self.request(
            "DELETE", f"/api/locations/{payload['locations'][0]['id']}", token=self.token
        )
        self.assertEqual(response["status"], 200)
        self.assertEqual(self.json_body(response)["links"], [])

    def test_non_numeric_ids_do_not_crash_the_handler(self):
        for path in ("/api/links/abc", "/api/locations/abc", "/api/admin-users/abc"):
            with self.subTest(path=path):
                self.assertIn(
                    self.request("DELETE", path, token=self.token)["status"], (200, 400)
                )


class BrandingTests(ServerTestCase):
    def test_missing_logo_file_keeps_the_rest_of_the_branding(self):
        token = self.create_admin()
        response = self.request(
            "POST", "/api/branding",
            {
                "company_name": "Acme", "department_title": "Links",
                "admin_title": "Console",
                # 1x1 transparent PNG
                "logo_data": "data:image/png;base64,"
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkAAAABgAD"
                "j0dxjwAAAABJRU5ErkJggg==",
            },
            token=token,
        )
        self.assertEqual(response["status"], 200, response["body"])
        self.assertIn("/api/branding/logo", self.json_body(response)["branding"]["logo_url"])

        (server.DATA_DIR / "branding-logo.png").unlink()
        branding = self.json_body(self.request("GET", "/api/catalog"))["branding"]
        self.assertEqual(branding["company_name"], "Acme")
        self.assertEqual(branding["department_title"], "Links")
        self.assertEqual(branding["logo_url"], server.DEFAULT_BRANDING["logo_url"])
        self.assertEqual(self.request("GET", "/api/branding/logo")["status"], 404)

    def test_non_image_logo_is_rejected(self):
        token = self.create_admin()
        response = self.request(
            "POST", "/api/branding",
            {
                "company_name": "Acme", "department_title": "Links", "admin_title": "Console",
                "logo_data": "data:image/gif;base64,AAAA",
            },
            token=token,
        )
        self.assertEqual(response["status"], 400)


class ImportTests(ServerTestCase):
    HEADER = ",".join(server.CSV_FIELDS)

    def test_import_rejects_script_urls(self):
        token = self.create_admin()
        rows = [
            self.HEADER,
            "location,Example,ex,,,,,,,,10,1",
            "link,Bad,,general,,general,javascript:alert(1),,Operations,,10,1",
        ]
        response = self.request(
            "POST", "/api/import", {"csv": "\r\n".join(rows), "mode": "merge"}, token=token
        )
        self.assertEqual(response["status"], 400)
        self.assertIn("javascript", self.json_body(response)["error"])
        # The whole import is rolled back, so the valid location did not land.
        self.assertEqual(self.json_body(self.request("GET", "/api/admin", token=token))["locations"], [])

    def test_replace_mode_swaps_the_catalog(self):
        # Worth covering explicitly: the location foreign key now cascades on
        # delete, so the order of the wipe in replace mode matters.
        token = self.create_admin()
        first = [
            self.HEADER,
            "location,Old Site,old,,,,,,,,10,1",
            "link,Old Service,,location,old,standard,https://old.example.com,,Standard Services,,10,1",
        ]
        self.assertEqual(
            self.request("POST", "/api/import", {"csv": "\r\n".join(first), "mode": "merge"}, token=token)["status"],
            200,
        )
        second = [
            self.HEADER,
            "location,New Site,new,,,,,,,,10,1",
            "link,New Service,,location,new,standard,https://new.example.com,,Standard Services,,10,1",
        ]
        response = self.request(
            "POST", "/api/import", {"csv": "\r\n".join(second), "mode": "replace"}, token=token
        )
        self.assertEqual(response["status"], 200, response["body"])
        payload = self.json_body(response)
        self.assertEqual([location["code"] for location in payload["locations"]], ["new"])
        self.assertEqual([(link["name"], link["location_code"]) for link in payload["links"]],
                         [("New Service", "new")])

    def test_round_trip_import_and_export(self):
        token = self.create_admin()
        rows = [
            self.HEADER,
            "location,Example,ex,,,,,,,,10,1",
            "link,Service,,location,ex,standard,https://svc.example.com,Desc,Standard Services,,10,1",
        ]
        response = self.request(
            "POST", "/api/import", {"csv": "\r\n".join(rows), "mode": "merge"}, token=token
        )
        self.assertEqual(response["status"], 200, response["body"])
        self.assertEqual(self.json_body(response)["imported"], {"locations": 1, "links": 1, "mode": "merge"})
        export = self.request("GET", "/api/export.csv", token=token)
        self.assertEqual(export["status"], 200)
        self.assertIn(b"https://svc.example.com", export["body"])


TENANT_ID = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "66666666-7777-8888-9999-aaaaaaaaaaaa"
REDIRECT_URI = "https://links.example.com/api/auth/entra/callback"


def entra_settings(**overrides):
    payload = {
        "enabled": True,
        "tenant_id": TENANT_ID,
        "client_id": CLIENT_ID,
        "client_secret": "a-client-secret",
        "redirect_uri": REDIRECT_URI,
        "admin_users": "owner@example.com",
        "admin_groups": "",
        "admin_roles": "",
    }
    payload.update(overrides)
    return payload


def id_token(**claims):
    """Build an unsigned ID token. The server reads claims from the payload."""
    payload = {
        "iss": f"{server.ENTRA_AUTHORITY}/{TENANT_ID}/v2.0",
        "aud": CLIENT_ID,
        "tid": TENANT_ID,
        "exp": int(server.time.time()) + 600,
        "nbf": int(server.time.time()) - 60,
        "preferred_username": "owner@example.com",
    }
    payload.update(claims)
    encode = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return ".".join([
        encode(b'{"alg":"RS256","typ":"JWT"}'),
        encode(json.dumps(payload).encode()),
        encode(b"not-a-real-signature"),
    ])


class EntraConfigTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.create_admin()

    def save(self, **overrides):
        return self.request(
            "POST", "/api/entra-config", entra_settings(**overrides), token=self.token
        )

    def test_valid_settings_are_accepted_and_enable_the_button(self):
        self.assertEqual(self.save()["status"], 200)
        self.assertTrue(self.json_body(self.request("GET", "/api/session"))["entra_available"])

    def test_client_secret_is_never_returned(self):
        self.save()
        payload = self.json_body(self.request("GET", "/api/auth-config", token=self.token))
        self.assertNotIn("client_secret", payload["entra"])
        self.assertTrue(payload["entra"]["client_secret_set"])
        self.assertNotIn(b"a-client-secret", self.request("GET", "/api/auth-config", token=self.token)["body"])

    def test_blank_secret_keeps_the_stored_one(self):
        self.save()
        self.assertEqual(self.save(client_secret="", admin_users="someone@example.com")["status"], 200)
        payload = self.json_body(self.request("GET", "/api/auth-config", token=self.token))
        self.assertTrue(payload["entra"]["client_secret_set"])
        self.assertEqual(payload["entra"]["admin_users"], "someone@example.com")

    def test_incomplete_settings_are_rejected(self):
        cases = [
            ({"tenant_id": "contoso.com"}, "Directory (tenant) ID"),
            ({"client_id": "not-a-guid"}, "Application (client) ID"),
            ({"client_secret": ""}, "client secret is required"),
            ({"redirect_uri": "http://links.example.com/api/auth/entra/callback"}, "must use https"),
            ({"redirect_uri": "https://links.example.com/wrong"}, "must end with"),
            ({"admin_users": "", "admin_groups": "", "admin_roles": ""}, "at least one"),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                response = self.save(**overrides)
                self.assertEqual(response["status"], 400)
                self.assertIn(expected, self.json_body(response)["error"])

    def test_settings_may_be_saved_while_disabled_without_validation(self):
        response = self.save(enabled=False, tenant_id="", client_id="", client_secret="", redirect_uri="")
        self.assertEqual(response["status"], 200)
        self.assertFalse(self.json_body(self.request("GET", "/api/session"))["entra_available"])

    def test_config_endpoint_requires_admin(self):
        self.assertEqual(self.request("POST", "/api/entra-config", entra_settings())["status"], 401)


class EntraFlowTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.admin_token = self.create_admin()
        self.assertEqual(
            self.request("POST", "/api/entra-config", entra_settings(), token=self.admin_token)["status"],
            200,
        )

    def start(self):
        response = self.request("GET", "/api/auth/entra/start")
        self.assertEqual(response["status"], 303)
        flow = next(
            value.split(";", 1)[0].split("=", 1)[1]
            for value in response["cookies"]
            if value.startswith(f"{server.ENTRA_FLOW_COOKIE}=")
        )
        return response, flow

    def callback(self, query, flow=None):
        cookies = {server.ENTRA_FLOW_COOKIE: flow} if flow else None
        return self.request("GET", f"/api/auth/entra/callback?{query}", cookies=cookies)

    def test_start_redirects_to_microsoft_with_pkce(self):
        response, flow = self.start()
        target = urlparse(response["location"])
        params = {key: value[0] for key, value in parse_qs(target.query).items()}
        self.assertEqual(f"{target.scheme}://{target.netloc}", server.ENTRA_AUTHORITY)
        self.assertEqual(target.path, f"/{TENANT_ID}/oauth2/v2.0/authorize")
        self.assertEqual(params["client_id"], CLIENT_ID)
        self.assertEqual(params["response_type"], "code")
        self.assertEqual(params["redirect_uri"], REDIRECT_URI)
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertTrue(params["code_challenge"] and params["state"] and params["nonce"])

        # The challenge must be the S256 hash of the verifier held in the cookie,
        # never the verifier itself.
        pending = server.verify_payload(flow)
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(pending["verifier"].encode()).digest()
        ).decode().rstrip("=")
        self.assertEqual(params["code_challenge"], expected)
        self.assertNotIn(pending["verifier"], response["location"])
        self.assertEqual(params["state"], pending["state"])

    def test_start_is_refused_when_entra_is_disabled(self):
        self.request(
            "POST", "/api/entra-config", entra_settings(enabled=False), token=self.admin_token
        )
        response = self.request("GET", "/api/auth/entra/start")
        self.assertEqual(response["status"], 303)
        self.assertEqual(response["location"], "/admin?entra_error=config")

    def test_callback_rejects_a_missing_or_mismatched_state(self):
        _, flow = self.start()
        for query, cookie in (
            ("code=abc&state=wrong-state", flow),
            ("code=abc", flow),
            ("code=abc&state=anything", None),
            ("state=onlystate", flow),
        ):
            with self.subTest(query=query, has_cookie=bool(cookie)):
                response = self.callback(query, cookie)
                self.assertEqual(response["location"], "/admin?entra_error=state")

    def test_callback_surfaces_a_microsoft_error(self):
        _, flow = self.start()
        response = self.callback("error=access_denied&error_description=user+cancelled", flow)
        self.assertEqual(response["location"], "/admin?entra_error=denied")

    def test_callback_never_issues_a_session_on_failure(self):
        _, flow = self.start()
        response = self.callback("code=abc&state=wrong", flow)
        self.assertNotIn(
            server.SESSION_COOKIE,
            " ".join(value.split("=", 1)[0] for value in response["cookies"]),
        )

    def test_successful_sign_in_issues_an_entra_session(self):
        pending = None

        def fake_exchange(config, code, verifier):
            # Prove the real verifier reaches the token endpoint.
            self.assertEqual(verifier, pending["verifier"])
            self.assertEqual(code, "the-auth-code")
            return {"id_token": id_token(nonce=pending["nonce"])}

        response, flow = self.start()
        pending = server.verify_payload(flow)
        original = server.exchange_entra_code
        server.exchange_entra_code = fake_exchange
        try:
            result = self.callback(f"code=the-auth-code&state={pending['state']}", flow)
        finally:
            server.exchange_entra_code = original

        self.assertEqual(result["status"], 303)
        self.assertEqual(result["location"], "/admin")
        session = next(
            value.split(";", 1)[0].split("=", 1)[1]
            for value in result["cookies"]
            if value.startswith(f"{server.SESSION_COOKIE}=")
        )
        self.assertEqual(server.parse_session(session)["source"], "entra")
        self.assertEqual(server.parse_session(session)["username"], "owner@example.com")
        self.assertEqual(self.request("GET", "/api/admin", token=session)["status"], 200)

        # Disabling Entra invalidates the session it issued.
        self.request(
            "POST", "/api/entra-config", entra_settings(enabled=False), token=self.admin_token
        )
        self.assertEqual(self.request("GET", "/api/admin", token=session)["status"], 401)

    def test_changing_entra_settings_signs_out_earlier_entra_sessions(self):
        self.request(
            "POST", "/api/entra-config",
            entra_settings(admin_users="someone-else@example.com"),
            token=self.admin_token,
        )
        # Pinned to the stored epoch rather than wall-clock offsets, so the
        # cutoff is asserted exactly instead of depending on test timing.
        with server.db() as conn:
            epoch = server.source_config_epoch(conn, "entra")
        self.assertTrue(epoch > 0)
        stale = server.sign_session("entra", "owner@example.com", epoch - 1, epoch + 3600)
        fresh = server.sign_session("entra", "owner@example.com", epoch, epoch + 3600)
        self.assertEqual(self.request("GET", "/api/admin", token=stale)["status"], 401)
        self.assertEqual(self.request("GET", "/api/admin", token=fresh)["status"], 200)

    def test_changing_settings_does_not_sign_out_local_administrators(self):
        # An administrator configuring a directory must not log themselves out.
        self.assertEqual(self.request("GET", "/api/admin", token=self.admin_token)["status"], 200)
        self.request(
            "POST", "/api/entra-config",
            entra_settings(admin_users="someone-else@example.com"),
            token=self.admin_token,
        )
        self.assertEqual(self.request("GET", "/api/admin", token=self.admin_token)["status"], 200)

    def test_entra_session_logout_revokes_it(self):
        issued = int(server.time.time())
        session = server.sign_session("entra", "owner@example.com", issued, issued + 3600)
        self.assertEqual(self.request("POST", "/api/logout", {}, token=session)["status"], 200)
        self.assertEqual(self.request("GET", "/api/admin", token=session)["status"], 401)

    def test_last_local_admin_may_be_removed_once_entra_is_enabled(self):
        users = self.json_body(self.request("GET", "/api/auth-config", token=self.admin_token))["users"]
        response = self.request(
            "DELETE", f"/api/admin-users/{users[0]['id']}", token=self.admin_token
        )
        self.assertEqual(response["status"], 200, response["body"])
        server.reset_runtime_state()  # simulate a restart
        self.assertFalse(server.setup_required())
        self.assertEqual(
            self.request("POST", "/api/setup", {"username": "attacker", "password": PASSWORD})["status"],
            400,
        )


class EntraClaimTests(unittest.TestCase):
    """Claim validation, checked directly so each rule has its own case."""

    def setUp(self):
        self.config = {"tenant_id": TENANT_ID, "client_id": CLIENT_ID}
        self.nonce = "the-nonce"

    def claims(self, **overrides):
        payload = json.loads(
            base64.urlsafe_b64decode(
                id_token(nonce=self.nonce).split(".")[1] + "=="
            )
        )
        payload.update(overrides)
        return payload

    def test_valid_claims_pass(self):
        server.validate_entra_claims(self.claims(), self.config, self.nonce)

    def test_each_rule_rejects(self):
        other_tenant = "99999999-9999-9999-9999-999999999999"
        cases = [
            ({"tid": other_tenant, "iss": f"{server.ENTRA_AUTHORITY}/{other_tenant}/v2.0"}, "different Microsoft tenant"),
            ({"tid": "contoso.com"}, "different Microsoft tenant"),
            ({"iss": "https://evil.example.com/v2.0"}, "issuer was not recognised"),
            ({"iss": f"{server.ENTRA_AUTHORITY}/{TENANT_ID}/v1.0"}, "issuer was not recognised"),
            ({"aud": "another-client"}, "different application"),
            ({"nonce": "replayed"}, "could not be matched"),
            ({"exp": int(server.time.time()) - 3600}, "expired"),
            ({"nbf": int(server.time.time()) + 3600}, "not valid yet"),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, expected):
                    server.validate_entra_claims(self.claims(**overrides), self.config, self.nonce)

    def test_audience_may_be_a_list(self):
        server.validate_entra_claims(
            self.claims(aud=["other", CLIENT_ID]), self.config, self.nonce
        )

    def test_missing_nonce_on_our_side_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "could not be matched"):
            server.validate_entra_claims(self.claims(), self.config, "")

    def test_unreadable_token_is_rejected(self):
        for value in (None, "", "not-a-jwt", "a.b", "a.!!!.c"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "readable ID token"):
                    server.decode_jwt_claims(value)


class EntraAuthorizationTests(unittest.TestCase):
    def config(self, **overrides):
        base = {"admin_users": "", "admin_groups": "", "admin_roles": ""}
        base.update(overrides)
        return base

    def test_user_match_is_case_insensitive_across_name_claims(self):
        config = self.config(admin_users="Owner@Example.COM")
        for key in ("preferred_username", "upn", "email", "unique_name"):
            with self.subTest(claim=key):
                self.assertTrue(server.entra_authorized({key: "owner@example.com"}, config))

    def test_group_object_ids_authorize(self):
        group = "abcdef01-2345-6789-abcd-ef0123456789"
        config = self.config(admin_groups=group.upper())
        self.assertTrue(server.entra_authorized({"groups": [group]}, config))
        self.assertFalse(server.entra_authorized({"groups": ["another-group"]}, config))

    def test_app_roles_authorize(self):
        config = self.config(admin_roles="QuickLinks.Admin")
        self.assertTrue(server.entra_authorized({"roles": ["quicklinks.admin"]}, config))
        self.assertFalse(server.entra_authorized({"roles": ["QuickLinks.Reader"]}, config))

    def test_nothing_configured_denies_everyone(self):
        self.assertFalse(
            server.entra_authorized({"preferred_username": "owner@example.com"}, self.config())
        )

    def test_group_overage_is_denied_and_logged(self):
        config = self.config(admin_groups="abcdef01-2345-6789-abcd-ef0123456789")
        claims = {
            "preferred_username": "owner@example.com",
            "_claim_names": {"groups": "src1"},
            "_claim_sources": {"src1": {"endpoint": "https://graph.microsoft.com/..."}},
        }
        with self.assertLogs(server.LOGGER, level="WARNING") as captured:
            self.assertFalse(server.entra_authorized(claims, config))
        self.assertIn("overage", " ".join(captured.output))


class DepartmentTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.create_admin()
        self.departments = {"general": 1}
        for name, public, order in (("IT", False, 20), ("Facilities Team", True, 30)):
            response = self.request(
                "POST", "/api/departments",
                {"name": name, "public": public, "sort_order": order},
                token=self.admin,
            )
            self.assertEqual(response["status"], 200, response["body"])
            self.departments = {d["slug"]: d["id"] for d in self.json_body(response)["departments"]}

    def add_content(self, slug, location_name, code):
        self.request("POST", "/api/locations",
                     {"name": location_name, "code": code, "department_id": self.departments[slug]},
                     token=self.admin)
        self.request("POST", "/api/links",
                     {"page_type": "location", "location_code": code, "name": f"{code} tool",
                      "url": f"https://{code}.example.com", "group_name": "Std"}, token=self.admin)
        self.request("POST", "/api/links",
                     {"page_type": "general", "name": f"{slug} general", "url": "https://g.example.com",
                      "group_name": "Ops", "department_id": self.departments[slug]}, token=self.admin)

    def make_viewer(self, username, slugs, password="viewerpass1"):
        response = self.request(
            "POST", "/api/admin-users",
            {"username": username, "password": password, "is_admin": False,
             "department_ids": [self.departments[s] for s in slugs]},
            token=self.admin,
        )
        self.assertEqual(response["status"], 200, response["body"])
        login = self.request("POST", "/api/login", {"username": username, "password": password})
        self.assertEqual(login["status"], 200, login["body"])
        return self.token_from(login)

    def catalog(self, token=None):
        return self.json_body(self.request("GET", "/api/catalog", token=token))

    # -- migration and defaults ------------------------------------------

    def test_a_default_department_always_exists(self):
        # Every location and link needs one, so with none defined the portal
        # would serve an empty catalog on a brand new install.
        payload = self.json_body(self.request("GET", "/api/admin", token=self.admin))
        self.assertTrue(payload["departments"])
        self.assertEqual(payload["departments"][0]["slug"], "general")
        self.assertEqual(payload["departments"][0]["public"], 1)

    def test_records_created_without_a_department_land_in_the_default(self):
        self.request("POST", "/api/links",
                     {"page_type": "general", "name": "Unassigned", "url": "https://u.example.com",
                      "group_name": "Ops"}, token=self.admin)
        links = self.json_body(self.request("GET", "/api/admin", token=self.admin))["links"]
        self.assertEqual([l["department_id"] for l in links], [self.departments["general"]])
        # And it is visible to an anonymous visitor, as it was before departments.
        self.assertEqual(len(self.catalog()["links"]), 1)

    def test_slug_is_derived_from_the_name(self):
        self.assertIn("facilities-team", self.departments)

    # -- scoping ---------------------------------------------------------

    def test_anonymous_visitors_see_only_public_departments(self):
        self.add_content("it", "Datacentre", "dc")
        self.add_content("facilities-team", "Plant", "pl")
        catalog = self.catalog()
        self.assertEqual(sorted(d["slug"] for d in catalog["departments"]),
                         ["facilities-team", "general"])
        self.assertNotIn("dc", [l["location_code"] for l in catalog["links"]])
        self.assertFalse(catalog["viewer"]["authenticated"])

    def test_viewer_sees_only_assigned_departments(self):
        self.add_content("it", "Datacentre", "dc")
        self.add_content("facilities-team", "Plant", "pl")
        token = self.make_viewer("it-viewer", ["it"])
        catalog = self.catalog(token)
        self.assertEqual([d["slug"] for d in catalog["departments"]], ["it"])
        self.assertTrue(catalog["viewer"]["authenticated"])
        self.assertFalse(catalog["viewer"]["is_admin"])
        # The public department is not implicitly added on top of the assignment.
        self.assertNotIn("general", [d["slug"] for d in catalog["departments"]])

    def test_viewer_with_several_departments_receives_all_of_them(self):
        """The toggle case: more than one department means a choice to make."""
        self.add_content("it", "Datacentre", "dc")
        self.add_content("facilities-team", "Plant", "pl")
        token = self.make_viewer("both", ["it", "facilities-team"])
        self.assertEqual(sorted(d["slug"] for d in self.catalog(token)["departments"]),
                         ["facilities-team", "it"])

    def test_admin_sees_every_department(self):
        self.add_content("it", "Datacentre", "dc")
        catalog = self.catalog(self.admin)
        self.assertEqual(sorted(d["slug"] for d in catalog["departments"]),
                         ["facilities-team", "general", "it"])
        self.assertTrue(catalog["viewer"]["is_admin"])

    def test_a_viewer_is_not_an_administrator(self):
        token = self.make_viewer("it-viewer", ["it"])
        for method, path, body in (
            ("GET", "/api/admin", None),
            ("GET", "/api/auth-config", None),
            ("GET", "/api/export.csv", None),
            ("POST", "/api/departments", {"name": "Sneaky"}),
            ("POST", "/api/links", {"page_type": "general", "name": "x", "url": "https://x.example.com",
                                    "group_name": "g"}),
            ("POST", "/api/portal-settings", {"require_login": True}),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request(method, path, body, token=token)["status"], 401)

    # -- location links inherit ------------------------------------------

    def test_moving_a_location_moves_its_links(self):
        """A location link must never sit in a department that cannot see its location."""
        self.add_content("it", "Datacentre", "dc")
        payload = self.json_body(self.request("GET", "/api/admin", token=self.admin))
        location_id = next(l["id"] for l in payload["locations"] if l["code"] == "dc")
        self.request("POST", "/api/locations",
                     {"id": location_id, "name": "Datacentre", "code": "dc",
                      "department_id": self.departments["general"]}, token=self.admin)
        links = self.json_body(self.request("GET", "/api/admin", token=self.admin))["links"]
        moved = [l for l in links if l["page_type"] == "location" and l["location_code"] == "dc"]
        self.assertEqual([l["department_id"] for l in moved], [self.departments["general"]])

    def test_location_link_ignores_a_submitted_department(self):
        self.request("POST", "/api/locations",
                     {"name": "Datacentre", "code": "dc", "department_id": self.departments["it"]},
                     token=self.admin)
        self.request("POST", "/api/links",
                     {"page_type": "location", "location_code": "dc", "name": "tool",
                      "url": "https://t.example.com", "group_name": "Std",
                      "department_id": self.departments["facilities-team"]}, token=self.admin)
        links = self.json_body(self.request("GET", "/api/admin", token=self.admin))["links"]
        self.assertEqual([l["department_id"] for l in links], [self.departments["it"]])

    # -- require_login ---------------------------------------------------

    def test_require_login_blocks_anonymous_access(self):
        self.add_content("facilities-team", "Plant", "pl")
        viewer = self.make_viewer("fac", ["facilities-team"])
        self.assertTrue(self.catalog()["departments"])

        self.assertEqual(
            self.request("POST", "/api/portal-settings", {"require_login": True},
                         token=self.admin)["status"], 200)
        anonymous = self.catalog()
        self.assertEqual(anonymous["departments"], [])
        self.assertEqual(anonymous["links"], [])
        self.assertTrue(anonymous["viewer"]["requires_login"])
        # A signed-in viewer is unaffected.
        self.assertTrue(self.catalog(viewer)["departments"])

    def test_require_login_can_be_turned_back_off(self):
        self.request("POST", "/api/portal-settings", {"require_login": True}, token=self.admin)
        self.request("POST", "/api/portal-settings", {"require_login": False}, token=self.admin)
        self.assertTrue(self.catalog()["departments"])
        self.assertFalse(self.catalog()["viewer"]["requires_login"])

    # -- guard rails -----------------------------------------------------

    def test_viewer_must_have_at_least_one_department(self):
        response = self.request("POST", "/api/admin-users",
                                {"username": "nobody", "password": "viewerpass9",
                                 "is_admin": False, "department_ids": []}, token=self.admin)
        self.assertEqual(response["status"], 400)
        self.assertIn("could not see anything", self.json_body(response)["error"])

    def test_department_holding_content_cannot_be_deleted(self):
        self.add_content("it", "Datacentre", "dc")
        response = self.request("DELETE", f"/api/departments/{self.departments['it']}", token=self.admin)
        self.assertEqual(response["status"], 400)
        self.assertIn("still holds", self.json_body(response)["error"])

    def test_empty_department_can_be_deleted(self):
        response = self.request("DELETE", f"/api/departments/{self.departments['it']}", token=self.admin)
        self.assertEqual(response["status"], 200, response["body"])
        self.assertNotIn("it", [d["slug"] for d in self.json_body(response)["departments"]])

    def test_last_department_cannot_be_deleted(self):
        for slug in ("it", "facilities-team"):
            self.request("DELETE", f"/api/departments/{self.departments[slug]}", token=self.admin)
        response = self.request("DELETE", f"/api/departments/{self.departments['general']}", token=self.admin)
        self.assertEqual(response["status"], 400)
        self.assertIn("at least one department", self.json_body(response)["error"])

    def test_unknown_department_is_rejected(self):
        for body in (
            {"page_type": "general", "name": "x", "url": "https://x.example.com",
             "group_name": "g", "department_id": 99999},
            {"name": "Nowhere", "code": "nw", "department_id": 99999},
        ):
            with self.subTest(body=body):
                path = "/api/links" if "page_type" in body else "/api/locations"
                response = self.request("POST", path, body, token=self.admin)
                self.assertEqual(response["status"], 400)
                self.assertIn("does not exist", self.json_body(response)["error"])

    def test_disabled_department_is_hidden_from_everyone(self):
        self.add_content("facilities-team", "Plant", "pl")
        self.request("POST", "/api/departments",
                     {"id": self.departments["facilities-team"], "name": "Facilities Team",
                      "public": True, "enabled": False}, token=self.admin)
        self.assertNotIn("facilities-team", [d["slug"] for d in self.catalog()["departments"]])
        self.assertNotIn("facilities-team", [d["slug"] for d in self.catalog(self.admin)["departments"]])


if __name__ == "__main__":
    unittest.main()
