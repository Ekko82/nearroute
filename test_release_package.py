"""The public release includes only application files, with a matching digest."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from tools.build_release import build_release, MANIFEST_NAME, RELEASE_FILES


class ReleasePackageTests(unittest.TestCase):
    def test_release_whitelist_manifest_and_tag_guard(self):
        with tempfile.TemporaryDirectory(prefix="nearroute-release-test-") as temporary:
            root = Path(temporary) / "source"
            output = Path(temporary) / "dist"
            for name in RELEASE_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("0.1.0\n" if name == "VERSION" else name, encoding="utf-8")
            for name in ("config.json", "state/private.json", "state/github_publish.py", "test_private.py", ".git/config"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private test data", encoding="utf-8")

            manifest = build_release(root, output)
            self.assertEqual(manifest, json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8")))
            self.assertEqual(manifest["schema"], 1)
            self.assertEqual(manifest["version"], "0.1.0")
            self.assertEqual(manifest["asset"], "NearRoute-0.1.0-windows.zip")
            archive = output / manifest["asset"]
            self.assertEqual(manifest["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertEqual(manifest["size"], archive.stat().st_size)
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(set(bundle.namelist()), {
                    "VERSION", "optimizer.py", "dashboard.py", "tray_icon.py", "windows_app.py", "updater.py",
                    "install.ps1", "launch.ps1", "uninstall.ps1", "config.example.json", "README.md", "LICENSE",
                    "assets/nearroute.ico", "assets/nearroute.svg",
                })
                self.assertEqual(bundle.read("VERSION"), (root / "VERSION").read_bytes())
                self.assertIsNone(bundle.testzip())

            rejected_output = Path(temporary) / "rejected"
            result = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).resolve().parent / "tools" / "build_release.py"),
                 "--tag", "not-a-release-tag", "--output", str(rejected_output)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(rejected_output.exists())


if __name__ == "__main__":
    unittest.main()
