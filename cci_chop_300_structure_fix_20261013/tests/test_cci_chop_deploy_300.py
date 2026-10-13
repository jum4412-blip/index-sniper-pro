"""Shell update ordering against a fake EC2; no real service or network access."""
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


class DeployOrderingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stage = self.root / "stage"
        self.live = self.root / "live"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.log"
        self.active = self.root / "active"
        self.active.write_text("1")
        for directory in (self.stage, self.live):
            (directory / "cci_chop_v1/deploy").mkdir(parents=True)
            (directory / "cci_chop_v1/engine.py").write_text("# code\n")
            (directory / "cc").write_text("#!/bin/bash\nprintf 'cc:%s\\n' \"$*\" >> \"$FAKE_LOG\"\n")
            (directory / "cc").chmod(0o700)
        (self.stage / "cci_chop_v1/recovery.py").write_text("# recovery\n")
        (self.stage / "cci_chop_v1/deploy/check_flat_before_update.py").write_text("# flat\n")
        (self.stage / "cci_chop_v1/deploy/install_ec2.sh").write_text("#!/bin/bash\nprintf 'install\\n' >> \"$FAKE_LOG\"\n")
        (self.stage / "tests").mkdir()
        (self.stage / "README_CC300_KO.txt").write_text("instructions\n")
        source = Path(__file__).resolve().parents[1] / "cci_chop_v1/deploy/update_ec2_flat.sh"
        self.script = self.stage / "cci_chop_v1/deploy/update_ec2_flat.sh"
        shutil.copy2(source, self.script)
        (self.live / ".venv/bin").mkdir(parents=True)
        python = self.live / ".venv/bin/python"
        python.write_text("#!/bin/bash\nprintf 'python:%s\\n' \"$*\" >> \"$FAKE_LOG\"\n"
                          "if [[ \"$*\" == *check_flat_before_update.py* && \"${FAKE_FLAT_FAIL:-0}\" == 1 ]]; then exit 2; fi\n")
        python.chmod(0o700)
        systemctl = self.bin / "systemctl"
        systemctl.write_text("#!/bin/bash\nprintf 'systemctl:%s\\n' \"$*\" >> \"$FAKE_LOG\"\n"
                             "case \"$1\" in\n"
                             " is-active) [[ $(cat \"$FAKE_ACTIVE\") == 1 ]];;\n"
                             " stop) printf 0 > \"$FAKE_ACTIVE\";;\n"
                             " start|enable) printf 1 > \"$FAKE_ACTIVE\";;\n"
                             " *) exit 0;;\nesac\n")
        systemctl.chmod(0o700)
        sudo = self.bin / "sudo"
        sudo.write_text("#!/bin/bash\nexec \"$@\"\n")
        sudo.chmod(0o700)
        self.env = {**os.environ, "PATH": str(self.bin)+os.pathsep+os.environ["PATH"],
                    "FAKE_LOG": str(self.log), "FAKE_ACTIVE": str(self.active)}

    def tearDown(self):
        self.tmp.cleanup()

    def run_update(self, **extra):
        return subprocess.run(["bash", str(self.script), str(self.live)],
                              env={**self.env, **extra}, text=True, capture_output=True)

    def test_missing_release_file_never_stops_or_copies(self):
        (self.stage / "README_CC300_KO.txt").unlink()
        result = self.run_update()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.active.read_text(), "1")
        self.assertFalse((self.live / "cci_chop_v1/recovery.py").exists())

    def test_failed_flat_read_keeps_current_service_and_code(self):
        result = self.run_update(FAKE_FLAT_FAIL="1")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("systemctl:stop", self.log.read_text())
        self.assertEqual(self.active.read_text(), "1")
        self.assertFalse((self.live / "cci_chop_v1/recovery.py").exists())

    def test_flat_checks_before_stop_and_before_copy_recovery_arm_start(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        checks = [i for i, line in enumerate(calls) if "check_flat_before_update.py" in line]
        stop = next(i for i, line in enumerate(calls) if line.startswith("systemctl:stop"))
        recover = next(i for i, line in enumerate(calls) if line == "cc:recover-flat --mode live")
        arm = next(i for i, line in enumerate(calls) if line.startswith("cc:arm"))
        start = next(i for i, line in enumerate(calls) if line.startswith("systemctl:enable"))
        self.assertEqual(len(checks), 2)
        self.assertLess(checks[0], stop)
        self.assertLess(stop, checks[1])
        self.assertLess(checks[1], recover)
        self.assertLess(recover, arm)
        self.assertLess(arm, start)
        self.assertTrue((self.live / "cci_chop_v1/recovery.py").exists())
        self.assertEqual(len(list(self.live.glob("cci_code_before_300.*.tgz"))), 1)


if __name__ == "__main__":
    unittest.main()
