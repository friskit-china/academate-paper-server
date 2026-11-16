"""Direct installation shell integration with a disposable host HOME."""
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]


class ShellPathTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory(prefix="amp-shell-path-")
        self.addCleanup(sandbox.cleanup)
        self.home = Path(sandbox.name).resolve()
        self.root = self.home / "AcadeMatePaper"
        self.amp = self.root / "bin/amp"
        self.amp.parent.mkdir(parents=True)
        self.amp.write_text("#!/bin/sh\necho test-amp\n")
        self.amp.chmod(0o700)
        (self.root / ".academate-personal-server").write_text("format=1\n")
        self.bin = self.home / "user's bin"
        self.bin.mkdir()
        self.link = self.bin / "amp"
        self.link.symlink_to(self.amp)
        (self.root / ".amp-link").write_text(str(self.link) + "\n")
        self.env = dict(HOME=str(self.home), SHELL="/bin/bash", PATH=os.defpath)

    def cli(self, *args, answer=None):
        return subprocess.run([str(SCRIPTS / "install.sh"), *args], input=answer, text=True,
                              capture_output=True, env=self.env, cwd=self.home, timeout=10)

    def helper(self, *args):
        return subprocess.run([sys.executable, "-IB", str(SCRIPTS / "shell_path.py"), *args],
                              env=self.env, cwd=self.home, text=True, capture_output=True, timeout=10)

    def test_bash_interactive_and_login_profile_repair_and_uninstall(self):
        profile = self.home / ".bash_login"
        profile.write_bytes(b"# original without final newline")
        rc = self.home / ".bashrc"
        original = self.home / "my-dotfiles"
        original.write_bytes(b"# own config\n")
        rc.symlink_to(original)
        self.link.unlink()
        # Repair when the caller already has this path, e.g. from an earlier manual export.
        self.env["PATH"] = str(self.bin) + ":" + os.defpath
        for _ in range(2):
            result = self.cli("--repair-path")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link.resolve(), self.amp)
        for path in (rc, profile):
            self.assertEqual(path.read_text().count("# AcadeMate Paper amp"), 1)
            # Use an isolated PATH and no system login profile; source the selected file twice.
            script = '. "$1"; . "$1"; amp status; printf "%s" "$PATH"'
            reader = subprocess.run(["bash", "--noprofile", "--norc", "-c", script, "bash", str(path)],
                                    env={**self.env, "PATH": os.defpath}, text=True,
                                    capture_output=True, check=True)
            self.assertEqual(reader.stdout, "test-amp\n" + str(self.bin) + ":" + os.defpath)
        self.assertFalse((self.home / ".bash_profile").exists())
        with rc.open("ab") as stream:
            stream.write(b"# later personal addition\n")
        cancelled = self.cli("--uninstall", "--prefix", self.root, answer="\n")
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertIn("# AcadeMate Paper amp", rc.read_text())
        removed = self.cli("--uninstall", "--prefix", self.root, answer=str(self.root) + "\n")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(profile.read_bytes(), b"# original without final newline")
        self.assertEqual(original.read_bytes(), b"# own config\n# later personal addition\n")
        self.assertTrue(rc.is_symlink())
        self.assertFalse(self.root.exists())
        self.assertFalse(self.link.is_symlink())

    def test_uninstall_completes_if_a_managed_profile_becomes_read_only(self):
        self.assertEqual(self.cli("--repair-path").returncode, 0)
        rc = self.home / ".bashrc"
        line = rc.read_text()
        rc.chmod(0o400)
        if os.access(rc, os.W_OK):
            # Root may still write a read-only file, even in an isolated test container.
            rc.rename(self.home / "original-profile")
            rc.mkdir()
        removed = self.cli("--uninstall", "--prefix", self.root, answer=str(self.root) + "\n")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertIn(str(rc), removed.stderr)
        self.assertIn(line.split("# AcadeMate Paper amp ")[1].strip(), removed.stderr)
        self.assertFalse(self.root.exists())
        self.assertFalse((self.home / ".profile").exists())
        self.assertFalse(self.link.is_symlink())

    def test_other_command_is_preserved_and_internal_bin_can_be_selected(self):
        self.link.unlink()
        self.link.write_text("someone else's amp\n")
        failed = self.cli("--repair-path", "--prefix", self.root)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.link.read_text(), "someone else's amp\n")
        self.assertFalse((self.home / ".bashrc").exists())
        repaired = self.cli("--repair-path", "--bin-dir", self.amp.parent)
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertIn(str(self.amp.parent), (self.home / ".bashrc").read_text())

    def test_skip_unknown_and_unwritable_profiles_leave_runtime_usable(self):
        skipped = self.helper("configure", self.root, self.bin, "--no-path-update")
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.assertIn(shlex.quote(str(self.bin)), skipped.stdout)
        self.assertFalse((self.home / ".bashrc").exists())
        self.env["SHELL"] = "/bin/other"
        unknown = self.cli("--repair-path")
        self.assertEqual(unknown.returncode, 0, unknown.stderr)
        self.assertIn(shlex.quote(str(self.link)), unknown.stdout)
        self.env["SHELL"] = "/bin/zsh"
        (self.home / ".zshrc").mkdir()
        unavailable = self.cli("--repair-path")
        self.assertEqual(unavailable.returncode, 0, unavailable.stderr)
        self.assertIn("无法更新", unavailable.stderr)
        self.assertTrue(self.amp.is_file())

    def test_fish_configuration_uses_host_xdg_and_posix_sh_uses_profile(self):
        self.env.update(SHELL="/bin/fish", XDG_CONFIG_HOME=str(self.home / "xdg"))
        self.assertEqual(self.cli("--repair-path").returncode, 0)
        config = self.home / "xdg/fish/config.fish"
        self.assertIn("set -gx PATH", config.read_text())
        self.assertIn("user\\'s bin", config.read_text())
        fish = shutil.which("fish")
        if fish:
            result = subprocess.run([fish, "-c", "amp status"], env=self.env,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, "test-amp\n")
        self.env.update(SHELL="/bin/sh", ENV=str(self.home / "sh-interactive"))
        self.assertEqual(self.cli("--repair-path").returncode, 0)
        for path in (self.home / ".profile", self.home / "sh-interactive"):
            self.assertIn("# AcadeMate Paper amp", path.read_text())
        self.assertEqual(self.helper("remove", self.root).returncode, 0)
        for path in (config, self.home / ".profile", self.home / "sh-interactive"):
            self.assertFalse(path.exists())

    @unittest.skipUnless(shutil.which("zsh"), "zsh not installed")
    def test_fresh_zsh_with_custom_zdotdir_finds_quoted_command(self):
        dotfiles = self.home / "zsh configs"
        self.env.update(SHELL=shutil.which("zsh"), ZDOTDIR=str(dotfiles))
        result = self.cli("--repair-path")
        self.assertEqual(result.returncode, 0, result.stderr)
        for mode in ("-ic", "-lc"):
            with self.subTest(mode=mode):
                shell = subprocess.run([self.env["SHELL"], "-d", mode, "amp status"], env=self.env,
                                       capture_output=True, text=True, timeout=10, check=True)
                self.assertEqual(shell.stdout, "test-amp\n")
        self.assertEqual(self.helper("remove", self.root).returncode, 0)
        for name in (".zshrc", ".zprofile"):
            self.assertFalse((dotfiles / name).exists())


if __name__ == "__main__":
    unittest.main()
