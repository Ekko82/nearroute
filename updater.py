"""Stable GitHub releases and in-place updates for extracted NearRoute packages."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parent
REPOSITORY = "Ekko82/nearroute"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
CHECK_INTERVAL_SECONDS = 6 * 3600
MAX_PACKAGE_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
REQUIRED_FILES = {"VERSION", "optimizer.py", "dashboard.py", "updater.py", "tray_icon.py",
                  "windows_app.py", "launch.ps1", "install.ps1", "uninstall.ps1",
                  "config.example.json", "README.md", "LICENSE", "assets/nearroute.ico"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    url: str
    notes: str
    asset_url: str
    sha256: str
    size: int


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value):
        raise UpdateError("版本号必须为 X.Y.Z 格式的稳定版本")
    return tuple(int(part) for part in value.split("."))


def current_version(root=ROOT) -> str:
    value = (Path(root) / "VERSION").read_text(encoding="utf-8-sig").strip()
    version_tuple(value)
    return value


def _read_document(path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def _write_document(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".update-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_preferences(root=ROOT) -> dict:
    data = _read_document(Path(root) / "state" / "update-settings.json", {})
    return {"auto_check": data.get("auto_check", True) is True,
            "mode": data.get("mode") if data.get("mode") in ("notify", "download", "install") else "notify"}


def save_preferences(auto_check: bool, mode: str, root=ROOT) -> dict:
    if type(auto_check) is not bool or mode not in ("notify", "download", "install"):
        raise UpdateError("更新设置不正确")
    data = {"auto_check": auto_check, "mode": mode}
    _write_document(Path(root) / "state" / "update-settings.json", data)
    return data


def check_due(root=ROOT, now=None) -> bool:
    if not load_preferences(root)["auto_check"]:
        return False
    last = _read_document(Path(root) / "state" / "update-check.json", {}).get("last_checked", 0)
    if not isinstance(last, (int, float)):
        last = 0
    return (time.time() if now is None else now) - last >= CHECK_INTERVAL_SECONDS


def read_update_result(root=ROOT) -> dict:
    return _read_document(Path(root) / "state" / "update-result.json", {})


def cached_release(root=ROOT) -> Release | None:
    data = _read_document(Path(root) / "state" / "update-check.json", {}).get("release")
    if not isinstance(data, dict):
        return None
    try:
        release = Release(**data)
        _validate_release(release)
        return release if version_tuple(release.version) > version_tuple(current_version(root)) else None
    except (UpdateError, OSError, ValueError, TypeError):
        return None


def _https_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (ValueError, TypeError) as error:
        raise UpdateError("更新地址格式不正确") from error
    if (parsed.scheme != "https" or parsed.hostname not in
            {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
            or parsed.username is not None or parsed.password is not None or port not in (None, 443)):
        raise UpdateError("更新地址必须来自 GitHub 官方 HTTPS 下载服务")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(url):
    _https_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "NearRoute-Updater",
                                                  "Accept": "application/vnd.github+json"})
    return urllib.request.build_opener(SafeRedirect()).open(request, timeout=15)


def _read_url(url, limit):
    with _open_url(url) as response:
        _https_url(response.geturl())
        data = response.read(limit + 1)
        if len(data) > limit:
            raise UpdateError("更新元数据超过大小限制")
        return data


def _asset(metadata, name, tag):
    expected = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
    assets = metadata.get("assets", [])
    if not isinstance(assets, list):
        raise UpdateError("新版本的文件清单格式不正确")
    matches = [a for a in assets if isinstance(a, dict) and a.get("name") == name]
    if len(matches) != 1 or matches[0].get("browser_download_url") != expected:
        raise UpdateError("新版本的安装包尚未发布完整，请稍后检查")
    return matches[0]


def _validate_release(release):
    version_tuple(release.version)
    asset = f"NearRoute-{release.version}-windows.zip"
    if (release.tag != "v" + release.version
            or release.url != f"https://github.com/{REPOSITORY}/releases/tag/{release.tag}"
            or release.asset_url != f"https://github.com/{REPOSITORY}/releases/download/{release.tag}/{asset}"
            or not isinstance(release.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", release.sha256)
            or type(release.size) is not int or not 0 < release.size <= MAX_PACKAGE_BYTES):
        raise UpdateError("更新包的版本、来源或校验信息不正确")


def check_for_update(root=ROOT) -> Release | None:
    state = Path(root) / "state" / "update-check.json"
    try:
        local = version_tuple(current_version(root))
        try:
            metadata = json.loads(_read_url(RELEASES_URL, 512 * 1024))
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            _write_document(state, {"last_checked": time.time(), "latest_version": None})
            return None
        if not isinstance(metadata, dict):
            raise UpdateError("GitHub 返回的版本信息格式不正确")
        tag = metadata.get("tag_name", "")
        if metadata.get("draft") or metadata.get("prerelease"):
            _write_document(state, {"last_checked": time.time(), "latest_version": None})
            return None
        if not isinstance(tag, str) or not tag.startswith("v"):
            raise UpdateError("发布标签不是稳定版本标签")
        version = tag[1:]
        if version_tuple(version) <= local:
            _write_document(state, {"last_checked": time.time(), "latest_version": version})
            return None
        asset_name = f"NearRoute-{version}-windows.zip"
        asset = _asset(metadata, asset_name, tag)
        manifest_asset = _asset(metadata, "nearroute-update.json", tag)
        manifest = json.loads(_read_url(manifest_asset["browser_download_url"], 16 * 1024))
        if (not isinstance(manifest, dict) or manifest.get("schema") != 1
                or manifest.get("version") != version or manifest.get("asset") != asset_name
                or manifest.get("size") != asset.get("size")):
            raise UpdateError("更新清单与发布的安装包不匹配")
        release = Release(version, tag, metadata.get("html_url", ""),
                          str(metadata.get("body") or "暂无更新说明。")[:12000],
                          asset["browser_download_url"], manifest.get("sha256", ""), manifest.get("size"))
        _validate_release(release)
        _write_document(state, {"last_checked": time.time(), "latest_version": version, "release": asdict(release)})
        return release
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as error:
        _write_document(state, _read_document(state, {}) | {"last_checked": time.time(), "error": type(error).__name__})
        raise UpdateError("无法检查新版本，请确认能够连接 GitHub 后重试") from error
    except UpdateError:
        _write_document(state, _read_document(state, {}) | {"last_checked": time.time(), "error": "invalid_release"})
        raise


def can_install(root=ROOT) -> bool:
    root = Path(root).resolve()
    return not any((parent / ".git").exists() for parent in (root, *root.parents))


def _safe_member(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or path.is_absolute()
            or any(part in ("", ".", "..") or part.endswith((".", " ")) for part in name.split("/"))):
        return False
    if any(part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                           *(f"LPT{i}" for i in range(1, 10))} for part in path.parts):
        return False
    if len(path.parts) == 1:
        return (name in {"VERSION", "LICENSE", "README.md", "config.example.json"}
                or (re.fullmatch(r"[a-z][a-z0-9_]*\.(?:py|ps1)", name) and not name.startswith("test_")))
    return (len(path.parts) == 2 and path.parts[0] == "assets"
            and re.fullmatch(r"[A-Za-z0-9_-]+\.(?:ico|svg|png)", path.parts[1]))


def validate_package(archive, release):
    _validate_release(release)
    archive = Path(archive)
    if archive.stat().st_size != release.size or hashlib.sha256(archive.read_bytes()).hexdigest() != release.sha256:
        raise UpdateError("更新包校验失败，请重新下载")
    try:
        with zipfile.ZipFile(archive) as package:
            members = package.infolist()
            names = [item.filename for item in members]
            if (len(members) > 256 or len({name.casefold() for name in names}) != len(names)
                    or not REQUIRED_FILES.issubset(names)
                    or sum(item.file_size for item in members) > MAX_UNPACKED_BYTES):
                raise UpdateError("更新包的文件清单或大小不正确")
            for item in members:
                mode = item.external_attr >> 16
                if (not _safe_member(item.filename) or item.is_dir() or stat.S_ISLNK(mode)
                        or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)) or item.flag_bits & 1):
                    raise UpdateError("更新包包含不允许安装的路径或链接")
                content = package.read(item)
                if item.filename.endswith(".py"):
                    compile(content, item.filename, "exec")
            if package.read("VERSION").decode("utf-8-sig").strip() != release.version:
                raise UpdateError("更新包中的版本号不匹配")
            return names
    except (zipfile.BadZipFile, RuntimeError, SyntaxError, UnicodeError) as error:
        raise UpdateError("更新包损坏或程序文件无效，请重新下载") from error


def prepare_update(release: Release, root=ROOT) -> Path:
    _validate_release(release)
    root = Path(root).absolute()
    updates = _safe_target(root, "state/updates")
    updates.mkdir(parents=True, exist_ok=True)
    prepared = _safe_target(root, "state/updates/" + release.tag)
    if prepared.exists():
        try:
            validate_package(prepared / "package.zip", release)
            _write_document(prepared / "release.json", asdict(release))
            return prepared
        except (OSError, UpdateError):
            # Only this version's private download cache is discarded.
            shutil.rmtree(prepared)
    with tempfile.TemporaryDirectory(prefix=".download-", dir=updates) as temporary:
        archive = Path(temporary) / "package.zip"
        started = time.monotonic()
        try:
            with _open_url(release.asset_url) as response, archive.open("wb") as stream:
                _https_url(response.geturl())
                received = 0
                while block := response.read(128 * 1024):
                    received += len(block)
                    if received > release.size or time.monotonic() - started > 180:
                        raise UpdateError("更新包下载超过大小或时间限制")
                    stream.write(block)
        except (OSError, http.client.HTTPException) as error:
            raise UpdateError("下载失败，请确认能够连接 GitHub 后重试") from error
        validate_package(archive, release)
        _write_document(Path(temporary) / "release.json", asdict(release))
        os.replace(temporary, prepared)
    return prepared


def _safe_target(root, name):
    target = root / name
    for item in (root, *target.relative_to(root).parents):
        candidate = item if item.is_absolute() else root / item
        if _is_link(candidate):
            raise UpdateError("安装目录含有链接，请使用普通目录安装")
    if _is_link(target):
        raise UpdateError("程序文件是链接，已保留原文件")
    if not target.resolve().is_relative_to(root.resolve()):
        raise UpdateError("安装路径超出程序目录")
    return target


def _is_link(path):
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def apply_package(archive: Path, release: Release, root=ROOT):
    root = Path(root).absolute()
    if not can_install(root):
        raise UpdateError("源码检出目录请使用 git pull --ff-only 更新；内置安装适用于发行 ZIP")
    names = validate_package(archive, release)
    targets = {name: _safe_target(root, name) for name in names}
    backup = _safe_target(root, "state/updates/backup")
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir(parents=True)
    existed = set()
    for name, target in targets.items():
        if target.exists():
            if not target.is_file():
                raise UpdateError("程序文件位置被目录占用，已取消更新")
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
            existed.add(name)
    changed = []
    try:
        with zipfile.ZipFile(archive) as package:
            for name, target in targets.items():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".update-", suffix=".tmp", delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(package.read(name))
                    os.replace(temporary, target)
                    changed.append(name)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
    except Exception as error:
        rollback_errors = []
        for name in reversed(changed):
            try:
                target = targets[name]
                if name in existed:
                    shutil.copy2(backup / name, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                rollback_errors.append(name)
        if rollback_errors:
            raise UpdateError("更新未完成，自动还原失败；旧版备份保留在 state/updates/backup") from error
        raise UpdateError("更新未完成，已还原旧版程序") from error


class InstallLock:
    def __init__(self, root=ROOT):
        self.path = Path(root) / "state" / "update.lock"
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.stream.close()
            self.stream = None
            return False

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def is_installing(root=ROOT):
    if not (Path(root) / "state" / "update.lock").exists():
        return False
    lock = InstallLock(root)
    try:
        return not lock.acquire()
    finally:
        lock.close()


def start_installer(prepared: Path, *, parent_pid: int, python_exe: str, root=ROOT):
    if not can_install(root):
        raise UpdateError("源码检出目录请使用 git pull --ff-only 更新；内置安装适用于发行 ZIP")
    release = Release(**_read_document(Path(prepared) / "release.json", {}))
    validate_package(Path(prepared) / "package.zip", release)
    worker = Path(tempfile.mkdtemp(prefix="nearroute-updater-"))
    try:
        shutil.copy2(Path(__file__), worker / "updater.py")
        return subprocess.Popen(
            [python_exe, "-B", str(worker / "updater.py"), "--apply", str(Path(prepared).resolve()),
             "--root", str(Path(root).resolve()), "--parent", str(parent_pid), "--python", python_exe],
            cwd=worker, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW, close_fds=True)
    except Exception:
        shutil.rmtree(worker)
        raise


def _wait_for_parent(pid, timeout=30):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # Parent has already exited.
                return
            raise UpdateError("无法确认旧窗口已退出，已取消更新")
        try:
            if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
                raise UpdateError("旧窗口尚未退出，已取消更新")
        finally:
            kernel.CloseHandle(handle)
    else:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        raise UpdateError("旧窗口尚未退出，已取消更新")


def _run_installer(prepared, root, parent_pid, python_exe):
    root = Path(root).resolve()
    lock = InstallLock(root)
    helper_lock = None
    restart = False
    try:
        if not lock.acquire():
            return
        _wait_for_parent(parent_pid)
        restart = True
        release = Release(**_read_document(Path(prepared) / "release.json", {}))
        validate_package(Path(prepared) / "package.zip", release)
        if not can_install(root):
            raise UpdateError("源码检出目录不能使用内置安装")
        result = subprocess.run([python_exe, "-B", str(root / "optimizer.py"), "stop"], cwd=root,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=NO_WINDOW, timeout=35, check=False)
        if result.returncode:
            raise UpdateError("后台尚未退出，已取消更新")
        spec = importlib.util.spec_from_file_location("nearroute_update_core", root / "optimizer.py")
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        helper_lock = core.InstanceLock()
        if not helper_lock.acquire():
            raise UpdateError("后台已重新启动，已取消更新")
        apply_package(Path(prepared) / "package.zip", release, root)
        _write_document(root / "state" / "update-result.json",
                        {"success": True, "version": release.version, "message": "近舟已更新到 " + release.version})
    except Exception as error:
        message = str(error) if isinstance(error, UpdateError) else "更新未完成，请重试；用户配置已保留"
        _write_document(root / "state" / "update-result.json", {"success": False, "message": message})
    finally:
        if helper_lock is not None:
            helper_lock.close()
        lock.close()
        if restart:
            subprocess.Popen([python_exe, "-B", str(root / "dashboard.py")], cwd=root,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=NO_WINDOW, close_fds=True)


def main():
    parser = argparse.ArgumentParser(description="NearRoute release updater")
    parser.add_argument("--apply", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--parent", required=True, type=int)
    parser.add_argument("--python", required=True)
    args = parser.parse_args()
    try:
        _run_installer(args.apply, args.root, args.parent, args.python)
    finally:
        # This CLI runs from its own temporary copy, never from the installation.
        if ROOT.name.startswith("nearroute-updater-") and ROOT.parent == Path(tempfile.gettempdir()).resolve():
            os.chdir(tempfile.gettempdir())
            shutil.rmtree(ROOT)


if __name__ == "__main__":
    main()
