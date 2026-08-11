"""Request-level tests.

These exercise the HTTP surface rather than the helper functions, because the
serious defects this suite was written for -- a downloadable session secret, a
traversable static handler, sessions that outlived logout -- were all invisible
from inside the module.
"""

import gc
import http.client
import json
import socket
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

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

    def request(self, method, path, body=None, token=None, headers=None):
        """Send `path` verbatim, without client-side normalization."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            head = dict(headers or {})
            payload = None
            if body is not None:
                payload = json.dumps(body).encode()
                head["Content-Type"] = "application/json"
            if token:
                head["Cookie"] = f"{server.SESSION_COOKIE}={token}"
            connection.request(method, path, body=payload, headers=head)
            response = connection.getresponse()
            return {
                "status": response.status,
                "body": response.read(),
                "set_cookie": response.getheader("Set-Cookie"),
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
        """Create a file (and any missing parent) and remove it in tearDown."""
        path = Path(path)
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
        self.temp_file(server.APP_DIR / "data" / ".session_secret", b"super-secret")
        self.temp_file(server.APP_DIR / "data" / "links.db", b"sqlite")
        for path in ("/data/.session_secret", "/data/links.db", "/data/", "/data"):
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertEqual(response["status"], 404)
                self.assertNotIn(b"super-secret", response["body"])

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


if __name__ == "__main__":
    unittest.main()
