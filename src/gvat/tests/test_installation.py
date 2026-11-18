import json
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform == "linux", "Installation scripts target Linux")
class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "checkout"
        self.bin = self.base / "bin"
        self.cache = self.base / "cache"
        self.root.mkdir()
        self.bin.mkdir()
        repo = Path(__file__).resolve().parents[3]
        shutil.copy2(repo / "uninstall.sh", self.root / "uninstall.sh")
        helper = self.root / "src/gvat/src/gvat_local/bash_alias.py"
        helper.parent.mkdir(parents=True)
        shutil.copy2(repo / "src/gvat/src/gvat_local/bash_alias.py", helper)
        for relative in ("config.yml", "src/gvat/scripts/setup-common.sh", "src/gvat/tools/install_config.py", "src/gvat/tools/install_node.py", "src/gvat/tools/install_build_tools.py", "src/gvat/tools/uninstall_files.py"):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / relative, destination)
        self.launcher = self.root / "src/bin/gvat"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("#!/bin/sh\n")
        self.artifacts = [".venv-gvat", "src/gvat/web/node_modules", "src/gvat/web/dist", "src/gvat/src/gvat_local.egg-info"]
        for relative in self.artifacts:
            directory = self.root / relative
            directory.mkdir(parents=True)
            (directory / "generated").write_text("generated")
        self.dataset = self.root / "sample/annotations.xml"
        self.dataset.parent.mkdir(parents=True)
        self.dataset.write_text("keep sample XML")
        self.environment = {**os.environ, "GVAT_BIN_DIR": str(self.bin), "GVAT_BASHRC": str(self.base / ".bashrc"), "XDG_CACHE_HOME": str(self.cache), "XDG_CONFIG_HOME": str(self.base / 'config')}

    def tearDown(self):
        self.temporary.cleanup()

    def uninstall(self, *arguments):
        return subprocess.run([str(self.root / "uninstall.sh"), *arguments], env=self.environment, capture_output=True, text=True)

    def install_with_fake_dependencies(self, fail=False):
        repo = Path(__file__).resolve().parents[3]
        shutil.copy2(repo / "install.sh", self.root / "install.sh")
        python = self.root / ".venv-gvat/bin/python"
        python.parent.mkdir()
        python.write_text("#!/bin/bash\nif [[ $1 == -m && $2 == pip ]]; then echo 'verbose dependency details'; exit 0; fi\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
        python.chmod(0o755)
        tools = self.base / "fake-tools"
        tools.mkdir()
        for name, body in {
            "node": "exit 0",
            "npm": 'echo "verbose npm $*"; if [[ ${GVAT_TEST_FAIL:-0} == 1 ]]; then echo "fixture npm failure" >&2; exit 17; fi',
            "sudo": 'echo "unexpected privilege escalation" >&2; exit 99',
            "apt-get": 'echo "unexpected system package installation" >&2; exit 99',
        }.items():
            tool = tools / name
            tool.write_text("#!/bin/bash\n" + body + "\n")
            tool.chmod(0o755)
        environment = {**self.environment, "PATH": f"{tools}:{os.environ['PATH']}", "GVAT_TEST_FAIL": "1" if fail else "0"}
        return subprocess.run([str(self.root / "install.sh"), "--no-link", "--no-alias"], env=environment, capture_output=True, text=True)

    def test_install_keeps_dependency_output_in_log_without_system_package_commands(self):
        result = self.install_with_fake_dependencies()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 3)
        self.assertIn("GVAT installed.", result.stdout)
        self.assertNotIn("verbose", result.stdout)
        log = (self.root / ".gvat-cache/install.log").read_text()
        self.assertIn("verbose dependency details", log)
        self.assertIn("run build", log)
        self.assertNotIn("unexpected", log)
        self.assertFalse((self.base / ".bashrc").exists())
        self.assertTrue((self.base / 'config/gvat/config.yml').is_file())

    def test_python_fallback_and_explicit_interpreter_path(self):
        tools = self.base / 'python-tools'
        tools.mkdir()
        for name, body in (('python3', 'exit 1'), ('python', 'exec ' + shlex.quote(sys.executable) + ' "$@"')):
            command = tools / name
            command.write_text('#!/bin/bash\n' + body + '\n')
            command.chmod(0o755)
        script = 'source ' + shlex.quote(str(self.root / 'src/gvat/scripts/setup-common.sh')) + '; select_python; printf "%s" "$GVAT_SETUP_PYTHON"'
        environment = {**self.environment, 'PATH': str(tools) + ':' + os.environ['PATH']}
        environment.pop('GVAT_PYTHON', None)
        result = subprocess.run(['bash', '-c', script], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'python')
        result = subprocess.run(['bash', '-c', script], env={**environment, 'GVAT_PYTHON': sys.executable}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, sys.executable)

    def test_install_failure_stops_build_and_reports_a_short_error_with_log_path(self):
        result = self.install_with_fake_dependencies(fail=True)
        self.assertEqual(result.returncode, 17)
        self.assertNotIn("GVAT installed.", result.stdout)
        self.assertIn(".gvat-cache/install.log", result.stderr)
        self.assertIn("fixture npm failure", result.stderr)
        self.assertLessEqual(len(result.stderr.splitlines()), 2)
        log = (self.root / ".gvat-cache/install.log").read_text()
        self.assertNotIn("run build", log)

    def test_uninstall_removes_generated_files_and_owned_links_and_can_be_repeated(self):
        (self.bin / "gvat").symlink_to(self.launcher)
        custom = self.base / "custom-bin/gvat"
        custom.parent.mkdir()
        custom.symlink_to(self.launcher)
        record = {"version": 1, "root": str(self.root), "links": [str(custom)]}
        (self.root / ".venv-gvat/gvat-install.json").write_text(json.dumps(record))
        result = self.uninstall()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 3)
        self.assertIn("GVAT uninstalled", result.stdout)
        self.assertNotIn("Removed generated files:", result.stdout)
        self.assertIn("Removed generated files:", (self.root / ".gvat-cache/uninstall.log").read_text())
        self.assertFalse((self.bin / "gvat").is_symlink())
        self.assertFalse(custom.is_symlink())
        for relative in self.artifacts:
            self.assertFalse((self.root / relative).exists())
        self.assertEqual(self.dataset.read_text(), "keep sample XML")
        self.assertTrue(self.launcher.exists())
        self.assertEqual(self.uninstall().returncode, 0)

    def test_unrelated_commands_and_external_symlink_targets_are_preserved(self):
        unrelated = self.base / "other-checkout/gvat"
        unrelated.parent.mkdir()
        unrelated.write_text("another installation")
        (self.bin / "gvat").symlink_to(unrelated)
        shutil.rmtree(self.root / ".venv-gvat")
        shared = self.base / "shared-environment"
        shared.mkdir()
        (shared / "keep").write_text("shared")
        (self.root / ".venv-gvat").symlink_to(shared, target_is_directory=True)
        result = self.uninstall()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.bin / "gvat").is_symlink())
        self.assertEqual(unrelated.read_text(), "another installation")
        self.assertEqual((shared / "keep").read_text(), "shared")
        self.assertFalse((self.root / ".venv-gvat").is_symlink())

    def test_parent_symlinks_cannot_redirect_cleanup_outside_the_checkout(self):
        web = self.root / "src/gvat/web"
        shutil.rmtree(web)
        external = self.base / "external-web/node_modules"
        external.mkdir(parents=True)
        (external / "keep").write_text("external")
        web.symlink_to(external.parent, target_is_directory=True)
        result = self.uninstall()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the installation", result.stderr)
        self.assertEqual((external / "keep").read_text(), "external")

    def test_cache_removal_is_explicit_and_limited_to_downloaded_node_tools(self):
        tools = self.cache / "gvat/node"
        tools.mkdir(parents=True)
        (tools / "downloaded").write_text("node")
        unrelated = self.cache / "other/cache"
        unrelated.parent.mkdir()
        unrelated.write_text("keep")
        self.assertEqual(self.uninstall().returncode, 0)
        self.assertTrue(tools.exists())
        self.assertEqual(self.uninstall("--purge-cache").returncode, 0)
        self.assertFalse(tools.exists())
        self.assertEqual(unrelated.read_text(), "keep")
