"""Exercise bootstrap failures and ordering without installing host packages."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

CHART = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packages = self.root / "packages"
        self.calls = self.root / "calls.jsonl"
        mocks = self.root / "bin"
        mocks.mkdir()
        mock = mocks / "mock"
        mock.write_text('''#!/usr/bin/python3
import json, os, shutil, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["TEST_CALLS"], "a") as log:
    log.write(json.dumps({"name": name, "args": args,
                          "proxy": os.environ.get("http_proxy"),
                          "home": os.environ.get("HOME")}) + "\\n")
if name == "apt-get":
    if "update" in args and os.environ.get("FAIL_UPDATE") == "1":
        sys.exit(9)
    if "--download-only" in args:
        if os.environ.get("FAIL_DOWNLOAD") == "1":
            sys.exit(8)
        (Path(os.environ["RUNNER_PACKAGE_DIR"]) / "dependency.deb").write_bytes(b"test package")
    elif "install" in args and os.environ.get("FAIL_INSTALL") == "1":
        sys.exit(7)
elif name == "curl":
    if os.environ.get("FAIL_AWS_DOWNLOAD") == "1":
        sys.exit(22)
    shutil.copyfile(os.environ["TEST_AWS_ZIP"], args[args.index("--output") + 1])
elif name == "aws-install":
    if os.environ.get("FAIL_AWS_INSTALL") == "1":
        sys.exit(6)
elif name == "aws":
    print("aws-cli/2.test Python/test Linux/test")
''')
        mock.chmod(0o755)
        for command in ("apt-get", "curl", "aws-install", "aws", "groupadd", "useradd", "setpriv"):
            (mocks / command).symlink_to(mock)
        aws_zip = self.root / "aws-fixture.zip"
        with zipfile.ZipFile(aws_zip, "w") as archive:
            install = zipfile.ZipInfo("aws/install")
            install.external_attr = 0o100755 << 16
            archive.writestr(install, '#!/bin/sh\nexec aws-install "$@"\n')
        self.env = dict(os.environ, PATH=f"{mocks}:{os.environ['PATH']}",
                        RUNNER_PACKAGE_DIR=str(self.packages), TEST_CALLS=str(self.calls),
                        RUNNER_APT_CACHE_DIR=str(self.root / "apt-cache"),
                        TEST_AWS_ZIP=str(aws_zip),
                        http_proxy="", HTTP_PROXY="http://test-proxy:8080")

    def run_script(self, name, success=True, **overrides):
        result = subprocess.run(["bash", str(CHART / "scripts" / name),
                                 "/bin/bash", "/opt/runner-scripts/runner.sh"],
                                env=dict(self.env, **overrides), text=True,
                                capture_output=True, timeout=10)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def commands(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_download_then_offline_install_then_drop_privileges(self):
        self.run_script("download-dependencies.sh")
        self.assertTrue((self.packages / ".complete").exists())
        self.run_script("start-container.sh")
        calls = self.commands()
        self.assertEqual([call["name"] for call in calls],
                         ["apt-get", "apt-get", "apt-get", "curl", "apt-get",
                          "aws-install", "aws", "groupadd", "useradd", "setpriv"])
        self.assertEqual(calls[0]["proxy"], "http://test-proxy:8080")
        self.assertIn("--download-only", calls[1]["args"])
        self.assertIn("--no-download", calls[2]["args"])
        self.assertIn("https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip", calls[3]["args"])
        self.assertIn("--no-download", calls[4]["args"])
        self.assertIn(str(self.root / "apt-cache" / "dependency.deb"), calls[4]["args"])
        self.assertNotIn("update", calls[4]["args"])
        self.assertEqual(calls[5]["args"], ["--install-dir", "/usr/local/aws-cli", "--bin-dir", "/usr/local/bin"])
        self.assertEqual(calls[6]["args"], ["--version"])
        for call in calls:
            if call["name"] == "apt-get":
                self.assertNotIn("awscli", call["args"])
        self.assertEqual(calls[-1]["args"], ["--reuid=1001", "--regid=1001", "--init-groups",
                                            "--no-new-privs", "/usr/bin/tini", "--",
                                            "/bin/bash", "/opt/runner-scripts/runner.sh"])
        self.assertEqual(calls[-1]["home"], "/home/runner")

    def test_update_failure_stops_without_retry_or_install(self):
        self.run_script("download-dependencies.sh", False, FAIL_UPDATE="1")
        self.assertEqual(len(self.commands()), 1)
        self.assertFalse((self.packages / ".complete").exists())
        self.run_script("start-container.sh", False)
        self.assertEqual(len(self.commands()), 1)

    def test_retry_removes_stale_packages_and_requires_complete_download(self):
        self.packages.mkdir()
        (self.packages / "stale.deb").write_text("obsolete")
        (self.packages / ".complete").touch()
        self.run_script("download-dependencies.sh", False, FAIL_DOWNLOAD="1")
        self.assertFalse((self.packages / ".complete").exists())
        self.assertFalse((self.packages / "stale.deb").exists())
        self.run_script("download-dependencies.sh")
        self.run_script("start-container.sh")

    def test_corrupt_cache_is_rejected_before_package_install(self):
        self.run_script("download-dependencies.sh")
        (self.packages / "dependency.deb").write_text("corrupt")
        self.run_script("start-container.sh", False)
        self.assertEqual(len(self.commands()), 4)

    def test_different_base_image_is_rejected_before_package_install(self):
        self.run_script("download-dependencies.sh")
        (self.packages / "base.sha256").write_text("0" * 64 + "  /var/lib/dpkg/status\n")
        result = self.run_script("start-container.sh", False)
        self.assertIn("Base image differs", result.stderr)
        self.assertEqual(len(self.commands()), 4)

    def test_install_failure_does_not_start_runner(self):
        self.run_script("download-dependencies.sh")
        self.run_script("start-container.sh", False, FAIL_INSTALL="1")
        self.assertEqual([call["name"] for call in self.commands()], ["apt-get"] * 3 + ["curl", "apt-get"])

    def test_aws_download_failure_prevents_completion(self):
        self.run_script("download-dependencies.sh", False, FAIL_AWS_DOWNLOAD="1")
        self.assertFalse((self.packages / ".complete").exists())
        self.assertFalse((self.packages / "awscliv2.zip").exists())
        self.run_script("start-container.sh", False)
        self.assertEqual(len(self.commands()), 4)

    def test_corrupt_aws_zip_is_rejected_before_install(self):
        self.run_script("download-dependencies.sh")
        (self.packages / "awscliv2.zip").write_bytes(b"corrupt")
        self.run_script("start-container.sh", False)
        self.assertEqual(len(self.commands()), 4)

    def test_aws_install_failure_prevents_runner_start(self):
        self.run_script("download-dependencies.sh")
        self.run_script("start-container.sh", False, FAIL_AWS_INSTALL="1")
        self.assertEqual(self.commands()[-1]["name"], "aws-install")


if __name__ == "__main__":
    unittest.main()
