"""Move selected idle Codex chats using the metadata-only resume RPC.

Adapted from the reviewed private migration helper. The installed-engine proof is
state/network-repair-20261103/transport-persistence-fixture/result.json.
Only app-server writes chat state. No model turns or new threads are requested;
authentication files and token environment variables are never inspected.
"""

from contextlib import closing, contextmanager
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid

try:
    import tomllib
except ImportError:  # Python 3.10 release runtime; bundled official Tomli.
    import toml_compat as tomllib


PROVIDER = "nearroute-http"
TAIL_BYTES = 3 * 1024 * 1024
CHUNK = 1024 * 1024
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WINDOWS = os.name == "nt"
SAVED_KEYS = ("cwd", "approval_policy", "approvals_reviewer", "permission_profile",
              "active_permission_profile", "sandbox_policy", "runtime_workspace_roots",
              "disabled_plugin_ids")
RESPONSE_KEYS = {"cwd": "cwd", "approval_policy": "approvalPolicy",
                 "approvals_reviewer": "approvalsReviewer",
                 "active_permission_profile": "activePermissionProfile",
                 "runtime_workspace_roots": "runtimeWorkspaceRoots",
                 "disabled_plugin_ids": "disabledPluginIds"}


class Refused(Exception):
    pass


def process_inventory():
    command = ("Get-CimInstance Win32_Process | "
               "Select-Object ProcessId,ParentProcessId,Name | ConvertTo-Json -Compress")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, timeout=20, creationflags=FLAGS)
    if result.returncode:
        raise Refused("Cannot verify running Windows processes; no migration is permitted.")
    rows = json.loads(result.stdout.decode("utf-8-sig"))
    return rows if isinstance(rows, list) else ([rows] if rows else [])


def assert_no_user_codex(owned_pid=None):
    rows = process_inventory()
    owned = {owned_pid} if owned_pid is not None else set()
    while True:
        children = {row["ProcessId"] for row in rows if row["ParentProcessId"] in owned}
        if children <= owned:
            break
        owned.update(children)
    running = [row["ProcessId"] for row in rows
               if str(row.get("Name", "")).lower() == "codex.exe" and row["ProcessId"] not in owned]
    if running:
        raise Refused("Codex UI/engine is running. Exit Codex normally before applying the switch.")


