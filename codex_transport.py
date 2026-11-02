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
import os
from pathlib import Path
import re
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


def process_inventory():
    if os.name != "nt":
        raise Refused("此功能需要 Windows，并须先确认 Codex 已正常退出。")
    script = ("Get-CimInstance Win32_Process -Filter \"Name = 'codex.exe'\" | "
              "Select-Object ProcessId,Name,ExecutablePath | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                                creationflags=_FLAGS)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused("无法确认 Codex 进程状态，尚未修改配置。") from None
    if result.returncode:
        raise Refused("无法确认 Codex 进程状态，尚未修改配置。")
    try:
        rows = json.loads(result.stdout.decode("utf-8-sig")) if result.stdout.strip() else []
    except (ValueError, UnicodeError):
        raise Refused("无法确认 Codex 进程状态，尚未修改配置。") from None
    if rows is None:
        return []
    rows = rows if isinstance(rows, list) else [rows]
    if any(not isinstance(row, dict) or type(row.get("ProcessId")) is not int for row in rows):
        raise Refused("无法确认 Codex 进程状态，尚未修改配置。")
    return rows


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
            return [dict(row) for row in db.execute(
                "SELECT id,model_provider,model,reasoning_effort,archived FROM threads "
                "WHERE model_provider IN ('openai','nearroute-http')")]
        finally:
            db.close()
    except sqlite3.Error:
        raise Refused("Codex 会话数据库暂不可读或格式不受支持，请稍后重试。") from None


def _eligible(rows, provider):
    return [row for row in rows if not row["archived"] and row["model_provider"] == provider
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
                 capability_check=None, startup_check=None):
        self.root = Path(root) if root is not None else ROOT
        self.home = (Path(home) if home is not None else Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")).expanduser().resolve()
        self.engine = Path(engine).resolve() if engine is not None else None
        self.state = self.root / "state" / "codex-transport"
        self.config = self.home / "config.toml"
        self.process_check = process_check or process_inventory
        self.migrate = migrate
        self.capability_check = capability_check or validate_capabilities
        self.startup_check = startup_check or validate_startup

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
                      "can_request": False, "can_restore": False, "owned": False, "compatible_http": False}
            try:
                pending = self._load("pending")
                result["pending"] = pending.get("mode") if pending else None
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
                        result.update(status="pending", explanation="已保存切换请求；请正常退出 Codex，NearRoute 将自动应用。" if running else "已保存切换请求，正在等待后台应用。")
                elif compatible:
                    result["explanation"] = "当前已使用 HTTP/SSE。" if selected == PROVIDER_ID else "已检测到现有 HTTP/SSE 配置，尚未改动。"
                else:
                    result["explanation"] = "当前使用 Codex 原有传输设置。"
                if not result["engine_available"]:
                    result["explanation"] = "未找到 Codex 桌面引擎，请安装或启动 Codex 后刷新。"
            except (Refused, OSError) as exc:
                result.update(ok=False, status="unsupported", supported=False,
                              can_request=False, can_restore=False,
                              explanation=str(exc) if isinstance(exc, Refused) else "无法读取传输设置，请检查文件访问权限。")
            return result

    def request_mode(self, mode):
        with _LOCK:
            if mode not in ("http", "original"):
                raise ValueError("mode must be 'http' or 'original'")
            status = self.inspect()
            allowed = status["can_request"] if mode == "http" else status["can_restore"]
            if not allowed:
                return dict(status, ok=False, explanation=status["explanation"] or "没有可恢复的 NearRoute 管理配置。")
            self._save("pending", {"mode": mode, "home": str(self.home), "requested_at": _now()})
            self._journal("requested", mode=mode)
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
                return dict(self.inspect(), status="applied", explanation="已应用 HTTP/SSE；可重新打开 Codex。" if pending["mode"] == "http" else "已恢复原传输设置；可重新打开 Codex。")
            except Exception as exc:
                reason = str(exc) if isinstance(exc, Refused) else "切换未完成，备份和切换记录已保留，请检查后重试。"
                pending["error"] = reason
                self._save("pending", pending)
                self._journal("stopped", mode=pending["mode"], reason=reason, error_type=type(exc).__name__)
                return dict(self.inspect(), ok=False, status="error", explanation=reason)
            finally:
                _release_home_lock(home_lock)

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


def request_mode(mode, **kwargs):
    return TransportManager(**kwargs).request_mode(mode)


def process_pending(**kwargs):
    return TransportManager(**kwargs).process_pending()


def cancel_pending(**kwargs):
    return TransportManager(**kwargs).cancel_pending()
