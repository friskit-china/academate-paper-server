"""Integration checks for the direct installer's local removal flow."""

import fcntl
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


class UninstallTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory(prefix="amp-removal-test-")
        self.addCleanup(sandbox.cleanup)
        self.sandbox = Path(sandbox.name).resolve()
        self.home = self.sandbox / "home"
        self.home.mkdir()
        self.root = self.home / "Paper instance"
        self.root.mkdir(mode=0o700)
        self.marker = self.root / ".academate-personal-server"
        self.marker.write_text("format=1\n")
        self.cached = self.root / "cache" / "package.tgz"
        self.cached.parent.mkdir()
        self.cached.write_bytes(b"disposable package")
        self.bin_dir = self.home / "custom bin"
        self.bin_dir.mkdir()
        self.link = self.bin_dir / "amp"
        self.stop_log = self.sandbox / "stop.log"

    def wizard(self, *args, answers):
        return subprocess.run(
            ["/bin/sh", str(INSTALLER), *map(str, args)],
            input=answers,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.sandbox,
            env={"HOME": str(self.home), "PATH": os.defpath},
            timeout=15,
            check=False,
        )

    def remove(self, *, answers=None, prefix=None):
        if answers is None:
            answers = f"{self.root}\n"
        return self.wizard(
            "--uninstall",
            "--prefix",
            prefix or self.root,
            "--bin-dir",
            self.bin_dir,
            answers=answers,
        )

    def add_service(self, stop_code=0):
        release = self.root / "releases" / "personal"
        release.mkdir(parents=True)
        (self.root / "current").symlink_to(
            "releases/personal", target_is_directory=True
        )
        data = self.root / "data"
        (data / "database").mkdir(parents=True)
        (data / "run").mkdir(mode=0o700)
        (data / "database" / "workspace.sqlite3").write_bytes(b"disposable database")
        (self.root / "instance.json").write_text(
            json.dumps(
                {
                    "STORAGE_ROOT": str(data),
                    "DATABASE_URL": f"sqlite:///{data}/database/workspace.sqlite3",
                }
            )
        )
        (self.root / "bin").mkdir()
        amp = self.root / "bin" / "amp"
        amp.write_text(
            "#!/bin/sh\n"
            'test "$1" = stop || exit 90\n'
            f"echo stopped >> {shlex.quote(str(self.stop_log))}\n"
            'echo \'{"server":"stopped","components":{}}\'\n'
            f"exit {stop_code}\n"
        )
        amp.chmod(0o700)
        self.link.symlink_to(amp)
        return data

    def test_partial_install_can_be_deleted_without_bootstrap_download(self):
        result = self.remove()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(str(self.root), result.stdout)
        self.assertFalse(self.root.exists())

    def test_wizard_offers_deletion_for_existing_instance(self):
        result = self.wizard(answers=f"{self.root}\n2\n{self.root}\n")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("删除", result.stdout)
        self.assertIn(str(self.root), result.stdout)
        self.assertFalse(self.root.exists())

    def test_empty_menu_choice_and_inexact_confirmation_preserve_instance(self):
        self.add_service()
        self.wizard(answers=f"{self.root}\n\n")
        for text in ("", "\n", "yes\n", f"{self.home}\n"):
            with self.subTest(confirmation=text):
                self.remove(answers=text)
                self.assertTrue(self.cached.is_file())
                self.assertTrue(self.link.is_symlink())
                self.assertFalse(self.stop_log.exists())

    def test_confirmed_removal_stops_only_target_and_preserves_external_files(self):
        data = self.add_service()
        other = self.home / "other instance"
        other.mkdir()
        (other / ".academate-personal-server").write_text("format=1\n")
        external = other / "certificate.pem"
        external.write_text("test certificate")
        (data / "certificate.pem").symlink_to(external)
        result = self.remove()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.stop_log.read_text().splitlines(), ["stopped"])
        self.assertFalse(self.root.exists())
        self.assertFalse(self.link.is_symlink())
        self.assertTrue(self.bin_dir.is_dir())
        self.assertEqual(external.read_text(), "test certificate")

    def test_recorded_custom_bin_dir_is_removed_without_passing_the_flag(self):
        self.add_service()
        (self.root / ".amp-link").write_text(f"{self.link}\n")
        result = self.wizard(
            "--uninstall", "--prefix", self.root, answers=f"{self.root}\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.link.is_symlink())

    def test_replaced_external_command_is_preserved(self):
        self.add_service()
        self.link.unlink()
        other = self.sandbox / "other-amp"
        other.write_text("replacement command")
        self.link.symlink_to(other)
        result = self.remove()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.root.exists())
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(self.link.read_text(), "replacement command")

    def test_refuses_missing_or_unknown_format_marker(self):
        self.marker.unlink()
        self.assertNotEqual(self.remove().returncode, 0)
        self.assertTrue(self.cached.exists())
        self.marker.write_text("unrecognized\n")
        self.assertNotEqual(self.remove().returncode, 0)
        self.assertTrue(self.cached.exists())

    def test_refuses_disposable_home_even_with_a_copied_marker(self):
        (self.home / ".academate-personal-server").write_text("format=1\n")
        result = self.remove(prefix=self.home, answers=f"{self.home}\n")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(self.cached.exists())

    def test_refuses_linked_marker(self):
        self.marker.unlink()
        outside_marker = self.sandbox / "other-marker"
        outside_marker.write_text("format=1\n")
        self.marker.symlink_to(outside_marker)
        result = self.remove()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(self.cached.exists())
        self.assertEqual(outside_marker.read_text(), "format=1\n")

    def test_refuses_symlink_selected_as_prefix(self):
        alias = self.home / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        result = self.remove(prefix=alias, answers=f"{self.root}\n")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(self.cached.exists())
        self.assertTrue(alias.is_symlink())

    def test_invalid_target_cannot_run_a_fake_interpreter_before_confirmation(self):
        self.marker.unlink()
        interpreter = self.root / "releases/personal/runtime/bin/python"
        interpreter.parent.mkdir(parents=True)
        probe = self.sandbox / "unexpected-launch"
        interpreter.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(probe))}\nexit 18\n")
        interpreter.chmod(0o700)
        self.remove(answers="\n")
        self.assertFalse(probe.exists())
        self.assertTrue(self.cached.exists())

    def test_directory_replaced_during_stop_is_never_removed(self):
        self.add_service()
        old_root = self.sandbox / "moved-by-operator"
        quoted_root = shlex.quote(str(self.root))
        sentinel = self.root / "unrelated-file"
        amp = self.root / "bin/amp"
        # Model a second operator replacing a path while the service drains.
        amp.write_text(
            "#!/bin/sh\nset -eu\n"
            f"mv {quoted_root} {shlex.quote(str(old_root))}\n"
            f"mkdir {quoted_root}\n"
            f"touch {shlex.quote(str(sentinel))}\n"
            'echo \'{"server":"stopped","components":{}}\'\n'
        )
        result = self.remove()
        self.assertTrue(sentinel.exists(), result.stdout)
        self.assertTrue(self.link.is_symlink())
        self.assertFalse((old_root / ".install-lock").exists())
        self.assertTrue((old_root / ".academate-personal-server").exists())
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_active_installer_prevents_deletion(self):
        lock = self.root / ".install-lock"
        lock.mkdir()
        result = self.remove()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(lock.is_dir())
        self.assertTrue(self.cached.exists())

    def test_external_writer_blocks_removal_after_stop(self):
        data = self.add_service()
        lock = data / "database" / ".academate-server.lock"
        with lock.open("a") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.remove()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(self.cached.exists())
        self.assertTrue(self.link.is_symlink())
        result = self.remove()
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_stop_failure_leaves_code_data_and_external_command_in_place(self):
        data = self.add_service(stop_code=1)
        result = self.remove()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(self.stop_log.exists())
        self.assertTrue((data / "database" / "workspace.sqlite3").exists())
        self.assertTrue(self.link.is_symlink())
        self.assertTrue(self.cached.exists())


if __name__ == "__main__":
    unittest.main()
