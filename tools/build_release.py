"""Build the public Windows source package without local configuration or state."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


RELEASE_FILES = (
    "VERSION",
    "optimizer.py",
    "client_detection.py",
    "dashboard.py",
    "tray_icon.py",
    "windows_app.py",
    "updater.py",
    "install.ps1",
    "launch.ps1",
    "uninstall.ps1",
    "config.example.json",
    "README.md",
    "LICENSE",
    "assets/nearroute.ico",
    "assets/nearroute.svg",
)
MANIFEST_NAME = "nearroute-update.json"


def read_version(root):
    version = (Path(root) / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("VERSION must contain a stable X.Y.Z version")
    return version


def build_release(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    version = read_version(root)
    for name in RELEASE_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Release file is missing or outside the project: {name}")

    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"NearRoute-{version}-windows.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in RELEASE_FILES:
            bundle.write(root / name, arcname=name)
    manifest = {
        "schema": 1,
        "version": version,
        "asset": archive.name,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "size": archive.stat().st_size,
    }
    (output / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / "dist")
    parser.add_argument("--tag", help="Require the exact stable release tag vVERSION")
    args = parser.parse_args()
    if args.tag is not None and args.tag != "v" + read_version(root):
        parser.error("--tag must match vVERSION")
    print(json.dumps(build_release(root, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
