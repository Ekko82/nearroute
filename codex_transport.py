"""Software-managed HTTP/SSE selection for idle Codex installations.

The UI records intent while Codex runs. An off-main-thread worker applies it
after normal exit, using metadata-only resumes for existing compatible chats.
Authentication files and model APIs are never accessed here.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import errno
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading

try:
    import tomllib
except ImportError:  # Windows launcher also supports Python 3.10.
    import toml_compat as tomllib


ROOT = Path(__file__).resolve().parent
PROVIDER_ID = "nearroute-http"
PROVIDER = {"name": "OpenAI", "wire_api": "responses", "requires_openai_auth": True,
            "supports_websockets": False, "supports_standalone_web_search": True}
_BLOCK = '''# NearRoute HTTP transport begin
[model_providers.nearroute-http]
name = "OpenAI"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
supports_standalone_web_search = true
# NearRoute HTTP transport end
'''
_LOCK = threading.RLock()
_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DESKTOP_FAMILY = "OpenAI.Codex_2p2nqsd0c76g0"
_DESKTOP_APP_ID = _DESKTOP_FAMILY + "!App"
_DESKTOP_REGISTRATION = """
$desktop = $null
$packages = @(Get-AppxPackage -Name 'OpenAI.Codex' | Where-Object { $_.PackageFamilyName -eq 'OpenAI.Codex_2p2nqsd0c76g0' })
if ($packages.Count -eq 1) {
    $package = $packages[0]
    $manifest = Get-AppxPackageManifest -Package $package.PackageFullName
    $applications = @($manifest.Package.Applications.Application | Where-Object { $_.Id -eq 'App' })
    if ($applications.Count -eq 1) {
        $desktop = @{family=$package.PackageFamilyName; application_id=$applications[0].Id;
                     install_location=$package.InstallLocation; executable=$applications[0].Executable}
    }
}
"""


class Refused(Exception):
    """A safe, user-facing reason that contains no raw config or process output."""


def _now():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _acquire_home_lock(home):
    """Serialize mutations across installations and Windows login sessions.

    Keep the inert file after release: unlinking a lock file can let different
    processes lock different files at the same path. The OS releases the handle
    and lock if the owning process exits unexpectedly.
    """
    home.mkdir(parents=True, exist_ok=True)
    stream = (home / ".nearroute-transport.lock").open("a+b")
    try:
        stream.seek(0, os.SEEK_END)
        if not stream.tell():
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except OSError as exc:
        stream.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or getattr(exc, "winerror", None) in (32, 33):
            return None
        raise
    except Exception:
        stream.close()
        raise


def _release_home_lock(stream):
    # Closing this handle releases its OS lock on both supported platforms.
    if stream is not None:
        stream.close()


def _powershell_json(script, failure):
    script = "$ErrorActionPreference = 'Stop'; [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); " + script
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                                creationflags=_FLAGS)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused(failure) from None
    if result.returncode:
        raise Refused(failure)
    try:
        return json.loads(result.stdout.decode("utf-8-sig")) if result.stdout.strip() else None
    except (ValueError, UnicodeError):
        raise Refused(failure) from None


def _registered_desktop(value):
    if not isinstance(value, dict):
        return None
    location, executable = value.get("install_location"), value.get("executable")
    if (value.get("family") != _DESKTOP_FAMILY or value.get("application_id") != "App"
            or not isinstance(location, str) or not ntpath.isabs(location)
            or not isinstance(executable, str)
            or ntpath.normcase(executable.replace("/", "\\")) != r"app\chatgpt.exe"):
        return None
    return {"app_id": _DESKTOP_APP_ID, "executable": ntpath.join(location, executable)}


def process_inventory():
    if os.name != "nt":
        raise Refused("此功能需要 Windows，并须先确认 Codex 已正常退出。")
    failure = "无法确认 Codex 进程状态，尚未修改配置。"
    script = _DESKTOP_REGISTRATION + """
$processes = @(Get-CimInstance Win32_Process -Filter "Name = 'codex.exe' OR Name = 'ChatGPT.exe'" |
              Select-Object ProcessId,Name,ExecutablePath)
