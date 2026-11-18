import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gvat_local.bash_alias import install, markers, uninstall


class BashAliasTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "checkout with 'quotes' and spaces"
        (self.root / ".venv-gvat").mkdir(parents=True)
        launcher = self.root / "src/bin/gvat"
        launcher.parent.mkdir(parents=True)
        launcher.write_text('#!/bin/sh\nprintf "gvat %s\\n" "$1"\n')
        launcher.chmod(0o755)
        self.profile = Path(self.temporary.name) / ".bashrc"
        self.original = b"export KEEP_ME=yes\r\n# existing profile without trailing newline"
        self.profile.write_bytes(self.original)

    def tearDown(self):
        self.temporary.cleanup()

    def test_alias_handles_quoted_checkout_paths_and_is_idempotent(self):
        install(self.root, "", str(self.profile))
        install(self.root, "", str(self.profile))
        start, _ = markers(self.root)
        self.assertEqual(self.profile.read_text().count(start), 1)
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", 'shopt -s expand_aliases; source "$1"; eval "gvat --version"', "--", str(self.profile)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "gvat --version")
        uninstall(self.root, str(self.profile))
        self.assertEqual(self.profile.read_bytes(), self.original)

    def test_uninstall_preserves_other_checkout_and_unmanaged_aliases(self):
        other = self.root.parent / "other"
        (other / ".venv-gvat").mkdir(parents=True)
        self.profile.write_bytes(self.original + b"\nalias gvat='echo unmanaged'\n")
        before = self.profile.read_bytes()
        install(other, "", str(self.profile))
        other_installed = self.profile.read_bytes()
        install(self.root, "", str(self.profile))
        uninstall(self.root, str(self.profile))
        self.assertEqual(self.profile.read_bytes(), other_installed)
        uninstall(other, str(self.profile))
        self.assertEqual(self.profile.read_bytes(), before)

    def test_uninstall_script_removes_recorded_alias_before_deleting_environment(self):
        repo = Path(__file__).resolve().parents[3]
        shutil.copy2(repo / "uninstall.sh", self.root / "uninstall.sh")
        helper = self.root / "src/gvat/src/gvat_local/bash_alias.py"
        helper.parent.mkdir(parents=True)
        shutil.copy2(repo / "src/gvat/src/gvat_local/bash_alias.py", helper)
        for relative in ("src/gvat/scripts/setup-common.sh", "src/gvat/tools/uninstall_files.py"):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / relative, destination)
        install(self.root, "", str(self.profile))
        record = json.loads((self.root / ".venv-gvat/gvat-install.json").read_text())
        self.assertIn(str(self.profile), record["profiles"])
        import os
        result = subprocess.run([str(self.root / "uninstall.sh")], env={**os.environ, "GVAT_BASHRC": str(self.profile), "GVAT_BIN_DIR": str(self.root / "bin")}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.profile.read_bytes(), self.original)
        self.assertFalse((self.root / ".venv-gvat").exists())