@contextmanager
def read_database(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


def thread_row(db, thread_id):
    row = db.execute("SELECT id,rollout_path,model_provider,model,reasoning_effort,archived "
                     "FROM threads WHERE id=?", (thread_id,)).fetchone()
    if row is None:
        raise Refused("An explicitly selected thread is absent from the current state database.")
    return dict(row)


def tail_settings(path, since=None):
    """Read bounded metadata only; whole large histories are not parsed."""
    with path.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        boundary = since if since is not None else 0
        start = max(boundary, size - TAIL_BYTES)
        stream.seek(start)
        data = stream.read()
    if data and not data.endswith(b"\n"):
        raise Refused("A target rollout ends with an unfinished record; leave it unchanged.")
    if start > boundary:
        data = data.partition(b"\n")[2]
    found = {}
    for line in data.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        payload = item.get("payload", {})
        if item.get("type") == "turn_context":
            if "model" in payload:
                found["model"] = payload["model"]
            if "effort" in payload:
                found["effort"] = payload["effort"]
            for key in SAVED_KEYS:
                if key in payload:
                    found[key] = payload[key]
        elif item.get("type") == "event_msg" and payload.get("type") == "thread_settings_applied":
            settings = payload.get("thread_settings", {})
            if "model" in settings:
                found["model"] = settings["model"]
            # These Option fields are omitted when the saved value is None.
            found["effort"] = settings.get("reasoning_effort")
            found["service_tier"] = settings.get("service_tier")
            if "model_provider_id" in settings:
                found["model_provider_id"] = settings["model_provider_id"]
            for key in SAVED_KEYS:
                if key in settings:
                    found[key] = settings[key]
    return found


def comparison_path(path):
    """Normalize Windows extended file paths only for containment/equality checks."""
    if not isinstance(path, PureWindowsPath):
        return path
    value = str(path)
    if value.casefold().startswith("\\\\?\\unc\\"):
        value = "\\\\" + value[8:]
    elif re.match(r"^\\\\\?\\[A-Za-z]:\\", value):
        value = value[4:]
    elif value.startswith(("\\\\?\\", "\\\\.\\", "\\??\\", "\\\\??\\")):
        raise Refused("Device namespace rollout paths are not supported.")
    return PureWindowsPath(value)


def inspect_targets(home, config, thread_ids, provider_id, original):
    sqlite_home = Path(os.environ.get("CODEX_SQLITE_HOME") or config.get("sqlite_home") or home)
    if not sqlite_home.is_absolute():
        raise Refused("The state database directory is not absolute; review it explicitly.")
    candidates = [(int(match[1]), path) for path in sqlite_home.glob("state_*.sqlite")
                  if (match := re.fullmatch(r"state_(\d+)\.sqlite", path.name))]
    if not candidates:
        raise Refused("No current Codex state database was found.")
    database = max(candidates, key=lambda item: item[0])[1]
    targets = []
    with read_database(database) as db:
        database_bytes = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
        for thread_id in thread_ids:
            row = thread_row(db, thread_id)
            destination = provider_id if provider_id is not None else original.get(thread_id)
            if not isinstance(destination, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", destination):
                raise Refused("Every restored chat requires its recorded original provider ID.")
            if destination not in (PROVIDER, "openai", original.get(thread_id)):
                raise Refused("Only the HTTP provider or a recorded original provider can be selected.")
            if row["archived"]:
                raise Refused("An explicitly selected thread is archived; it will not be resumed automatically.")
            if row["model_provider"] not in ("openai", PROVIDER, original.get(thread_id)):
                raise Refused("A selected thread uses another custom provider; no migration is permitted.")
            if not isinstance(row["model"], str) or not row["model"].strip():
                raise Refused("A selected thread has no saved exact model; no global default will be guessed.")
            if not isinstance(row["reasoning_effort"], str) or not row["reasoning_effort"].strip():
                raise Refused("A selected thread has no saved exact reasoning effort; no default will be guessed.")
            path = Path(row["rollout_path"])
            comparison_path(path)  # Reject device namespaces before filesystem I/O.
            path = path.resolve(strict=True)
            if thread_id not in path.name:
                raise Refused("A selected rollout filename does not match its thread ID.")
            try:
                comparison_path(path).relative_to(comparison_path(home))
            except ValueError:
                raise Refused("A selected rollout is outside this Codex home; review it before migration.") from None
            with path.open("rb") as stream:
                first = json.loads(stream.readline())
            if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != thread_id:
                raise Refused("A selected rollout has inconsistent original thread metadata.")
            saved = tail_settings(path)
            for key, expected in (("model", row["model"]), ("effort", row["reasoning_effort"])):
                if key in saved and saved[key] != expected:
                    raise Refused("Saved thread model/effort disagrees with recent rollout settings; review it first.")
            stat = path.stat()
            targets.append({"row": row, "path": path, "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns, "saved": saved, "provider": destination})
    check_config(config, [target["provider"] for target in targets])
    return database, database_bytes, targets


def check_config(config, provider_ids):
    if config.get("profile") is not None:
        raise Refused("A selected profile requires separate effective-configuration review.")
    if config.get("features", {}).get("enable_request_compression") is False:
        raise Refused("Request compression is disabled in existing config; this migration will not change it.")
    providers = config.get("model_providers", {})
    for provider_id in set(provider_ids):
        if provider_id == "openai":
            continue
        if not isinstance(providers.get(provider_id), dict):
            raise Refused("A selected target provider is not configured.")
    if PROVIDER not in provider_ids:
        return
    provider = config.get("model_providers", {}).get(PROVIDER, {})
    required = {"name": "OpenAI", "wire_api": "responses", "requires_openai_auth": True,
                "supports_websockets": False, "supports_standalone_web_search": True}
    if any(provider.get(k) != v for k, v in required.items()):
        raise Refused("Prepare and validate the exact OpenAI HTTP provider before migrating chats.")
    if provider.get("base_url", "https://chatgpt.com/backend-api/codex").rstrip("/") != "https://chatgpt.com/backend-api/codex":
        raise Refused("The HTTP provider does not use the expected ChatGPT Codex backend.")
    if any(provider.get(key) is not None for key in ("env_key", "experimental_bearer_token", "auth", "gateway_oauth")):
        raise Refused("The HTTP provider has a different authentication configuration; review it first.")


def create_backups(home, config_path, config_bytes, database, database_bytes, targets, report):
    required_bytes = database_bytes + len(config_bytes) + sum(target["size"] for target in targets) + 65536
    if shutil.disk_usage(home).free < required_bytes:
        raise Refused("Insufficient disk space for the database, config and selected-rollout backups.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / "nearroute-transport-backups" / (stamp + "-" + str(os.getpid()))
    backup.mkdir(parents=True, exist_ok=False)
    report["backup_directory"] = str(backup)
    (backup / "config.toml.before").write_bytes(config_bytes)
    with read_database(database) as source, closing(sqlite3.connect(backup / database.name)) as destination:
        source.backup(destination)
    with read_database(backup / database.name) as db:
        for target in targets:
            if thread_row(db, target["row"]["id"]) != target["row"]:
                raise Refused("Thread metadata changed during backup; backups are retained, no migration started.")
    manifest = {"database": database.name, "config": "config.toml.before", "targets": []}
    for target in targets:
        path, before = target["path"], target["row"]
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (target["size"], target["mtime_ns"]):
            raise Refused("A target rollout changed during backup; no migration started.")
        copied = 0
        lines = 0
        digest = hashlib.sha256()
        name = before["id"] + ".jsonl.before"
        with path.open("rb") as source, (backup / name).open("xb") as destination:
            for chunk in iter(lambda: source.read(CHUNK), b""):
                copied += len(chunk)
                lines += chunk.count(b"\n")
                digest.update(chunk)
                destination.write(chunk)
        after = path.stat()
        if copied != target["size"] or (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise Refused("A target rollout changed while copying; no migration started.")
        target["prefix_sha256"] = digest.hexdigest()
        target["baseline_records"] = lines
        manifest["targets"].append({"thread_id": before["id"], "provider": before["model_provider"], "backup": name,
                                    "original_path": str(path), "bytes": copied,
                                    "records": lines, "prefix_sha256": digest.hexdigest()})
    if config_path.read_bytes() != config_bytes:
        raise Refused("Config changed during backup; no migration started.")
    (backup / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=True, indent=2), encoding="utf-8")
    return backup


class OwnedJob:
    """Keep only this helper and its MCP children in a close-to-clean-up Windows job."""
    def __init__(self):
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active_limit", w.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise Refused("Cannot create an owned process-cleanup job; no helper will be started.")

    def assign(self, proc):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise Refused("Cannot contain the helper's child processes; migration did not start.")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Client:
    def __init__(self, engine, home, sqlite_home, report):
        self.proc = None
        self.reader = None
        self.job = OwnedJob()
        self.messages = queue.Queue()
        self.next_id = 1
        try:
            environment = os.environ.copy()
            environment.update(CODEX_HOME=str(home), CODEX_SQLITE_HOME=str(sqlite_home))
            self.proc = subprocess.Popen([str(engine), "app-server", "--stdio"], cwd=home, env=environment,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, creationflags=FLAGS)
            report["app_server_started"] = True
            self.job.assign(self.proc)
            self.reader = threading.Thread(target=self.read, daemon=True)
            self.reader.start()
            self.request("initialize", {"clientInfo": {"name": "nearroute_transport_migration",
                         "title": "NearRoute transport metadata migration", "version": "1"},
                         "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized", "params": {}})
        except Exception:
            report["cleanup"] = self.close()
            raise

    def read(self):
        for line in self.proc.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                self.messages.put({"invalid_output": True})
        self.messages.put({"eof": True})

    def send(self, message):
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 45
        while True:
            try:
                message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise Refused("A metadata RPC timed out; raw responses are withheld.") from None
            if message.get("id") == request_id:
                if "error" in message:
                    raise Refused("A metadata RPC was rejected; raw error details are withheld.")
                return message.get("result")
            if message.get("eof") or message.get("invalid_output"):
                raise Refused("The metadata helper stopped or returned invalid output.")
            if time.monotonic() >= deadline:
                raise Refused("A metadata RPC timed out; raw responses are withheld.")

    def close(self):
        forced = False
        if self.proc is not None:
            if self.proc.stdin and not self.proc.stdin.closed:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                forced = True
        self.job.close()  # Also releases any task-owned MCP children.
        if self.proc is not None:
            if self.proc.poll() is None:
                # Only this newly created helper can reach this fallback if job assignment failed.
                self.proc.terminate()
                self.proc.wait(timeout=5)
                forced = True
            if self.reader is not None:
                self.reader.join(timeout=3)
            if self.proc.stdout:
                self.proc.stdout.close()
        return {"helper_stopped": self.proc is None or self.proc.poll() is not None,
                "exit_code": self.proc.returncode if self.proc is not None else None,
                "reader_stopped": self.reader is None or not self.reader.is_alive(),
                "child_cleanup_job_closed": self.job.handle is None,
                "forced_owned_cleanup": forced}


def validate_prefix(target):
    digest = hashlib.sha256()
    remaining = target["size"]
    with target["path"].open("rb") as stream:
        while remaining:
            chunk = stream.read(min(CHUNK, remaining))
            if not chunk:
                raise Refused("A target rollout was truncated; backups are retained for review.")
            digest.update(chunk)
            remaining -= len(chunk)
    if digest.hexdigest() != target["prefix_sha256"]:
        raise Refused("A target's original rollout prefix changed; backups are retained for review.")


def request_service_tier(value):
    # Native SERVICE_TIER_DEFAULT_REQUEST_VALUE selects no tier explicitly,
    # suppressing model-catalog defaults. A null optional RPC parameter would
    # omit that override. See codex-rs/protocol/src/config_types.rs.
    return "default" if value is None else value


def same_metadata(key, before, after):
    if key == "service_tier":
        # Native app-server represents Standard speed as null in responses and
        # may persist it as "default". Other tier identifiers remain exact.
        return request_service_tier(before) == request_service_tier(after)
    if key == "cwd":
        return isinstance(after, str) and comparison_path(Path(before).resolve()) == comparison_path(Path(after).resolve())
    if key == "active_permission_profile" and isinstance(before, dict) and isinstance(after, dict):
        return before.get("id") == after.get("id") and before.get("extends") == after.get("extends")
    return before == after


def verify_response(target, response):
    row, saved = target["row"], target["saved"]
    if not isinstance(response, dict) or (response.get("thread", {}).get("id") != row["id"]
            or response.get("modelProvider") != target["provider"] or response.get("model") != row["model"]
            or response.get("reasoningEffort") != row["reasoning_effort"]):
        raise Refused("Resume did not retain the original chat/model/effort; review backups.")
    if "service_tier" in saved and not same_metadata("service_tier", saved["service_tier"], response.get("serviceTier")):
        raise Refused("Resume changed the saved service tier; review backups.")
    for key, response_key in RESPONSE_KEYS.items():
        if key in saved and not same_metadata(key, saved[key], response.get(response_key)):
            raise Refused("Resume changed saved working-directory or permission metadata; review backups.")


def verify_persisted(database, targets, report):
    with read_database(database) as db:
        for target, safe in zip(targets, report["targets"]):
            row = target["row"]
            after = thread_row(db, row["id"])
            if any(after[key] != expected for key, expected in (
                    ("model_provider", target["provider"]), ("model", row["model"]),
                    ("reasoning_effort", row["reasoning_effort"]), ("archived", row["archived"]))):
                raise Refused("Persisted provider/model/effort validation failed; review backups.")
            if comparison_path(Path(after["rollout_path"]).resolve()) != comparison_path(target["path"]):
                raise Refused("The original rollout path changed; review backups.")
            validate_prefix(target)
            saved, current = target["saved"], tail_settings(target["path"], since=target["size"])
            if (current.get("model_provider_id") != target["provider"]
                    or current.get("model") != row["model"] or current.get("effort") != row["reasoning_effort"]):
                raise Refused("Persisted rollout settings do not match the requested metadata; review backups.")
            for key in (*SAVED_KEYS, "service_tier"):
                # The modern applied-settings record stores permission_profile;
                # the legacy sandbox_policy is not emitted by this engine.
                if key == "sandbox_policy":
                    continue
                if key in saved and (key not in current or not same_metadata(key, saved[key], current[key])):
                    raise Refused("Persisted working-directory or permission metadata changed; review backups.")
            safe.update(state="persisted", original_prefix_intact=True, original_records=target["baseline_records"])


def _migrate(engine, home, thread_ids, provider_id, original, report):
    if not WINDOWS:
        raise Refused("This migration helper supports Windows only.")
    engine = Path(engine).expanduser()
    if not engine.is_absolute() or not engine.is_file() or engine.name.lower() != "codex.exe":
        raise Refused("The engine must be an existing absolute Codex executable.")
    home = Path(home).expanduser()
    if not home.is_absolute():
        raise Refused("The Codex home must be absolute.")
    home = home.resolve(strict=True)
    thread_ids = list(dict.fromkeys(str(uuid.UUID(value)) for value in thread_ids))
    if not thread_ids:
        report.update(ok=True, target_count=0)
        return
    assert_no_user_codex()
    config_path = home / "config.toml"
    config_bytes = config_path.read_bytes()
    config = tomllib.loads(config_bytes.decode("utf-8-sig"))
    database, database_bytes, targets = inspect_targets(home, config, thread_ids, provider_id, original)
    report.update(target_count=len(targets),
                  original={item["row"]["id"]: item["row"]["model_provider"] for item in targets},
                  targets=[{"thread_id": item["row"]["id"], "provider_before": item["row"]["model_provider"],
                            "provider_after": item["provider"],
                            "model": item["row"]["model"], "effort": item["row"]["reasoning_effort"],
                            "state": "inspected"} for item in targets])
    create_backups(home, config_path, config_bytes, database, database_bytes, targets, report)
    assert_no_user_codex()
    if config_path.read_bytes() != config_bytes:
        raise Refused("Config changed before helper startup; no migration started.")
    client = None
    try:
        client = Client(engine, home, database.parent, report)
        report["app_server_started"] = True
        for target, safe in zip(targets, report["targets"]):
            assert_no_user_codex(client.proc.pid)
            validate_prefix(target)
            row = target["row"]
            params = {"threadId": row["id"], "modelProvider": target["provider"], "model": row["model"],
                      "config": {"model_reasoning_effort": row["reasoning_effort"]}, "excludeTurns": True}
            if "service_tier" in target["saved"]:
                params["serviceTier"] = request_service_tier(target["saved"]["service_tier"])
            safe["state"] = "resume_requested"
            report["mutation_attempted"] = True
            if report["changed"] is not True:
                report["changed"] = None  # A lost RPC response can still follow a persisted change.
            response = client.request("thread/resume", params)
            verify_response(target, response)
            safe.update(state="resumed")
            report["changed"] = True
        assert_no_user_codex(client.proc.pid)
    finally:
        if client is not None:
            report["cleanup"] = client.close()
    if not all(report["cleanup"][key] for key in ("helper_stopped", "reader_stopped", "child_cleanup_job_closed")):
        raise Refused("Owned helper cleanup is incomplete; stop and review its recorded cleanup state.")
    if report["cleanup"]["exit_code"] != 0 or report["cleanup"]["forced_owned_cleanup"]:
        raise Refused("The owned helper required abnormal cleanup; backups remain intact for review.")
    verify_persisted(database, targets, report)
    if config_path.read_bytes() != config_bytes:
        raise Refused("Config changed during migration; preserve the external change and review backups.")
    assert_no_user_codex()
    report.update(ok=True, changed=True)


def migrate(engine, home, thread_ids, provider_id, *, original=None):
    """Resume selected existing IDs with another provider, without a model turn.

    Use ``provider_id='nearroute-http'`` for HTTP/SSE; use ``provider_id=None``
    and ``original={thread_id: original_provider_id}`` to restore providers with
    each chat's current saved model/effort. Reports retain partial progress on
    failure. ``backup_directory`` is private recovery state, never UI/log text.
    """
    report = {"ok": False, "changed": False, "mutation_attempted": False,
              "model_turns_requested": 0, "app_server_started": False, "targets": [], "original": {}}
    try:
        _migrate(engine, home, thread_ids, provider_id, original or {}, report)
    except Exception as exc:
        report.update(reason=str(exc) if isinstance(exc, Refused) else "Migration stopped; raw details withheld.",
                      error_type=type(exc).__name__)
        if report.get("backup_directory"):
            report["recovery"] = ("Backups remain intact. No automatic database rollback was attempted. "
                                  "Review completed chats before restoring.")
    return report