@{desktop=$desktop; processes=$processes} | ConvertTo-Json -Depth 4 -Compress
"""
    document = _powershell_json(script, failure)
    if not isinstance(document, dict) or not isinstance(document.get("processes"), list):
        raise Refused(failure)
    rows = document["processes"]
    if any(not isinstance(row, dict) or type(row.get("ProcessId")) is not int for row in rows):
        raise Refused(failure)
    if any(str(row.get("Name", "")).casefold() == "chatgpt.exe" and not row.get("ExecutablePath") for row in rows):
        raise Refused("有无法确认身份的 ChatGPT 桌面进程，尚未修改配置；请正常退出相关应用后重试。")
    desktop = _registered_desktop(document.get("desktop"))
    return [row for row in rows if str(row.get("Name", "")).casefold() == "codex.exe"
            or (desktop and isinstance(row.get("ExecutablePath"), str)
                and ntpath.normcase(ntpath.normpath(row["ExecutablePath"]))
                == ntpath.normcase(ntpath.normpath(desktop["executable"])))]


def _require_user_launch():
    if os.name != "nt":
        raise Refused("自动打开需要 Windows 上已安装的 Codex 桌面应用；可取消勾选后继续。")
    import ctypes
    if ctypes.windll.shell32.IsUserAnAdmin():
        raise Refused("请以普通用户运行近舟后自动打开 Codex，或取消勾选并在切换后手动打开。")


def discover_desktop():
    """Resolve the official registered desktop AppID, without copying process arguments."""
    _require_user_launch()
    document = _powershell_json(_DESKTOP_REGISTRATION + "$desktop | ConvertTo-Json -Compress",
                                "无法读取 Codex 桌面应用注册信息；可取消自动打开后继续。")
    desktop = _registered_desktop(document)
    if desktop is None:
        raise Refused("未找到已注册的 Codex 桌面应用；可取消自动打开后继续，切换完成后手动打开。")
    return desktop["app_id"]


def open_desktop(app_id):
    """Ask the Windows shell to open the captured official app, never its CLI engine."""
    if app_id != _DESKTOP_APP_ID:
        raise Refused("保存的 Codex 打开方式无效，请手动打开 Codex。")
    if discover_desktop() != app_id:
        raise Refused("Codex 桌面应用注册信息已变化，请手动打开 Codex。")
    explorer = ntpath.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")
    try:
        subprocess.Popen([explorer, "shell:AppsFolder\\" + app_id], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_FLAGS)
    except OSError:
        raise Refused("未能自动打开 Codex，请手动打开。") from None


def _applied_explanation(result):
    text = ("HTTP/SSE 配置已启用；实际传输方式待后续请求日志确认。" if result["mode"] == "http"
            else "已恢复原传输配置。")
    if result.get("reopened"):
        return text + "已请求自动打开 Codex。"
    if result.get("reopen_error"):
        return text + "自动打开未完成，请手动打开 Codex。"
    if result.get("reopen_skipped"):
        return text + "Codex 已在运行，无需重复打开。"
    return text + "请重新打开 Codex 使用。"


def discover_engine(rows=()):
    """Prefer the running desktop engine, then the newest installed desktop engine."""
    for row in rows:
        value = row.get("ExecutablePath")
        if value:
            path = Path(value)
            if path.name.lower() == "codex.exe" and path.parent.parent.name.lower() == "bin" and path.is_file():
                return path
    local = os.environ.get("LOCALAPPDATA")
    if local:
        paths = list((Path(local) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"))
        if paths:
            return max(paths, key=lambda item: item.stat().st_mtime_ns)
    return None


def _read_config(path):
    data = path.read_bytes() if path.exists() else b""
    try:
        parsed = tomllib.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise Refused("Codex 配置无法解析，请先修复配置文件。") from None
    return data, parsed


def _compatible(provider):
    return (isinstance(provider, dict) and provider.get("name") == "OpenAI"
            and provider.get("wire_api") == "responses"
            and provider.get("requires_openai_auth") is True
            and provider.get("supports_websockets") is False
            and isinstance(provider.get("base_url", "https://chatgpt.com/backend-api/codex"), str)
            and provider.get("base_url", "https://chatgpt.com/backend-api/codex").rstrip("/")
            in ("https://chatgpt.com/backend-api/codex", "https://api.openai.com/v1")
            and not any(key in provider for key in ("env_key", "experimental_bearer_token", "auth", "gateway_oauth")))


def _check_config(config, owned):
    profiles = config.get("profiles", {})
    if config.get("profile") is not None or not isinstance(profiles, dict):
        raise Refused("当前选用了 Codex profile，请先在 Codex 中确认其配置；此版本暂不自动切换。")
    for profile in profiles.values():
        if not isinstance(profile, dict) or any(key in profile for key in ("model_provider", "model_providers")):
            raise Refused("Codex profile 会覆盖模型服务商，此版本暂不自动切换。")
    providers = config.get("model_providers", {})
    if not isinstance(providers, dict):
        raise Refused("Codex 模型服务商配置格式不受支持。")
    selected = config.get("model_provider", "openai")
    if not isinstance(selected, str):
        raise Refused("Codex 模型服务商选择格式不受支持。")
    if selected == PROVIDER_ID:
        if not owned or providers.get(PROVIDER_ID) != PROVIDER:
            raise Refused("发现非本软件管理或已修改的 nearroute-http 配置，请先检查。")
    elif selected != "openai" and not _compatible(providers.get(selected)):
        raise Refused("当前使用自定义模型服务商，此功能仅自动管理 OpenAI / ChatGPT 配置。")
    elif selected == "openai" and "openai" in providers:
        raise Refused("当前覆盖了内置 OpenAI 服务商，此版本暂不自动切换。")
    if PROVIDER_ID in providers and (not owned or providers[PROVIDER_ID] != PROVIDER):
        raise Refused("nearroute-http 名称已有其他配置，尚未覆盖。")


def _root_line(text):
    # Only a simple top-level selection is edited. Semantic validation below
    # rejects inline/dotted/multiline forms instead of reserializing user TOML.
    prefix = re.split(r"(?m)^\s*\[", text, maxsplit=1)[0]
    matches = list(re.finditer(r'(?m)^[ \t]*model_provider[ \t]*=[^\r\n]*(?:\r?\n|$)', prefix))
    if len(matches) != 1:
        raise Refused("模型服务商选择使用了复杂 TOML 写法，请先在 Codex 中整理该字段。")
    return matches[0]


def prepare_http(original, config, ownership=None):
    """Preserve all text except the root selection and a marked owned block."""
    text = original.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    owned = copy.deepcopy(ownership) if ownership else {"targets": {}}
    capture_root = not ownership or config.get("model_provider") != PROVIDER_ID
    if capture_root:
        owned.update(original_provider_present="model_provider" in config,
                     original_provider=config.get("model_provider"))
    selected_line = 'model_provider = "' + PROVIDER_ID + '"' + newline
    if "model_provider" in config:
        match = _root_line(text)
        if capture_root:
            owned["original_root_line"] = match.group()
        text = text[:match.start()] + selected_line + text[match.end():]
    else:
        text = selected_line + text
    expected = copy.deepcopy(config)
    expected["model_provider"] = PROVIDER_ID
    if PROVIDER_ID not in config.get("model_providers", {}):
        block = _BLOCK.replace("\n", newline)
        separator = "" if not text or text.endswith(newline * 2) else (newline if text.endswith(newline) else newline * 2)
        addition = separator + block
        text += addition
        owned["provider_addition"] = addition
        owned["providers_table_present"] = "model_providers" in config
        expected.setdefault("model_providers", {})[PROVIDER_ID] = dict(PROVIDER)
    if tomllib.loads(text) != expected:
        raise Refused("切换会影响其他 TOML 字段，尚未修改配置。")
    candidate = (b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")
    return candidate, expected, owned


def prepare_original(original, config, owned, retain_provider=False):
    if config.get("model_provider") != PROVIDER_ID or config.get("model_providers", {}).get(PROVIDER_ID) != PROVIDER:
        raise Refused("本软件管理的配置已被外部修改，请检查后重试；其他设置已保留。")
    text = original.decode("utf-8-sig")
    match = _root_line(text)
    replacement = owned.get("original_root_line", "") if owned["original_provider_present"] else ""
    text = text[:match.start()] + replacement + text[match.end():]
    expected = copy.deepcopy(config)
    if owned["original_provider_present"]:
        expected["model_provider"] = owned["original_provider"]
    else:
        expected.pop("model_provider", None)
    if not retain_provider:
        addition = owned.get("provider_addition")
        if not addition or text.count(addition) != 1:
            raise Refused("本软件的服务商配置块已被编辑，尚未覆盖。")
        text = text.replace(addition, "", 1)
        expected["model_providers"].pop(PROVIDER_ID)
        if not expected["model_providers"] and not owned.get("providers_table_present"):
            expected.pop("model_providers")
    if tomllib.loads(text) != expected:
        raise Refused("恢复会影响其他 TOML 字段，尚未修改配置。")
    candidate = (b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")
    return candidate, expected


def _atomic_write(path, content, expected=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".nearroute-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not None and (path.read_bytes() if path.exists() else b"") != expected:
            raise Refused("Codex 配置同时发生了变化，已保留外部修改。")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _engine_run(engine, home, arguments):
    environment = dict(os.environ, CODEX_HOME=str(home))
    try:
        completed = subprocess.run([str(engine), *arguments], env=environment, cwd=home,
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=45,
                                   creationflags=_FLAGS)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused("Codex 引擎验证未完成，请检查安装后重试。") from None
    if completed.returncode:
        raise Refused("当前 Codex 引擎不支持所需的配置或元数据接口，尚未继续切换。")
    return completed.stdout


def validate_capabilities(engine, home, state):
    version = _engine_run(engine, home, ["--version"]).decode("utf-8", errors="replace").strip()
    match = re.fullmatch(r"codex-cli (\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9.+-]+)?", version)
    if not match or tuple(map(int, match.groups())) < (0, 166, 0):
        raise Refused("当前 Codex 版本不支持经验证的会话迁移接口，请更新 Codex 后重试。")
    state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="schema-", dir=state) as output:
        _engine_run(engine, home, ["app-server", "generate-json-schema", "--experimental", "--out", output])
        try:
            params = json.loads((Path(output) / "v2" / "ThreadResumeParams.json").read_text(encoding="utf-8"))
            response = json.loads((Path(output) / "v2" / "ThreadResumeResponse.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise Refused("当前 Codex 的会话迁移接口格式不受支持。") from None
        required = {"threadId", "modelProvider", "model", "config", "excludeTurns"}
        if not required <= params.get("properties", {}).keys() or not {"modelProvider", "model", "reasoningEffort", "thread"} <= response.get("properties", {}).keys():
            raise Refused("当前 Codex 缺少保留模型和推理强度所需的会话迁移能力。")
    return {"version": version, "metadata_resume_supported": True}


def validate_startup(engine, home, provider_id):
    output = _engine_run(engine, home, ["app-server", "--print-startup-config"])
    try:
        node = json.loads(output)
    except (ValueError, UnicodeError):
        # Older engines may return human-readable settings. Exit 0 is still
        # paired with strict parsed TOML equality and schema/postresume checks.
        return {"startup_validated": True, "safe_fields_available": False}
    for _ in range(4):
        if not isinstance(node, dict):
            break
        if "model_provider_id" in node:
            if node["model_provider_id"] != provider_id:
                raise Refused("Codex 实际生效的服务商与目标配置不一致。")
            provider = node.get("model_provider", node.get("provider", node))
            if provider_id == PROVIDER_ID and isinstance(provider, dict):
                for key, expected in (("supports_websockets", False), ("requires_openai_auth", True)):
                    if key in provider and provider[key] != expected:
                        raise Refused("Codex 实际生效的传输或认证配置与目标不一致。")
            return {"startup_validated": True, "safe_fields_available": True}
        node = next((node[key] for key in ("config", "startup_config", "startupConfig") if isinstance(node.get(key), dict)), None)
    return {"startup_validated": True, "safe_fields_available": False}


def _threads(home, config):
    directory_value = os.environ.get("CODEX_SQLITE_HOME") or config.get("sqlite_home") or home
    if not isinstance(directory_value, (str, Path)):
        raise Refused("Codex 会话数据库目录格式不受支持。")
    directory = Path(directory_value)
    if not directory.is_absolute():
        raise Refused("Codex 会话数据库目录不是绝对路径，尚未自动切换。")
    databases = [(int(match[1]), path) for path in directory.glob("state_*.sqlite")
                 if (match := re.fullmatch(r"state_(\d+)\.sqlite", path.name))]
    if not databases:
        return []
    database = max(databases, key=lambda item: item[0])[1]
    try:
        db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute(
                "SELECT id,model_provider,model,reasoning_effort,archived,rollout_path FROM threads "
                "WHERE model_provider IN ('openai','nearroute-http')")]
            from codex_history import comparison_path, Refused as HistoryRefused
            for row in rows:
                row["local_rollout_available"] = False
                value = row.get("rollout_path")
                if not isinstance(value, str) or not value:
                    continue
                try:
                    path = Path(value)
                    comparison_path(path)
                    path = path.resolve(strict=True)
                    comparison_path(path).relative_to(comparison_path(home.resolve()))
                    row["local_rollout_available"] = path.is_file() and row["id"] in path.name
                except (OSError, ValueError, HistoryRefused):
                    pass  # Synced/relocated records remain untouched on this device.
            return rows
        finally:
            db.close()
    except sqlite3.Error:
        raise Refused("Codex 会话数据库暂不可读或格式不受支持，请稍后重试。") from None


def _eligible(rows, provider):
    return [row for row in rows if not row["archived"] and row["model_provider"] == provider
            and row.get("local_rollout_available") is True
            and all(isinstance(row[key], str) and bool(row[key].strip()) for key in ("model", "reasoning_effort"))]


def _verify_checkpoint(rows, checkpoint):
    """Reuse the first attempt's evidence before accepting a new baseline."""
    by_id = {row["id"]: row for row in rows}
    for thread_id, settings in checkpoint.get("settings", {}).items():
        row = by_id.get(thread_id)
        if (row is None or row["archived"] or row["model_provider"] not in ("openai", PROVIDER_ID)
                or any(row[key] != settings[key] for key in ("model", "reasoning_effort"))):
            raise Refused("上次切换涉及的对话设置已发生变化。备份已保留，请先检查恢复记录。")
    for directory in checkpoint.get("backups", []):
        try:
            manifest = json.loads((Path(directory) / "manifest.json").read_text(encoding="utf-8"))
            for item in manifest["targets"]:
                digest = hashlib.sha256()
                remaining = item["bytes"]
                with Path(item["original_path"]).open("rb") as stream:
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("truncated original rollout")
                        digest.update(chunk)
                        remaining -= len(chunk)
                if digest.hexdigest() != item["prefix_sha256"]:
                    raise ValueError("changed original rollout prefix")
        except (OSError, ValueError, KeyError, TypeError):
            raise Refused("上次切换的原始对话记录校验未通过。备份已保留，请先检查恢复记录。") from None


