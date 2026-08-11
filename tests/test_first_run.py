import gc
import os
import tempfile
import unittest
from pathlib import Path

import server


class FirstRunTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.originals = (
            server.DATA_DIR,
            server.DB_PATH,
            server.ADMIN_USERNAME,
            server.ADMIN_PASSWORD,
            server.SESSION_SECRET,
        )
        server.DATA_DIR = Path(self.temp_dir.name)
        server.DB_PATH = server.DATA_DIR / "links.db"
        server.ADMIN_USERNAME = ""
        server.ADMIN_PASSWORD = ""
        server.SESSION_SECRET = ""
        # setup_required() caches its answer for the life of the process, so it
        # has to be cleared or one case leaks into the next.
        server.reset_runtime_state()

    def tearDown(self):
        (
            server.DATA_DIR,
            server.DB_PATH,
            server.ADMIN_USERNAME,
            server.ADMIN_PASSWORD,
            server.SESSION_SECRET,
        ) = self.originals
        server.reset_runtime_state()
        gc.collect()
        self.temp_dir.cleanup()

    def test_blank_environment_requires_setup_and_persists_secret(self):
        server.ensure_database()
        self.assertTrue(server.setup_required())
        first_secret = server.SESSION_SECRET
        self.assertTrue(first_secret)
        self.assertTrue((server.DATA_DIR / ".session_secret").exists())

        server.SESSION_SECRET = ""
        server.ensure_database()
        self.assertEqual(first_secret, server.SESSION_SECRET)

    def test_setup_creates_one_admin_and_rejects_second_attempt(self):
        server.ensure_database()
        server.create_initial_admin("owner", "1234567")
        self.assertFalse(server.setup_required())
        self.assertTrue(server.authenticate_local("owner", "1234567"))
        with self.assertRaisesRegex(ValueError, "already been completed"):
            server.create_initial_admin("attacker", "1234567")

    def test_environment_credentials_seed_first_admin(self):
        server.ADMIN_USERNAME = "provisioned-user"
        server.ADMIN_PASSWORD = "1234567"
        server.ensure_database()
        self.assertFalse(server.setup_required())
        self.assertTrue(server.authenticate_local("provisioned-user", "1234567"))

    def test_partial_environment_credentials_fail_startup(self):
        server.ADMIN_USERNAME = "owner"
        with self.assertRaisesRegex(RuntimeError, "must be provided together"):
            server.ensure_database()

    @unittest.skipIf(os.name == "nt", "relies on POSIX directory permissions")
    def test_unwritable_data_directory_reports_the_real_problem(self):
        """The failure mode that crash-looped a real deployment.

        Dropping all capabilities strips root's CAP_DAC_OVERRIDE, so a
        container running as root still cannot write a directory whose mode
        forbids it. That used to surface as a bare
        `sqlite3.OperationalError: attempt to write a readonly database`.
        """
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root bypasses directory permissions")
        os.chmod(server.DATA_DIR, 0o500)
        try:
            with self.assertRaises(RuntimeError) as caught:
                server.ensure_database()
            message = str(caught.exception)
            self.assertIn(str(server.DATA_DIR), message)
            self.assertIn("not writable", message)
            self.assertIn("DATA_DIR", message)
            self.assertIn("capabilities", message)
            self.assertNotIn("readonly database", message)
        finally:
            os.chmod(server.DATA_DIR, 0o700)

    @unittest.skipIf(os.name == "nt", "relies on POSIX file permissions")
    def test_unwritable_database_file_reports_the_real_problem(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root bypasses file permissions")
        server.ensure_database()
        os.chmod(server.DB_PATH, 0o400)
        try:
            with self.assertRaises(RuntimeError) as caught:
                server.ensure_database()
            self.assertIn(str(server.DB_PATH), str(caught.exception))
        finally:
            os.chmod(server.DB_PATH, 0o600)

    def test_data_directory_probe_leaves_nothing_behind(self):
        server.ensure_database()
        leftovers = [p.name for p in server.DATA_DIR.iterdir() if "probe" in p.name]
        self.assertEqual(leftovers, [])

    def test_password_minimum_is_seven(self):
        server.ensure_database()
        with self.assertRaisesRegex(ValueError, "at least 7"):
            server.create_initial_admin("owner", "123456")


if __name__ == "__main__":
    unittest.main()
