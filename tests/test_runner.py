"""Offline behavioral tests; no GitHub registration or Docker daemon required."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest

CHART = Path(__file__).resolve().parents[1]
ENTRYPOINT = CHART / "scripts" / "runner.sh"


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.persistent = self.root / "persistent"
        self.runner = self.persistent / "gh-runner"
        package = self.root / "package"
        package.mkdir()
        self.script(package / "config.sh", '''#!/bin/bash
set -eu
printf '%s\\n' "$@" >> "$TEST_ROOT/config-args"
if [[ "${FAIL_CONFIG:-}" == 1 ]]; then exit 4; fi
printf '{}' > .runner
printf '{}' > .credentials
printf '{}' > .credentials_rsaparams
''')
        self.script(package / "run.sh", '''#!/bin/bash
set -eu
[[ -z "${GITHUB_RUNNER_TOKEN+x}" ]]
[[ "$RUNNER_MANUALLY_TRAP_SIG" == 1 ]]
printf 'ran\\n' >> "$TEST_ROOT/runs"
''')
        self.archive = self.root / "runner.tar.gz"
        with tarfile.open(self.archive, "w:gz") as archive:
            for path in package.iterdir():
                archive.add(path, arcname=path.name)
        mocks = self.root / "bin"
        mocks.mkdir()
        self.script(mocks / "curl", '''#!/bin/bash
set -eu
printf 'download\\n' >> "$TEST_ROOT/downloads"
while [[ $# -gt 0 ]]; do
  if [[ "$1" == --output ]]; then cp "$TEST_ARCHIVE" "$2"; exit 0; fi
  shift
done
exit 1
''')
        self.env = dict(os.environ, PATH=f"{mocks}:{os.environ['PATH']}",
                        TEST_ROOT=str(self.root), TEST_ARCHIVE=str(self.archive),
                        RUNNER_PERSISTENT_DIR=str(self.persistent),
                        GITHUB_REPO="owner/repo", RUNNER_NAME="test-runner",
                        RUNNER_LABELS="k8s,ubuntu24", RUNNER_WORK_DIR="_work",
                        RUNNER_INIT_VERSION="2.337.0",
                        RUNNER_SHA256=hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                        GITHUB_RUNNER_TOKEN="fake-registration-token")

    @staticmethod
    def script(path, content):
        path.write_text(content)
        path.chmod(0o755)

    def run_entrypoint(self, success=True, **overrides):
        result = subprocess.run(["bash", str(ENTRYPOINT)],
                                env=dict(self.env, **overrides), text=True,
                                capture_output=True, timeout=10)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("fake-registration-token", result.stdout + result.stderr)
        return result

    def test_initial_registration_and_updated_version_survive_restart_without_token(self):
        self.run_entrypoint()
        args = (self.root / "config-args").read_text().splitlines()
        self.assertEqual(args, ["--unattended", "--url", "https://github.com/owner/repo",
                               "--token", "fake-registration-token", "--name", "test-runner",
                               "--labels", "k8s,ubuntu24", "--work", "_work"])
        self.script(self.runner / "run.sh", '#!/bin/bash\nprintf updated > "$TEST_ROOT/updated"\n')
        self.run_entrypoint(GITHUB_RUNNER_TOKEN="", RUNNER_INIT_VERSION="2.338.0")
        self.assertEqual((self.root / "updated").read_text(), "updated")
        self.assertEqual((self.root / "downloads").read_text().splitlines(), ["download"])
        self.assertEqual((self.root / "config-args").read_text().splitlines(), args)

    def test_hash_failure_does_not_publish_installation_and_retry_recovers(self):
        self.assertIn("SHA256 mismatch", self.run_entrypoint(False, RUNNER_SHA256="0" * 64).stderr)
        self.assertFalse(self.runner.exists())
        self.assertFalse((self.root / "config-args").exists())
        self.run_entrypoint()
        self.assertTrue((self.runner / ".installation-complete").exists())
        self.assertFalse((self.persistent / ".runner-install").exists())

    def test_interrupted_staging_is_replaced(self):
        stage = self.persistent / ".runner-install" / "package"
        stage.mkdir(parents=True)
        (stage / "partial").write_text("incomplete extraction")
        self.run_entrypoint()
        self.assertFalse((self.runner / "partial").exists())

    def test_missing_token_can_be_supplied_later_without_redownload(self):
        result = self.run_entrypoint(False, GITHUB_RUNNER_TOKEN="")
        self.assertIn("First registration requires", result.stderr)
        self.assertFalse((self.root / "runs").exists())
        self.run_entrypoint()
        self.assertEqual((self.root / "downloads").read_text().splitlines(), ["download"])

    def test_failed_registration_does_not_run(self):
        self.run_entrypoint(False, FAIL_CONFIG="1")
        self.assertFalse((self.root / "runs").exists())
        self.assertFalse((self.runner / ".chart-registration").exists())
        self.run_entrypoint()

    def test_changed_registration_settings_fail_without_overwriting(self):
        self.run_entrypoint()
        original = (self.runner / ".chart-registration").read_bytes()
        for override in ({"GITHUB_REPO": "other/repo"}, {"RUNNER_NAME": "other"},
                         {"RUNNER_LABELS": "other"}, {"RUNNER_WORK_DIR": "work2"}):
            with self.subTest(override=override):
                self.assertIn("changed", self.run_entrypoint(False, **override).stderr)
        self.assertEqual((self.runner / ".chart-registration").read_bytes(), original)
        self.assertEqual((self.root / "runs").read_text().splitlines(), ["ran"])

    def test_missing_credentials_fail_without_reregistering(self):
        self.run_entrypoint()
        (self.runner / ".credentials").unlink()
        self.assertIn("Incomplete registration", self.run_entrypoint(False).stderr)

    def test_partial_registration_is_preserved(self):
        self.run_entrypoint(False, GITHUB_RUNNER_TOKEN="")
        (self.runner / ".credentials").write_text("partial")
        self.assertIn("Partial registration", self.run_entrypoint(False).stderr)
        self.assertEqual((self.runner / ".credentials").read_text(), "partial")

    def test_unmanaged_installation_is_not_overwritten(self):
        self.runner.mkdir(parents=True)
        (self.runner / "keep").write_text("user data")
        self.assertIn("unmanaged installation", self.run_entrypoint(False).stderr)
        self.assertEqual((self.runner / "keep").read_text(), "user data")
        self.assertFalse((self.root / "downloads").exists())

    def test_existing_lock_prevents_second_runner(self):
        self.persistent.mkdir()
        with (self.persistent / ".runner.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn("already owns this PVC", self.run_entrypoint(False).stderr)
        self.assertFalse((self.root / "downloads").exists())
        self.run_entrypoint()

    def test_work_directory_cannot_escape_or_overwrite_program(self):
        for value in ("../escape", "/tmp/work", "bin", "externals", ".", "nested/work"):
            with self.subTest(value=value):
                self.run_entrypoint(False, RUNNER_WORK_DIR=value)
        self.assertFalse((self.root / "downloads").exists())

    def test_lock_remains_held_after_exec(self):
        self.run_entrypoint()
        ready = self.root / "ready"
        self.script(self.runner / "run.sh", '#!/bin/bash\ntouch "$TEST_ROOT/ready"\nexec sleep 30\n')
        process = subprocess.Popen(["bash", str(ENTRYPOINT)], env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists(), "Runner did not reach its long-running process")
            self.assertIn("already owns this PVC", self.run_entrypoint(False).stderr)
        finally:
            process.terminate()
            process.wait(timeout=5)


@unittest.skipUnless(shutil.which("helm"), "Helm is required for template tests")
class ChartTests(unittest.TestCase):
    def render(self, values=None, success=True, extra_args=()):
        with tempfile.TemporaryDirectory() as temp:
            overrides = Path(temp) / "values.json"
            overrides.write_text(json.dumps(values or {}))
            result = subprocess.run(["helm", "template", "smoke", str(CHART),
                                     "-f", str(overrides), *extra_args],
                                    text=True, capture_output=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result.stdout

    def test_default_retained_pvc_and_single_runner(self):
        output = self.render()
        self.assertEqual(output.count("kind: PersistentVolumeClaim"), 1)
        self.assertIn("helm.sh/resource-policy: keep", output)
        self.assertNotIn("storageClassName:", output)
        self.assertIn("replicas: 1", output)
        self.assertIn("type: Recreate", output)
        self.assertIn("automountServiceAccountToken: false", output)

    def test_existing_claim_digest_and_proxy(self):
        output = self.render({"persistence": {"existingClaim": "retained"},
                              "image": {"digest": "sha256:" + "a" * 64},
                              "extraEnv": [{"name": "https_proxy", "value": "http://proxy:8080"}]})
        self.assertNotIn("kind: PersistentVolumeClaim", output)
        self.assertIn('claimName: "retained"', output)
        self.assertEqual(output.count("image: \"public.ecr.aws/ubuntu/ubuntu@sha256:"), 2)
        self.assertEqual(output.count("name: https_proxy"), 2)

    def test_public_image_and_shared_packages_with_scripts(self):
        output = self.render()
        self.assertEqual(output.count('image: "public.ecr.aws/ubuntu/ubuntu:noble"'), 2)
        self.assertIn("checksum/scripts:", output)
        self.assertIn("kind: ConfigMap", output)
        for script in ("download-dependencies.sh", "start-container.sh", "runner.sh"):
            self.assertIn(f"  {script}: |", output)
        init, main = output.split("      initContainers:", 1)[1].split("      containers:", 1)
        self.assertNotIn("GITHUB_RUNNER_TOKEN", init)
        self.assertNotIn("mountPath: /persistent", init)
        self.assertIn("mountPath: /packages", init)
        self.assertIn("mountPath: /packages\n              readOnly: true", main)
        self.assertIn("args: [/bin/bash, /opt/runner-scripts/runner.sh]", main)

    def test_maintenance_prepares_environment_without_running_listener(self):
        output = self.render({"runner": {"maintenance": True}})
        self.assertIn("args: [/bin/sleep, infinity]", output)
        self.assertNotIn("args: [/bin/bash, /opt/runner-scripts/runner.sh]", output)

    def test_packaged_chart_includes_bootstrap_scripts(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run(["helm", "package", str(CHART), "--destination", temp],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            package = next(Path(temp).glob("*.tgz"))
            with tarfile.open(package) as archive:
                names = archive.getnames()
                for script in ("download-dependencies.sh", "start-container.sh", "runner.sh"):
                    self.assertIn(f"github-runner/scripts/{script}", names)
            rendered = subprocess.run(["helm", "template", "packaged", str(package)],
                                      capture_output=True, text=True, timeout=30)
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            self.assertIn("  download-dependencies.sh: |", rendered.stdout)

    def test_storage_class_empty_named_and_retention_disabled(self):
        for value in ("", "fast"):
            with self.subTest(storageClass=value):
                output = self.render({"persistence": {"storageClass": value, "retain": False}})
                self.assertIn(f'storageClassName: "{value}"', output)
                self.assertNotIn("helm.sh/resource-policy", output)

    def test_legacy_filename_remains_usable(self):
        self.assertEqual(self.render(), self.render(extra_args=("-f", str(CHART / "value.yaml"))))

    def test_invalid_configuration_rejected(self):
        for values in ({"runner": {"sha256": "wrong"}},
                       {"runner": {"workDir": "../outside"}},
                       {"runner": {"workDir": "bin"}},
                       {"github": {"repo": "https://github.com/owner/repo"}},
                       {"github": {"runner_init_version": "2.337.0"}},
                       {"nodeSelector": {"kubernetes.io/arch": "arm64"}},
                       {"extraEnv": [{"name": "GITHUB_REPO", "value": "other/repo"}]}):
            with self.subTest(values=values):
                self.render(values, success=False)


if __name__ == "__main__":
    unittest.main()