def _operation_targets(rows, owned, mode):
    checkpoint = owned.get("checkpoint", {}) if owned else {}
    _verify_checkpoint(rows, checkpoint)
    source = "openai" if mode == "http" else PROVIDER_ID
    targets = {row["id"]: row for row in _eligible(rows, source)}
    # A lost response can follow a persisted provider change. Resume those same
    # IDs again rather than treating an empty source-provider query as success.
    targets.update({row["id"]: row for row in rows if row["id"] in checkpoint.get("settings", {})})
    return list(targets.values())


def _prepare_checkpoint(owned, targets):
    checkpoint = owned.setdefault("checkpoint", {"settings": {}, "backups": []})
    for row in targets:
        checkpoint["settings"].setdefault(row["id"], {key: row[key] for key in ("model", "reasoning_effort")})


def _record_checkpoint_result(owned, result):
    directory = result.get("backup_directory")
    backups = owned["checkpoint"]["backups"]
    if directory and directory not in backups:
        backups.append(directory)


class TransportManager:
    def __init__(self, root=None, home=None, engine=None, *, process_check=None, migrate=None,
                 capability_check=None, startup_check=None, preflight=None):
        self.root = Path(root) if root is not None else ROOT
        self.home = (Path(home) if home is not None else Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")).expanduser().resolve()
        self.engine = Path(engine).resolve() if engine is not None else None
        self.state = self.root / "state" / "codex-transport"
        self.config = self.home / "config.toml"
        self.process_check = process_check or process_inventory
        self.migrate = migrate
        self.capability_check = capability_check or validate_capabilities
        self.startup_check = startup_check or validate_startup
        self.preflight = preflight

    def _load(self, name):
        path = self.state / (name + ".json")
        if not path.exists():
            return None
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (OSError, ValueError):
            raise Refused("NearRoute 传输切换记录不可读，请保留 state/codex-transport 以便检查。") from None

    def _save(self, name, value):
        _atomic_write(self.state / (name + ".json"), json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))

    def _journal(self, event, **fields):
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / "journal.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": _now(), "event": event, **fields}, ensure_ascii=False) + "\n")

    def _context(self):
        rows = self.process_check()
        engine = self.engine or discover_engine(rows)
        data, config = _read_config(self.config)
        owned = self._load("ownership")
        if owned and owned.get("home") != str(self.home):
            raise Refused("Codex 主目录与已保存的切换记录不同，请先切回原主目录。")
        _check_config(config, owned)
        return bool(rows), engine, data, config, owned

    def inspect(self):
        with _LOCK:
            result = {"ok": True, "mode": "unknown", "status": "ready", "running": None,
                      "engine_available": False, "pending": None, "explanation": "",
                      "affected_count": 0, "skipped_count": 0, "supported": False,
                      "can_request": False, "can_restore": False, "owned": False, "compatible_http": False,
                      "reopen_requested": False, "reopened": False, "reopen_error": "", "reopen_skipped": False,
                      "last_result": None}
            try:
                pending = self._load("pending")
                result["pending"] = pending.get("mode") if pending else None
                result["reopen_requested"] = bool(pending and pending.get("reopen") is True)
                running, engine, data, config, owned = self._context()
                result.update(running=running, engine_available=bool(engine and engine.is_file()), owned=bool(owned))
                selected = config.get("model_provider", "openai")
                compatible = _compatible(config.get("model_providers", {}).get(selected))
                result.update(mode="http" if compatible else "original", compatible_http=compatible,
                              supported=True, can_request=result["engine_available"],
                              can_restore=bool(owned and selected == PROVIDER_ID and result["engine_available"]))
                rows = _threads(self.home, config)
                source = PROVIDER_ID if pending and pending.get("mode") == "original" else "openai"
                eligible = _eligible(rows, source)
                result.update(affected_count=len(eligible), skipped_count=sum(not row["archived"] and row["model_provider"] == source for row in rows) - len(eligible))
                if pending:
                    if pending.get("home") != str(self.home):
                        raise Refused("待执行请求属于另一个 Codex 主目录，请取消后重新选择。")
                    if pending.get("error"):
                        result.update(ok=False, status="error", explanation=pending["error"])
                    else:
                        text = "切换尚未生效；请在任务结束后正常退出 Codex，近舟将自动应用。" if running else "切换尚未生效，正在等待后台应用。"
                        if result["reopen_requested"]:
                            text += "完成后会自动打开 Codex。"
                        result.update(status="pending", explanation=text)
                elif compatible:
                    result["explanation"] = ("HTTP/SSE 配置已启用；实际传输方式待后续请求日志确认。" if selected == PROVIDER_ID
                                             else "已检测到现有 HTTP/SSE 配置，尚未改动；实际传输方式待后续请求日志确认。")
                else:
                    result["explanation"] = "当前使用 Codex 原有传输设置。"
                if not pending:
                    last = self._load("last-result")
                    if (last and last.get("home") == str(self.home) and last.get("mode") == result["mode"]
                            and last.get("provider") == selected):
                        result.update(last_result=last, reopen_requested=last.get("reopen_requested") is True,
                                      reopened=last.get("reopened") is True, reopen_error=str(last.get("reopen_error") or ""),
                                      reopen_skipped=last.get("reopen_skipped") is True,
                                      explanation=_applied_explanation(last))
                if not result["engine_available"]:
                    result["explanation"] = "未找到 Codex 桌面引擎，请安装或启动 Codex 后刷新。"
            except (Refused, OSError) as exc:
                result.update(ok=False, status="unsupported", supported=False,
                              can_request=False, can_restore=False,
                              explanation=str(exc) if isinstance(exc, Refused) else "无法读取传输设置，请检查文件访问权限。")
            return result

    def request_mode(self, mode, *, reopen=False):
        with _LOCK:
            if mode not in ("http", "original"):
                raise ValueError("mode must be 'http' or 'original'")
            if type(reopen) is not bool:
                raise ValueError("reopen must be a boolean")
            status = self.inspect()
            allowed = status["can_request"] if mode == "http" else status["can_restore"]
            if not allowed:
                return dict(status, ok=False, explanation=status["explanation"] or "没有可恢复的 NearRoute 管理配置。")
            pending = {"mode": mode, "home": str(self.home), "requested_at": _now(), "reopen": reopen}
            if reopen:
                try:
                    app_id = discover_desktop()
                    if app_id != _DESKTOP_APP_ID:
                        raise Refused("Codex 桌面应用注册信息无效；可取消自动打开后继续。")
                except Refused as exc:
                    return dict(status, ok=False, request_error=str(exc), explanation=str(exc))
                pending["reopen_app_id"] = app_id
            self._save("pending", pending)
            self._journal("requested", mode=mode, reopen=reopen)
            return self.inspect()

    def cancel_pending(self):
        with _LOCK:
            (self.state / "pending.json").unlink(missing_ok=True)
            self._journal("cancelled")
            return self.inspect()

    def _assert_idle(self):
        if self.process_check():
            raise Refused("Codex 已重新启动，请正常退出后重新点击切换。")

    def _write_and_validate(self, engine, before, candidate, expected, selected):
        self._assert_idle()
        backup = self.state / "backups" / (_now() + "-config.toml")
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(before)
        _atomic_write(self.config, candidate, before)
        try:
            self.startup_check(engine, self.home, selected)
            persisted, parsed = _read_config(self.config)
            if persisted != candidate or parsed != expected:
                raise Refused("Codex 配置在验证期间发生变化，已保留外部修改。")
            self._assert_idle()
        except Exception:
            # A user may relaunch Codex during validation. Do not touch its
            # configuration once it is running, including automatic restoration.
            if not self.process_check() and self.config.read_bytes() == candidate:
                _atomic_write(self.config, before, candidate)
            raise
        return str(backup)

    def process_pending(self):
        with _LOCK:
            try:
                pending = self._load("pending")
            except Refused:
                return self.inspect()
            if not pending or pending.get("error"):
                return self.inspect()
            home_lock = None
            try:
                if pending.get("home") != str(self.home):
                    raise Refused("待执行请求属于另一个 Codex 主目录。")
                if (type(pending.get("reopen", False)) is not bool
                        or (pending.get("reopen") and pending.get("reopen_app_id") != _DESKTOP_APP_ID)):
                    raise Refused("保存的 Codex 自动打开请求无效，请取消待切换后重新选择。")
                if self.process_check():
                    return self.inspect()
                home_lock = _acquire_home_lock(self.home)
                if home_lock is None:
                    return dict(self.inspect(), ok=True, status="pending", pending=pending["mode"],
                                explanation="另一个 NearRoute 正在切换此 Codex 主目录，当前请求已保留，将稍后重试。")
                status = self.inspect()
                if status["status"] == "unsupported":
                    return status
                running, engine, before, config, owned = self._context()
                if running:
                    return self.inspect()
                if not engine or not engine.is_file():
                    raise Refused("未找到 Codex 桌面引擎，请启动 Codex 后重新选择。")
                self._assert_idle()
                capabilities = self.capability_check(engine, self.home, self.state)
                self._journal("validated_capabilities", **capabilities)
                rows = _threads(self.home, config)
                targets = _operation_targets(rows, owned, pending["mode"])
                if pending["mode"] == "http":
                    candidate, expected, owned = prepare_http(before, config, owned)
                    targets = self._preflight(expected, targets, PROVIDER_ID,
                                              allow_skip=not bool(owned.get("checkpoint")))
                    owned["home"] = str(self.home)
                    for row in targets:
                        owned.setdefault("targets", {}).setdefault(row["id"], row["model_provider"])
                    _prepare_checkpoint(owned, targets)
                    owned["stage"] = "prepared"
                    self._save("ownership", owned)
                    backup = self._write_and_validate(engine, before, candidate, expected, PROVIDER_ID)
                    owned.update(stage="config_applied", config_backup=backup)
                    self._save("ownership", owned)
                    result = self._migrate(engine, targets, PROVIDER_ID)
                    _record_checkpoint_result(owned, result)
                    owned["stage"] = "applied" if result.get("ok") else "migration_stopped"
                    if result.get("ok"):
                        owned.pop("checkpoint", None)
                    self._save("ownership", owned)
                else:
                    if not owned or config.get("model_provider") != PROVIDER_ID:
                        raise Refused("没有可恢复的 NearRoute 管理配置。")
                    mapping = {row["id"]: owned.get("targets", {}).get(row["id"], "openai") for row in targets}
                    # Validate text before any resume. Chats without saved exact
                    # settings and archived chats retain a usable definition.
                    retain = any(row["model_provider"] == PROVIDER_ID and row not in targets for row in rows)
                    candidate, expected = prepare_original(before, config, owned, retain)
                    self._preflight(config, targets, None, mapping)
                    _prepare_checkpoint(owned, targets)
                    self._save("ownership", owned)
                    result = self._migrate(engine, targets, None, mapping)
                    _record_checkpoint_result(owned, result)
                    self._save("ownership", owned)
                    if result.get("ok"):
                        backup = self._write_and_validate(engine, before, candidate, expected, expected.get("model_provider", "openai"))
                        owned.update(stage="restored", config_backup=backup, provider_retained=retain)
                        owned.pop("checkpoint", None)
                        self._save("ownership", owned)
                self._journal("migration", mode=pending["mode"], result=result)
                if not result.get("ok"):
                    raise Refused("部分对话切换尚未完成，备份已保留。可重试或恢复原设置。")
                (self.state / "pending.json").unlink(missing_ok=True)
                self._journal("completed", mode=pending["mode"], target_count=len(targets))
                target_ids = {row["id"] for row in targets}
                completed = {"mode": pending["mode"], "home": str(self.home), "completed_at": _now(),
                             "provider": expected.get("model_provider", "openai"),
                             "processed_count": len(targets),
                             "preserved_count": sum(not row["archived"] and row["model_provider"] ==
                                                    ("openai" if pending["mode"] == "http" else PROVIDER_ID)
                                                    and row["id"] not in target_ids for row in rows),
                             "reopen_requested": pending.get("reopen") is True, "reopened": False,
                             "reopen_error": "", "reopen_skipped": False}
                # Consume the switch intent before opening the app. A launch failure
                # cannot retry a completed migration or repeatedly start Codex.
                self._save("last-result", completed)
                if completed["reopen_requested"]:
                    try:
                        if self.process_check():
                            completed["reopen_skipped"] = True
                        else:
                            open_desktop(pending["reopen_app_id"])
                            completed["reopened"] = True
                    except Exception as exc:
                        completed["reopen_error"] = (str(exc) if isinstance(exc, Refused)
                                                     else "自动打开未完成，请手动打开 Codex。")
                    self._save("last-result", completed)
                return dict(self.inspect(), ok=True, status="applied", last_result=completed,
                            reopen_requested=completed["reopen_requested"], reopened=completed["reopened"],
                            reopen_error=completed["reopen_error"], reopen_skipped=completed["reopen_skipped"],
                            explanation=_applied_explanation(completed))
            except Exception as exc:
                reason = str(exc) if isinstance(exc, Refused) else "切换未完成，备份和切换记录已保留，请检查后重试。"
                pending["error"] = reason
                self._save("pending", pending)
                self._journal("stopped", mode=pending["mode"], reason=reason, error_type=type(exc).__name__)
                return dict(self.inspect(), ok=False, status="error", explanation=reason)
            finally:
                _release_home_lock(home_lock)

    def _preflight(self, config, targets, provider, original=None, *, allow_skip=False):
        if not targets:
            return []
        from codex_history import inspect_targets, Refused as HistoryRefused
        check = self.preflight or inspect_targets
        valid, skipped, total_bytes, database_bytes = [], [], 0, 0
        try:
            for row in targets:
                try:
                    _database, size, inspected = check(self.home, config, [row["id"]], provider, original or {})
                except (HistoryRefused, OSError, ValueError) as error:
                    if not allow_skip:
                        raise
                    skipped.append({"thread_id": row["id"], "error_type": type(error).__name__,
                                    "reason": str(error) if isinstance(error, HistoryRefused) else "unavailable rollout metadata"})
                    continue
                database_bytes = max(database_bytes, size)
                total_bytes += sum(item["size"] for item in inspected)
                valid.append(row)
            self._save("preflight", {"home": str(self.home), "provider": provider,
                                     "checked_at": _now(), "valid_count": len(valid),
                                     "backup_bytes": database_bytes + total_bytes,
                                     "skipped": skipped})
            if not valid:
                raise Refused("本地聊天记录均未通过预检查，尚未修改 Codex 配置；原记录已保留，请检查诊断记录。")
            required = database_bytes + total_bytes + len(self.config.read_bytes()) + 65536
            if shutil.disk_usage(self.home).free < required:
                raise Refused("备份空间不足，尚未修改 Codex 配置；请清理磁盘空间后重试。")
            return valid
        except Refused:
            raise
        except Exception as error:
            self._journal("preflight_failed", error_type=type(error).__name__,
                          reason=str(error) if isinstance(error, HistoryRefused) else "metadata validation failed")
            raise Refused("本地聊天记录预检查未通过，尚未修改 Codex 配置；原记录已保留，请检查诊断记录。") from None

    def _migrate(self, engine, targets, provider, original=None):
        if not targets:
            return {"ok": True, "changed": False, "target_count": 0}
        self._assert_idle()
        migrate = self.migrate
        if migrate is None:
            from codex_history import migrate
        return migrate(engine, self.home, [row["id"] for row in targets], provider, original=original)


def inspect(**kwargs):
    return TransportManager(**kwargs).inspect()


def request_mode(mode, *, reopen=False, **kwargs):
    return TransportManager(**kwargs).request_mode(mode, reopen=reopen)


def process_pending(**kwargs):
    return TransportManager(**kwargs).process_pending()


def cancel_pending(**kwargs):
    return TransportManager(**kwargs).cancel_pending()
