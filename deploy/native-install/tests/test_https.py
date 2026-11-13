"""Regression tests for native installation defaults, upgrades and explicit TLS choices."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(INSTALLER), str(INSTALLER.parents[1] / "backend")]

from configure_https import configure_tls, update_tls_aliases
from install import instance_settings


class NativeHttpsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_new_install_uses_loopback_https_and_preserves_private_ports_on_retry(self):
        first = instance_settings(self.root, None, None)
        self.assertEqual(first["WEB_PUBLIC_ORIGIN"], "https://127.0.0.1:8443")
        self.assertEqual(first["SERVER_HTTPS_API_ORIGIN"], "https://127.0.0.1:9443")
        self.assertEqual(first["SERVER_HTTPS_BIND_HOST"], "127.0.0.1")
        self.assertEqual(first["SERVER_HTTPS_MODE"], "internal")
        self.assertEqual(first["WEB_SESSION_COOKIE_SECURE"], "true")
        private = {first["SERVER_WEB_PORT"], first["SERVER_API_PORT"]}
        self.assertEqual(len(private), 2)
        self.assertTrue(private.isdisjoint({"8443", "9443"}))
        self.assertEqual(first, instance_settings(self.root, None, None))

    def test_backup_restores_original_http_acme_pem_and_local_ca(self):
        for mode in ("http", "acme", "provided", "internal"):
            with self.subTest(mode=mode):
                source = self.root / mode
                source.mkdir()
                old = instance_settings(source, 45201, 45202, http_loopback=mode == "http")
                if mode != "http":
                    configure_tls(old, "https://paper.example.org", "https://api.example.org",
                                  acme=mode == "acme",
                                  cert=Path("/tls/server.pem") if mode == "provided" else None,
                                  key=Path("/tls/server.key") if mode == "provided" else None)
                    if mode == "acme" or mode == "provided":
                        old.pop("SERVER_HTTPS_MODE")  # v0.1.1 config
                (source / "instance.json").write_text(json.dumps(old))
                (source / "data/database/workspace.sqlite3").touch()
                target = self.root / ("copy-" + mode)
                target.mkdir()
                restored = instance_settings(target, None, None, source)
                self.assertEqual(restored.get("SERVER_HTTPS_MODE", "http"), mode)
                self.assertEqual(restored["WEB_PUBLIC_ORIGIN"], old["WEB_PUBLIC_ORIGIN"])
                self.assertEqual(restored["SERVER_API_PORT"], old["SERVER_API_PORT"])
                self.assertEqual(restored.get("SERVER_HTTPS_CERT_FILE"), old.get("SERVER_HTTPS_CERT_FILE"))
                self.assertEqual(restored["CREDENTIAL_INSTALLATION_ID"], old["CREDENTIAL_INSTALLATION_ID"])

    def test_restore_relocates_operator_pems_inside_data(self):
        source = self.root / "source"
        source.mkdir()
        old = instance_settings(source, None, None)
        cert = source / "data/local.crt"
        key = source / "data/local.key"
        cert.touch()
        key.touch()
        configure_tls(old, "https://paper.example.org", "https://api.example.org", cert=cert, key=key)
        (source / "instance.json").write_text(json.dumps(old))
        (source / "data/database/workspace.sqlite3").touch()
        target = self.root / "restored"
        target.mkdir()
        restored = instance_settings(target, None, None, source)
        self.assertEqual(restored["SERVER_HTTPS_CERT_FILE"], str(target / "data/local.crt"))
        self.assertEqual(restored["SERVER_HTTPS_KEY_FILE"], str(target / "data/local.key"))

    def test_reconfigure_conflicting_private_port_and_explicit_lan_origin(self):
        initial = {"SERVER_WEB_PORT": "8443", "SERVER_API_PORT": "8000",
                   "SERVER_HTTPS_BIND_HOST": "127.0.0.1"}
        configure_tls(initial, "https://paper.home.arpa:8443", "https://paper.home.arpa:9443")
        self.assertNotEqual(initial["SERVER_WEB_PORT"], "8443")
        self.assertEqual(initial["SERVER_API_PORT"], "8000")
        self.assertNotIn("SERVER_HTTPS_BIND_HOST", initial)
        self.assertEqual(initial["SERVER_HTTPS_MODE"], "internal")

    def test_aliases_are_reachable_and_persist_when_upgrading(self):
        source = self.root / "source"
        source.mkdir()
        first = instance_settings(
            source, None, None, web_aliases="https://Paper.home.arpa:8443",
            api_aliases="https://paper.home.arpa:9443, https://api.example.org:443",
        )
        self.assertNotIn("SERVER_HTTPS_BIND_HOST", first)
        self.assertEqual(first["SERVER_HTTPS_WEB_ALIASES"], "https://paper.home.arpa:8443")
        self.assertEqual(first["SERVER_HTTPS_API_ALIASES"], "https://paper.home.arpa:9443,https://api.example.org")
        self.assertEqual(first["SERVER_WEB_BIND_HOST"], "127.0.0.1")
        self.assertEqual(first["SERVER_API_BIND_HOST"], "127.0.0.1")
        (source / "data/database/workspace.sqlite3").touch()
        target = self.root / "upgraded"
        target.mkdir()
        restored = instance_settings(target, None, None, source)
        self.assertEqual(restored["SERVER_HTTPS_API_ALIASES"], first["SERVER_HTTPS_API_ALIASES"])
        self.assertEqual(restored["SERVER_HTTPS_WEB_ALIASES"], first["SERVER_HTTPS_WEB_ALIASES"])
        update_tls_aliases(restored, "", "")
        self.assertEqual(restored["SERVER_HTTPS_BIND_HOST"], "127.0.0.1")

    def test_alias_ports_are_reserved_and_conflicts_are_rejected(self):
        original = {"SERVER_WEB_PORT": "8333", "SERVER_API_PORT": "9333"}
        configure_tls(original, "https://web.example", "https://api.example",
                      web_aliases="https://192.0.2.10:8333", api_aliases="https://192.0.2.10:9333")
        self.assertTrue({"8333", "9333"}.isdisjoint({original["SERVER_WEB_PORT"], original["SERVER_API_PORT"]}))
        with self.assertRaisesRegex(ValueError, "distinct"):
            update_tls_aliases(original, "https://API.example:443", None)

    def test_invalid_install_combinations_fail_before_writing(self):
        for flags in (["--cert", "fullchain.pem"], ["--web-origin", "https://example.org"],
                      ["--http-loopback", "--acme"], ["--acme", "--cert", "c", "--key", "k"],
                      ["--http-loopback", "--api-aliases", "https://app.example:9443"]):
            with self.subTest(flags=flags):
                target = self.root / "unused"
                result = subprocess.run([str(INSTALLER / "install.sh"), "--prefix", str(target), *flags],
                                        capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
