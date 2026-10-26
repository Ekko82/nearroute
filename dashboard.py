"""Native Windows control panel for NearRoute."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import optimizer as core
import updater
import windows_app
from tray_icon import ICON_PATH, TrayIcon, request_restore
from windows_app import clear_window, configure_taskbar, configure_window


ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
CONFIG = ROOT / "config.json"
STATUS = STATE / "status.json"
OPTIMIZER = ROOT / "optimizer.py"
CHINA_TIME = timezone(timedelta(hours=8))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WINDOW_TITLE = "近舟 NearRoute"
STATUS_STALE_SECONDS = 120


def apply_theme(root):
    """Use Tk's bundled theme so colors and spacing agree across Windows versions."""
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    background, surface, ink, muted = "#f3f6fb", "#ffffff", "#17243b", "#6b7a90"
    style.configure(".", font=("Microsoft YaHei UI", 10), background=background, foreground=ink)
    style.configure("TFrame", background=background)
    style.configure("Surface.TFrame", background=surface)
    style.configure("Card.TFrame", background=surface, borderwidth=1, relief="solid",
                    bordercolor="#e0e7f1", lightcolor=surface, darkcolor=surface)
    style.configure("TLabel", background=background, foreground=ink)
    style.configure("Surface.TLabel", background=surface)
    style.configure("Muted.TLabel", foreground=muted)
    style.configure("RuntimeWarning.TLabel", background="#fff5df", foreground="#8b6508", padding=(10, 6))
    style.configure("SurfaceMuted.TLabel", background=surface, foreground=muted, font=("Microsoft YaHei UI", 9))
    style.configure("Title.TLabel", font=("Microsoft YaHei UI", 21, "bold"), foreground=ink)
    style.configure("Section.TLabel", background=surface, font=("Microsoft YaHei UI", 11, "bold"))
    style.configure("PageSection.TLabel", background=background, font=("Microsoft YaHei UI", 11, "bold"))
    style.configure("Node.TLabel", background=surface, font=("Microsoft YaHei UI", 16, "bold"))
    style.configure("Metric.TLabel", background=surface, font=("Microsoft YaHei UI", 18, "bold"))
    style.configure("Status.TLabel", background="#eaf6ef", foreground="#227446",
                    font=("Microsoft YaHei UI", 9, "bold"), padding=(10, 5))
    style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(13, 8),
                    background=surface, foreground="#34445f", borderwidth=1, relief="flat",
                    bordercolor="#dce4ef", lightcolor=surface, darkcolor=surface, focuscolor="#c4d5f5")
    style.map("TButton", background=[("disabled", "#f1f4f8"), ("pressed", "#e3eaf5"), ("active", "#edf2fa")],
              foreground=[("disabled", "#9aa6b6")], bordercolor=[("active", "#bccce3")])
    style.configure("Primary.TButton", background="#2563eb", foreground=surface,
                    bordercolor="#2563eb", lightcolor="#2563eb", darkcolor="#2563eb")
    style.map("Primary.TButton", background=[("disabled", "#e8edf5"), ("pressed", "#1d4ed8"), ("active", "#3474f5")],
              foreground=[("disabled", "#9aa6b6"), ("!disabled", surface)],
              bordercolor=[("disabled", "#e0e7f1"), ("!disabled", "#2563eb")])
    style.configure("Danger.TButton", foreground="#b04444")
    style.configure("Quiet.TButton", padding=(9, 5), background=background, bordercolor=background)
    style.configure("TEntry", fieldbackground=surface, padding=(9, 7), bordercolor="#dce4ef", lightcolor=surface, darkcolor=surface)
    style.configure("TCombobox", fieldbackground=surface, padding=(9, 6), bordercolor="#dce4ef", arrowsize=13)
    style.map("TCombobox", fieldbackground=[("disabled", "#f1f4f8"), ("readonly", surface)],
              selectbackground=[("readonly", surface)], selectforeground=[("readonly", ink)])
    for control in ("TCheckbutton", "TRadiobutton"):
        style.configure(control, background=background, padding=(0, 3))
        style.configure("Surface." + control, background=surface)
        style.map("Surface." + control, background=[("active", surface)])
    style.configure("Treeview", background=surface, fieldbackground=surface, foreground=ink,
                    font=("Microsoft YaHei UI", 9), rowheight=35, borderwidth=0, relief="flat")
    style.map("Treeview", background=[("selected", "#dceafe")], foreground=[("selected", "#174ea6")])
    style.configure("Treeview.Heading", background="#f5f8fc", foreground="#63738c",
                    font=("Microsoft YaHei UI", 9, "bold"), padding=(10, 9), borderwidth=0, relief="flat")
    style.map("Treeview.Heading", background=[("active", "#edf2f9")])
    style.configure("TScrollbar", background="#cfdae9", troughcolor="#f4f7fb", borderwidth=0,
                    arrowsize=11, arrowcolor="#6b7a90", relief="flat", bordercolor="#f4f7fb",
                    lightcolor="#f4f7fb", darkcolor="#f4f7fb", gripcount=0)
    style.configure("TNotebook", background=background, borderwidth=0, bordercolor="#e0e7f1",
                    lightcolor="#e0e7f1", darkcolor="#e0e7f1")
    style.configure("TNotebook.Tab", padding=(16, 9), background="#eaf0f8", borderwidth=0,
                    bordercolor="#e0e7f1", lightcolor="#e0e7f1", darkcolor="#e0e7f1")
    style.map("TNotebook.Tab", background=[("selected", surface)], foreground=[("selected", "#2563eb")])
    return style


class WindowInstance:
    """Keep one control window per installation; bring it forward on relaunch."""

    def __init__(self) -> None:
        self.handle = None
        self.kernel = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        name = "Local\\CodexNodeOptimizerWindow_" + hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:16]
        ctypes.set_last_error(0)
        self.handle = self.kernel.CreateMutexW(None, False, name)
        if not self.handle:
            raise OSError("无法创建控制窗口的单实例锁")
        if ctypes.get_last_error() == 183:
            if request_restore(str(ROOT)):
                self.close()
                return False
            user = ctypes.WinDLL("user32", use_last_error=True)
            user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            user.FindWindowW.restype = wintypes.HWND
            user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user.SetForegroundWindow.argtypes = [wintypes.HWND]
            window = user.FindWindowW(None, WINDOW_TITLE) or user.FindWindowW(None, "Codex 线路优选")
            if window:
                user.ShowWindow(window, 9)  # SW_RESTORE also unhides a minimized window.
                user.SetForegroundWindow(window)
            self.close()
            return False
        return True

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} 的内容格式不正确")
    return data


def atomic_write_config(data: dict) -> None:
    """Write a complete, already validated document without partial reads."""
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=CONFIG.parent,
            prefix=".config-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, CONFIG)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def update_enabled(enabled: bool) -> None:
    """Keep the pause switch usable even if another setting needs correction."""
    with core.config_update_lock(CONFIG):
        data = read_json(CONFIG) if CONFIG.exists() else {}
        data["enabled"] = enabled
        atomic_write_config(data)


def save_settings(fields: dict) -> dict:
    with core.config_update_lock(CONFIG):
        current = read_json(CONFIG) if CONFIG.exists() else {}
        candidate = current | fields
        validated = core.validate_config(candidate)
        complete = candidate | validated
        atomic_write_config(complete)
        return complete


def parse_statuses(value: str) -> list[int]:
    parts = [part for part in re.split(r"[,，\s]+", value.strip()) if part]
    if not parts or any(not re.fullmatch(r"[0-9]{3}", part) for part in parts):
        raise ValueError("预期状态码须为逗号分隔的三位整数，例如 405 或 200,204。")
    return list(dict.fromkeys(int(part) for part in parts))


def parse_regions(value: str) -> list[str]:
    parts = [part.upper() for part in re.split(r"[,，\s]+", value.strip()) if part]
    if not parts or any(not re.fullmatch(r"[A-Z]{2}|UNKNOWN", part) for part in parts):
        raise ValueError("地区请填写两字母代码，多个用逗号分隔；UNKNOWN 表示无法识别地区的节点。")
    return list(dict.fromkeys(parts))


def policy_summary(raw: dict) -> str:
    cfg = core.DEFAULTS | raw
    mode = cfg.get("scan_mode", "regions")
    if mode == "all":
        scope = "全部节点"
    elif mode == "nodes":
        scope = f"指定节点（{len(cfg.get('scan_nodes', []))} 个）"
    elif mode == "adaptive":
        scope = "自动邻近地区（JP、TW、SG、HK、PH 中现有地区）"
    else:
        scope = "地区 " + "、".join(cfg.get("scan_regions", []))
    preference = "日本优先" if cfg.get("prefer_japan", False) else "按线路评分择优"
    expected = ", ".join(str(value) for value in cfg.get("probe_expected_statuses", []))
    compatible = cfg.get("controller_api_mode") == "selector"
    check_label = "节点检查" if compatible else "健康检查"
    probe_note = "兼容模式仅读取客户端测速耗时，不核对 HTTP 状态码" if compatible else f"预期 HTTP {expected}"
    return (
        f"{preference} · 范围：{scope} · {check_label} {cfg.get('health_interval_seconds')} 秒 / 测速 {cfg.get('scan_interval_seconds')} 秒\n"
        f"测试：{cfg.get('probe_url', '')} · {probe_note}"
    )


def python_executable() -> str:
    # Use the active environment; never depend on a developer's installation path.
    current = Path(sys.executable) if sys.executable else None
    if current is not None:
        hidden = current.with_name("pythonw.exe")
        if os.name == "nt" and hidden.is_file():
            return str(hidden)
        if current.is_file():
            return str(current)
    for name in (("pythonw", "python", "python3") if os.name == "nt" else ("python3", "python")):
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError("未找到 Python 运行环境。请使用安装时的 Python 打开近舟。")


def start_daemon() -> subprocess.Popen:
    if not OPTIMIZER.is_file():
        raise FileNotFoundError("未找到 optimizer.py，请先完成安装。")
    token = uuid.uuid4().hex
    process = subprocess.Popen(
        [python_executable(), str(OPTIMIZER), "run", "--ready-token", token],
        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
        close_fds=True,
    )
    process.nearroute_ready_file = STATE / f"startup-{token}.ready"
    process.nearroute_started = False
    return process


def wait_for_helper_startup(processes, timeout: float = 10.0) -> bool:
    """Wait until every helper we launched owns the lock or has exited.

    Each launch acknowledges acquiring the mutex and clearing a stale stop
    request independently of the best-effort status snapshot.
    """
    pending = list(processes)
    deadline = time.monotonic() + timeout
    while pending:
        starting = []
        for process in pending:
            if process.nearroute_started:
                continue
            if process.poll() is not None or process.nearroute_ready_file.is_file():
                process.nearroute_ready_file.unlink(missing_ok=True)
                process.nearroute_started = True
            else:
                starting.append(process)
        pending = starting
        if not pending:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def status_is_stale(status: dict) -> bool:
    try:
        updated = datetime.fromisoformat(str(status.get("updated_at", "")).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=CHINA_TIME)
        return (datetime.now(CHINA_TIME) - updated).total_seconds() > STATUS_STALE_SECONDS
    except ValueError:
        return True


def runtime_version(value):
    try:
        updater.version_tuple(value)
        return value
    except (ValueError, updater.UpdateError):
        return None


def runtime_version_notice(status, *, installed_version, gui_version):
    installed = runtime_version(installed_version)
    if installed is None:
        return "安装版本无法读取，请在“版本与更新 → 诊断信息”中检查。"
    gui = runtime_version(gui_version)
    differences = []
    if gui != installed:
        differences.append(f"窗口仍运行 {gui}" if gui is not None else "窗口版本未知")
    phase = status.get("phase")
    if (status.get("pid") and isinstance(phase, str)
            and phase in {"monitoring", "paused", "waiting", "scanning", "testing", "measuring", "verifying", "error"}
            and not status_is_stale(status)):
        helper = runtime_version(status.get("runtime_version"))
        if helper != installed:
            differences.append(f"后台仍运行 {helper}" if helper is not None else "后台版本未知")
    if not differences:
        return ""
    return (f"已安装 {installed}；" + "；".join(differences)
            + "。请点击“退出程序”后重新打开；× 仅收起到托盘。")


def diagnostic_summary(root=None, *, event_times=None) -> str:
    """Build a shareable whitelist from local records without changing the installation."""
    root = ROOT if root is None else Path(root)

    def document(name):
        try:
            value = read_json(root / name)
            return ("ok", value) if isinstance(value, dict) else ("invalid", {})
        except FileNotFoundError:
            return "missing", {}
        except (ValueError, UnicodeError):
            return "invalid", {}
        except OSError:
            return "unreadable", {}

    def timestamp(value):
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=CHINA_TIME)
            return parsed.isoformat()
        except ValueError:
            return None

    error_times = []
    if isinstance(event_times, (list, tuple)):
        for value in event_times[:20]:
            at = timestamp(value)
            if at is not None:
                error_times.append(datetime.fromisoformat(at))

    version, version_file = None, "ok"
    try:
        version = updater.current_version(root=root)
    except FileNotFoundError:
        version_file = "missing"
    except (ValueError, UnicodeError, updater.UpdateError):
        version_file = "invalid"
    except OSError:
        version_file = "unreadable"
    config_file, raw_config = document("config.json")
    cfg = None
    if config_file == "ok":
        try:
            cfg = core.validate_config(raw_config)
        except (TypeError, ValueError):
            config_file = "invalid"
    config = {"file": config_file}
    if cfg is not None:
        keys = ("enabled", "health_interval_seconds", "scan_interval_seconds", "switch_cooldown_seconds",
                "manual_selection_grace_seconds", "concurrency", "scan_mode", "scan_regions")
        config.update({key: cfg[key] for key in keys})
        config["codex_log_configured"] = bool(cfg["codex_log_db"])

    status_file, record = document("state/status.json")
    recorded_at = timestamp(record.get("updated_at"))
    phases = {"starting", "monitoring", "paused", "waiting", "scanning", "testing", "measuring",
              "verifying", "stopping", "stopped", "error"}
    phase = record.get("phase") if isinstance(record.get("phase"), str) and record["phase"] in phases else None
    stale = status_is_stale({"updated_at": recorded_at}) if status_file == "ok" else None
    stored_context = record.get("controller_context")
    expected_context = list(core.state_context(cfg)) if cfg is not None else None
    context_matches = None
    if (expected_context is not None and isinstance(stored_context, list)
            and len(stored_context) == len(expected_context)
            and all(isinstance(value, str) for value in stored_context)):
        context_matches = stored_context == expected_context
    current = (status_file == "ok" and context_matches is True and stale is False
               and phase is not None and phase not in ("starting", "stopping", "stopped"))
    available = record.get("controller_available")
    health = record.get("health")
    errors = record.get("network_errors_recent")
    helper_version = runtime_version(record.get("runtime_version"))
    gui_version = runtime_version(core.RUNTIME_VERSION)
    status = {
        "file": status_file, "recorded_at": recorded_at, "phase": phase, "stale": stale,
        "context_matches": context_matches, "current": current,
        "controller_available": available if current and type(available) is bool else None,
        "health_ok": health.get("ok") if current and available is True and isinstance(health, dict) and type(health.get("ok")) is bool else None,
        "network_errors_recent": errors if current and type(errors) is int and errors >= 0 else None,
        "recorded_last_switch_at": timestamp(record.get("last_switch_at")),
        "transport_recovery_until": timestamp(record.get("transport_recovery_until")) if current else None,
        "helper_runtime_version": helper_version,
        "started_at": timestamp(record.get("started_at")),
    }
    event_types = {"started", "stopped", "manual_selection", "external_selection_change", "switched", "rollback",
                   "selection_changed_during_validation", "candidate_rejected", "scan_completed", "restore", "error"}
    event_files, events, near_events = {}, [], []
    near_events_truncated = False
    for name in ("events.jsonl", "events.previous.jsonl"):
        recent_needed = name == "events.jsonl" or len(events) < 8
        if not recent_needed and not error_times:
            event_files[name] = "not_needed"
            continue
        try:
            with (root / "state" / name).open("r", encoding="utf-8-sig", errors="replace") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(item, dict) or not isinstance(item.get("event"), str) or item["event"] not in event_types:
                        continue
                    at = timestamp(item.get("at"))
                    if at is not None:
                        event = {"type": item["event"], "time": at}
                        if recent_needed:
                            events.append(event)
                        moment = datetime.fromisoformat(at)
                        # Rotation can expose the same sanitized event in both files.
                        if (any(abs((moment - error_time).total_seconds()) <= 120 for error_time in error_times)
                                and event not in near_events):
                            near_events.append(event)
                            if len(near_events) > 256:
                                near_events.sort(key=lambda row: datetime.fromisoformat(row["time"]))
                                del near_events[:-256]
                                near_events_truncated = True
            event_files[name] = "ok"
        except FileNotFoundError:
            event_files[name] = "missing"
        except OSError:
            event_files[name] = "unreadable"
    events.sort(key=lambda item: datetime.fromisoformat(item["time"]))
    event_summary = {"files": event_files, "installation_recent_events": events[-8:]}
    if event_times is not None:
        near_events.sort(key=lambda item: datetime.fromisoformat(item["time"]))
        event_summary.update(near_error_events=near_events, near_error_events_truncated=near_events_truncated)
    if os.name == "nt":
        system_name = "Windows"
        system_version = ".".join(str(value) for value in sys.getwindowsversion()[:3])
    else:
        system_name, system_version = platform.system(), platform.version()
    return json.dumps({
        "generated_at": datetime.now(CHINA_TIME).isoformat(), "version": version,
        "installed_version": version, "version_file": version_file,
        "runtime": {
            "python": platform.python_version(), "os": system_name, "os_version": system_version,
            "gui_runtime_version": gui_version,
            "gui_matches_installed": gui_version == version if gui_version is not None and version is not None else None,
            "helper_matches_installed": helper_version == version if current and helper_version is not None and version is not None else None,
        },
        "config": config, "status": status,
        "events": event_summary,
    }, ensure_ascii=False, indent=2)


def display_time(value: object) -> str:
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is not None:
            moment = moment.astimezone(CHINA_TIME)
        return moment.strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "—"


def number(value: object) -> str:
    if value is None or isinstance(value, bool):
        return "—"
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


def percentage(value: object) -> str:
    if value is None:
        return "—"
    try:
        rate = float(value)
        return f"{rate * 100 if rate <= 1 else rate:.0f}%"
    except (TypeError, ValueError):
        return "—"


class SettingsDialog:
    """Edit probe settings and scope; controller access here is read-only."""

    def __init__(self, owner: "Dashboard", *, parent: ttk.Frame, load_nodes: bool = True, visible: bool = True):
        self.owner = owner
        self.raw = read_json(CONFIG) if CONFIG.exists() else {}
        cfg = core.DEFAULTS | self.raw
        self.selected_nodes = set(cfg.get("scan_nodes", []))
        self.available_nodes: list[str] = []
        self.node_names: list[str] = sorted(self.selected_nodes)
        self.retained_nodes = set(self.selected_nodes)
        self.nodes_ready = False
        self.loading = False
        self.rebuilding = False
        self.closed = False
        self.after_id = None
        self.results: queue.SimpleQueue[tuple[bool, object]] = queue.SimpleQueue()
        self.window = ttk.Frame(parent)
        self.window.pack(fill="both", expand=True)
        self.url = tk.StringVar(value=str(cfg.get("probe_url", "")))
        self.expected = tk.StringVar(value=", ".join(str(value) for value in cfg.get("probe_expected_statuses", [])))
        self.mode = tk.StringVar(value=cfg.get("scan_mode", "regions"))
        self.regions = tk.StringVar(value=", ".join(cfg.get("scan_regions", [])))
        self.prefer_japan = tk.BooleanVar(value=cfg.get("prefer_japan", True))
        self.node_message = tk.StringVar(value=f"已保留 {len(self.selected_nodes)} 个已选节点，等待读取客户端。")
        self.save_message = tk.StringVar(value="保存后重新测速；已暂停时保持暂停。")
        self._saved_fields = self._draft_values()

        form = ttk.Frame(self.window, padding=(16, 12))
        form.pack(fill="both", expand=True)
        form.columnconfigure(0, weight=1)
        form.rowconfigure(1, weight=1)
        header = ttk.Frame(form)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(header, text="测速与选点", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="设置测试目标与候选范围，让优选更贴近你的使用习惯。", style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

        tabs = ttk.Notebook(form)
        tabs.grid(row=1, column=0, sticky="nsew")
        probe = ttk.Frame(tabs, style="Surface.TFrame", padding=12)
        probe.columnconfigure(0, weight=1)
        ttk.Label(probe, text="测速目标", style="Section.TLabel", background="#ffffff").grid(row=0, column=0, sticky="w")
        ttk.Entry(probe, textvariable=self.url).grid(row=1, column=0, sticky="ew", pady=(10, 12))
        response = ttk.Frame(probe, style="Surface.TFrame")
        response.grid(row=2, column=0, sticky="ew")
        ttk.Label(response, text="预期 HTTP 状态码", style="Surface.TLabel").pack(side="left", padx=(0, 12))
        ttk.Entry(response, textvariable=self.expected, width=19).pack(side="left")
        ttk.Label(response, text="逗号分隔，例如 200, 204", style="Surface.TLabel", foreground="#64748b").pack(side="left", padx=(12, 0))
        help_label = ttk.Label(
            probe, text="状态码需要与地址匹配。常见连通性地址返回 200 / 204；Codex responses 的 GET 探测通常返回 405。修改地址后，请同步确认状态码。",
            wraplength=730, style="Surface.TLabel", foreground="#64748b",
        )
        help_label.grid(row=3, column=0, sticky="ew", pady=(10, 0))

        candidates = ttk.Frame(tabs, style="Surface.TFrame", padding=12)
        tabs.add(candidates, text="节点范围")
        tabs.add(probe, text="测试目标")
        candidates.columnconfigure(0, weight=1)
        candidates.rowconfigure(5, weight=1)
        scope = ttk.Frame(candidates, style="Surface.TFrame")
        scope.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(scope, text="候选节点", style="Section.TLabel", background="#ffffff").pack(side="left", padx=(0, 20))
        for value, label in (("adaptive", "自动邻近"), ("regions", "按地区"), ("nodes", "指定节点"), ("all", "全部节点")):
            ttk.Radiobutton(scope, text=label, style="Surface.TRadiobutton", variable=self.mode, value=value, command=self.update_mode).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(scope, text="日本优先", style="Surface.TCheckbutton", variable=self.prefer_japan).pack(side="right")
        region_row = ttk.Frame(candidates, style="Surface.TFrame")
        region_row.grid(row=2, column=0, sticky="ew")
        region_row.columnconfigure(1, weight=1)
        ttk.Label(region_row, text="地区代码", style="Surface.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.region_entry = ttk.Entry(region_row, textvariable=self.regions)
        self.region_entry.grid(row=0, column=1, sticky="ew")
        self.preset_button = ttk.Button(region_row, text="常用邻近地区", command=lambda: self.regions.set("JP, TW, SG, HK"))
        self.preset_button.grid(row=0, column=2, padx=(10, 0))
        region_help = ttk.Label(
            candidates, text="自动邻近按现有节点识别 JP、TW、SG、HK、PH，订阅变化后自动适应；没有邻近节点时不会扩大范围。按地区可手动填写两字母代码，UNKNOWN 为未知地区。",
            style="Surface.TLabel", foreground="#64748b", wraplength=730,
        )
        region_help.grid(row=3, column=0, sticky="ew", pady=(8, 12))

        node_frame = ttk.Frame(candidates, style="Surface.TFrame")
        node_frame.grid(row=5, column=0, sticky="nsew")
        node_frame.columnconfigure(0, weight=1)
        node_frame.rowconfigure(1, weight=1)
        node_actions = ttk.Frame(node_frame, style="Surface.TFrame")
        node_actions.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Label(node_actions, text="节点名单", style="Surface.TLabel").pack(side="left")
        self.clear_button = ttk.Button(node_actions, text="清空选择", command=self.clear_nodes)
        self.clear_button.pack(side="right")
        self.all_button = ttk.Button(node_actions, text="全选", command=self.select_all_nodes)
        self.all_button.pack(side="right", padx=(0, 6))
        self.refresh_button = ttk.Button(node_actions, text="刷新节点", command=self.refresh_nodes)
        self.refresh_button.pack(side="right", padx=(0, 6))
        self.node_list = tk.Listbox(
            node_frame, selectmode=tk.EXTENDED, exportselection=False,
            font=("Microsoft YaHei UI", 10), height=5,
            background="#ffffff", foreground="#1e293b", selectbackground="#dbeafe", selectforeground="#1e40af",
            relief="flat", borderwidth=0, highlightthickness=1, highlightbackground="#dbe3ee", activestyle="none",
        )
        self.node_list.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(node_frame, orient="vertical", command=self.node_list.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(node_frame, orient="horizontal", command=self.node_list.xview)
        horizontal.grid(row=2, column=0, sticky="ew")
        self.node_list.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        self.node_list.bind("<<ListboxSelect>>", self.selection_changed)
        node_status = ttk.Label(candidates, textvariable=self.node_message, style="Surface.TLabel", foreground="#64748b", wraplength=730)
        node_status.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        footer = ttk.Frame(form)
        footer.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(footer, textvariable=self.save_message, style="Muted.TLabel").pack(side="left")
        ttk.Button(footer, text="保存设置", style="Primary.TButton", command=self.save).pack(side="right")
        ttk.Button(footer, text="返回概览", style="Secondary.TButton", command=self.close).pack(side="right", padx=(0, 8))
        ttk.Button(footer, text="放弃修改", command=self.discard_changes).pack(side="right", padx=(0, 8))
        form.bind("<Configure>", lambda event: self.resize_text(event.width, (help_label, region_help, node_status)))
        self.render_nodes()
        self.update_mode()
        self.after_id = self.window.after(150, self.poll_results)
        if load_nodes:
            self.refresh_nodes()

    @staticmethod
    def resize_text(width: int, labels: tuple) -> None:
        for label in labels:
            label.configure(wraplength=max(400, width - 86))

    def update_mode(self) -> None:
        regions_active = self.mode.get() == "regions"
        self.region_entry.state(["!disabled"] if regions_active else ["disabled"])
        self.preset_button.state(["!disabled"] if regions_active else ["disabled"])
        nodes_active = self.mode.get() == "nodes" and bool(self.node_names) and not self.loading
        self.node_list.configure(state=tk.NORMAL if nodes_active else tk.DISABLED)
        for button in (self.all_button, self.clear_button):
            button.state(["!disabled"] if nodes_active else ["disabled"])
        self.refresh_button.state(["disabled"] if self.loading else ["!disabled"])

    def render_nodes(self) -> None:
        self.rebuilding = True
        try:
            # Keep unavailable rows visible even after an explicit deselection,
            # so the user can select them again without a successful refresh.
            unavailable = (set(self.node_names) | self.selected_nodes) - set(self.available_nodes)
            self.node_names = self.available_nodes + sorted(unavailable)
            self.retained_nodes = unavailable
            self.node_list.configure(state=tk.NORMAL)
            self.node_list.delete(0, tk.END)
            for index, name in enumerate(self.node_names):
                suffix = "  [暂不可用]" if name in self.retained_nodes else ""
                self.node_list.insert(tk.END, name + suffix)
                if name in self.selected_nodes:
                    self.node_list.selection_set(index)
                if name in self.retained_nodes:
                    self.node_list.itemconfigure(index, foreground="#757e8a")
        finally:
            self.rebuilding = False
            self.update_mode()

    def selection_changed(self, _event=None) -> None:
        if self.rebuilding or self.loading:
            return
        selected = {self.node_names[index] for index in self.node_list.curselection()}
        self.selected_nodes = selected
        self.show_node_count()

    def show_node_count(self) -> None:
        self.node_message.set(
            f"客户端 {len(self.available_nodes)} 个节点 · 已选 {len(self.selected_nodes)} 个"
            + (f" · {len(self.retained_nodes)} 个列表节点暂不可用，可手动选择或取消。" if self.retained_nodes else " · 按住 Ctrl / Shift 可多选。")
        )

    def select_all_nodes(self) -> None:
        if self.loading:
            return
        self.selected_nodes.update(self.node_names)
        self.render_nodes()
        self.show_node_count()

    def clear_nodes(self) -> None:
        if self.loading:
            return
        self.selected_nodes.clear()
        self.render_nodes()
        self.show_node_count()

    def apply_loaded_nodes(self, names: list[str]) -> None:
        self.available_nodes = list(dict.fromkeys(names))
        self.retained_nodes = self.selected_nodes - set(self.available_nodes)
        self.nodes_ready = True
        self.loading = False
        self.render_nodes()
        self.show_node_count()

    def refresh_nodes(self) -> None:
        if self.loading:
            return
        self.loading = True
        self.node_message.set("正在读取客户端节点；不会切换节点…")
        self.update_mode()

        def worker() -> None:
            try:
                latest = read_json(CONFIG) if CONFIG.exists() else {}
                cfg = core.validate_config(latest)
                _general, proxies, group = core.Controller(cfg).snapshot(cfg)
                names = core.flatten_candidates(proxies, group)
                self.results.put((True, names))
            except Exception as error:
                self.results.put((False, str(error)[:200]))

        threading.Thread(target=worker, name="read-proxy-nodes", daemon=True).start()

    def poll_results(self) -> None:
        if self.closed:
            return
        try:
            while True:
                success, result = self.results.get_nowait()
                self.loading = False
                if success:
                    self.apply_loaded_nodes(result)
                else:
                    self.available_nodes = []
                    self.retained_nodes = set(self.selected_nodes)
                    self.nodes_ready = False
                    self.render_nodes()
                    self.node_message.set(f"无法读取客户端；已保留 {len(self.selected_nodes)} 个已选节点，未清空名单。{result}")
        except queue.Empty:
            pass
        self.after_id = self.window.after(150, self.poll_results)

    def fields(self) -> dict:
        mode = self.mode.get()
        regions = parse_regions(self.regions.get()) if self.regions.get().strip() or mode == "regions" else []
        return {
            "probe_url": self.url.get().strip(),
            "probe_expected_statuses": parse_statuses(self.expected.get()),
            "scan_mode": mode,
            "scan_regions": regions,
            "scan_nodes": [name for name in self.node_names if name in self.selected_nodes],
            "prefer_japan": self.prefer_japan.get(),
        }

    def _draft_values(self) -> tuple:
        return (self.url.get(), self.expected.get(), self.mode.get(), self.regions.get(),
                self.prefer_japan.get(), frozenset(self.selected_nodes))

    def has_unsaved_changes(self) -> bool:
        return self._draft_values() != self._saved_fields

    def save(self) -> None:
        if self.closed or any(getattr(self.owner, name, False) for name in
                              ("exiting", "restore_busy", "manual_busy", "client_busy")):
            self.save_message.set("当前操作尚未完成，请稍后保存设置。")
            return
        try:
            saved = save_settings(self.fields())
        except (OSError, ValueError) as error:
            messagebox.showerror("设置未保存", str(error), parent=self.window)
            return
        self.owner.settings_saved(saved)
        self._saved_fields = self._draft_values()
        self.save_message.set("设置已保存；可继续修改。")

    def close(self) -> None:
        if self.closed:
            return
        self.owner.show_tab("overview")

    def discard_changes(self) -> None:
        if self.closed:
            return
        self.dispose()
        self.owner.show_tab("overview")

    def dispose(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.after_id is not None:
            self.window.after_cancel(self.after_id)
            self.after_id = None
        self.window.destroy()
        if getattr(self.owner, "settings_dialog", None) is self:
            self.owner.settings_dialog = None


class ClientDialog:
    """Connect a local Mihomo/Clash API without changing proxy selections."""

    def __init__(self, owner: "Dashboard", *, parent: ttk.Frame, visible: bool = True):
        self.owner = owner
        cfg = core.DEFAULTS | (read_json(CONFIG) if CONFIG.exists() else {})
        self.original_context = core.state_context(cfg)
        self.closed = self.loading = self.applying = self.importing = False
        self.verified_identity = None
        self.selector_groups = []
        self.discovered_clients = {}
        self.discovery_identity = None
        self.results = queue.SimpleQueue()
        self.after_id = None
        self.window = ttk.Frame(parent)
        self.window.pack(fill="both", expand=True)
        self.controller = tk.StringVar(value=cfg["controller"])
        self.secret_env = tk.StringVar(value=cfg["controller_secret_env"])
        self.group = tk.StringVar(value=cfg["group"])
        self.auto_detect = tk.BooleanVar(value=cfg.get("auto_detect_client", True))
        self.auto_group = tk.BooleanVar(value=cfg.get("auto_select_group", True))
        self.compatible = tk.BooleanVar(value=cfg.get("controller_api_mode", "standard") == "selector")
        self._saved_fields = self._draft_values()
        self.message = tk.StringVar(value="自动发现本机客户端，或填写控制地址后测试连接；检查过程不会切换节点。")
        form = ttk.Frame(self.window, padding=(16, 12))
        form.pack(fill="both", expand=True)
        form.columnconfigure(0, weight=1)
        form.rowconfigure(2, weight=1)
        header = ttk.Frame(form)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="连接代理客户端", style="Title.TLabel").pack(anchor="w")
        help_text = ("支持 Clash Verge Rev、Clash Party（原 Mihomo Party）等本机 Mihomo/Clash 客户端。"
                     "猫猫云 5.6 可使用下方代理组兼容模式。")
        help_label = ttk.Label(header, text=help_text, wraplength=690, style="Muted.TLabel")
        help_label.pack(anchor="w", fill="x", pady=(6, 0))

        connection = ttk.Frame(form, style="Card.TFrame", padding=10)
        connection.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="控制地址", style="Surface.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 14))
        self.address_entry = ttk.Combobox(connection, textvariable=self.controller)
        self.address_entry.grid(row=0, column=1, sticky="ew")
        self.address_entry.bind("<<ComboboxSelected>>", self.select_discovered_client)
        self.discover_button = ttk.Button(connection, text="自动发现", command=self.discover_connections)
        self.discover_button.grid(row=0, column=2, padx=(10, 0))
        address_help = ttk.Label(connection, text="自动发现保留当前有效地址；多个客户端时从下拉列表选择，也可手动填写。", style="Surface.TLabel", foreground="#64748b", wraplength=650)
        address_help.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 10))
        ttk.Label(connection, text="密钥环境变量", style="Surface.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 14))
        self.secret_entry = ttk.Entry(connection, textvariable=self.secret_env)
        self.secret_entry.grid(row=2, column=1, sticky="ew")
        self.auth_button = ttk.Button(connection, text="导入猫猫云认证", command=self.import_auth)
        self.auth_button.grid(row=2, column=2, padx=(10, 0))
        secret_help = ttk.Label(connection, text="填写变量名，无需认证时留空。猫猫云导入需管理员确认，客户端重启后需重新导入。", wraplength=650, style="Surface.TLabel", foreground="#64748b")
        secret_help.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.auto_detect_button = ttk.Checkbutton(connection, text="当前地址失效时自动发现本机客户端", variable=self.auto_detect, style="Surface.TCheckbutton")
        self.auto_detect_button.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.compatible_button = ttk.Checkbutton(connection, text="代理组兼容模式（猫猫云 5.6 等受限接口）", variable=self.compatible, style="Surface.TCheckbutton")
        self.compatible_button.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        compatible_help = ttk.Label(connection, text="仅按所选代理组优选；Codex 实际路径、HTTP 状态与流式连接未验证。", wraplength=650, style="Surface.TLabel", foreground="#64748b")
        compatible_help.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(4, 0))

        selection = ttk.Frame(form, style="Card.TFrame", padding=10)
        selection.grid(row=2, column=0, sticky="nsew")
        selection.columnconfigure(0, weight=1)
        ttk.Label(selection, text="选择代理组", style="Section.TLabel", background="#ffffff").grid(row=0, column=0, columnspan=2, sticky="w")
        self.groups = ttk.Combobox(selection, textvariable=self.group, state="readonly")
        self.groups.grid(row=1, column=0, sticky="ew", pady=(10, 12))
        self.inspect_button = ttk.Button(selection, text="测试连接 · 读取代理组", command=self.inspect_connection)
        self.inspect_button.grid(row=1, column=1, sticky="e", padx=(10, 0), pady=(10, 12))
        self.auto_group_button = ttk.Checkbutton(selection, text="所选代理组不可用时自动判断合适的代理组", variable=self.auto_group, style="Surface.TCheckbutton")
        self.auto_group_button.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 12))
        message_label = ttk.Label(selection, textvariable=self.message, wraplength=650, style="Surface.TLabel", foreground="#64748b")
        message_label.grid(row=3, column=0, columnspan=2, sticky="ew")
        actions = ttk.Frame(form)
        actions.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(actions, text="更换连接会停止后台并暂停自动优选。", style="Muted.TLabel").pack(side="left")
        self.save_button = ttk.Button(actions, text="保存连接", style="Primary.TButton", command=self.save)
        self.save_button.pack(side="right")
        self.cancel_button = ttk.Button(actions, text="返回概览", style="Secondary.TButton", command=self.close)
        self.cancel_button.pack(side="right", padx=8)
        self.discard_button = ttk.Button(actions, text="放弃修改", command=self.discard_changes)
        self.discard_button.pack(side="right")
        form.bind("<Configure>", lambda event: (
            help_label.configure(wraplength=max(400, event.width - 48)),
            [label.configure(wraplength=max(400, event.width - 82)) for label in (address_help, secret_help, compatible_help, message_label)],
        ))
        self._variable_traces = [
            (self.controller, self.controller.trace_add("write", self.invalidate_connection)),
            (self.secret_env, self.secret_env.trace_add("write", self.invalidate_connection)),
            (self.compatible, self.compatible.trace_add("write", self.invalidate_connection)),
            (self.group, self.group.trace_add("write", lambda *_: self.update_buttons())),
        ]
        self.update_buttons()
        self.after_id = self.window.after(150, self.poll_results)
        if visible:
            self.discover_connections()

    def fields(self) -> dict:
        return {"controller": self.controller.get().strip().rstrip("/"),
                "controller_secret_env": self.secret_env.get().strip(), "group": self.group.get(),
                "auto_detect_client": self.auto_detect.get(), "auto_select_group": self.auto_group.get(),
                "controller_api_mode": "selector" if self.compatible.get() else "standard"}

    def _draft_values(self) -> tuple:
        return (self.controller.get(), self.secret_env.get(), self.group.get(),
                self.auto_detect.get(), self.auto_group.get(), self.compatible.get())

    def has_unsaved_changes(self) -> bool:
        return self._draft_values() != self._saved_fields

    def identity(self):
        fields = self.fields()
        identity = fields["controller"], fields["controller_secret_env"]
        return (*identity, "selector") if fields["controller_api_mode"] == "selector" else identity

    def invalidate_connection(self, *_args) -> None:
        self.verified_identity = None
        self.selector_groups = []
        self.groups.configure(values=())
        self.message.set("连接参数已改变，请重新测试连接。")
        self.update_buttons()

    def update_buttons(self) -> None:
        busy = self.loading or self.applying
        self.inspect_button.state(["disabled"] if busy else ["!disabled"])
        self.discover_button.state(["disabled"] if busy else ["!disabled"])
        self.auth_button.state(["disabled"] if busy or os.name != "nt" else ["!disabled"])
        ready = not busy and self.verified_identity == self.identity() and self.group.get() in self.selector_groups
        self.save_button.state(["!disabled"] if ready else ["disabled"])
        for widget in (self.address_entry, self.secret_entry, self.auto_detect_button, self.auto_group_button,
                       self.compatible_button, self.cancel_button, self.discard_button):
            widget.state(["disabled"] if self.applying else ["!disabled"])
        self.groups.configure(state="disabled" if self.applying else "readonly")

    def discover_connections(self) -> None:
        if self.closed or self.loading or self.applying:
            return
        fields = self.fields()
        fields["group"] = fields["group"] or self.original_context[2] or core.DEFAULTS["group"]
        self.loading = True
        self.verified_identity = None
        self.message.set("正在发现本机控制接口；只读检查，不测速或切换节点…")
        self.update_buttons()
        self.start_worker("discover", self.identity(), lambda: core.discover_clients(fields))

    def select_discovered_client(self, _event=None) -> None:
        candidate = self.discovered_clients.get(self.fields()["controller"])
        if candidate is None or self.applying:
            return
        identity = self.discovery_identity
        expected_env = identity[1] if identity and candidate["controller"] == identity[0] else ""
        self.secret_env.set(expected_env)
        if candidate.get("verified"):
            self.compatible.set(candidate.get("controller_api_mode") == "selector")
            self.apply_inspection(candidate, self.identity())
        else:
            self.verified_identity = None
            if candidate.get("unsupported"):
                self.message.set(str(core.ConfigReadUnsupported()))
            else:
                self.message.set("此控制地址需要认证。请填写对应的密钥环境变量名，再点击“测试连接”。"
                                 if candidate.get("needs_auth") else "此控制地址尚未验证，请点击“测试连接”。")
        self.update_buttons()

    def apply_inspection(self, result, identity) -> None:
        self.verified_identity = identity
        self.selector_groups = result["groups"]
        self.groups.configure(values=self.selector_groups)
        if self.group.get() not in self.selector_groups:
            suggested = result.get("recommended_group") if self.auto_group.get() else None
            suggested = suggested or result.get("active_group")
            self.group.set(suggested if suggested in self.selector_groups else "")
        compatible = result.get("controller_api_mode") == "selector" or result.get("verification_scope") == "selector"
        note = ("代理组兼容模式：当前模式未知，仅控制所选代理组；未验证 Codex 实际出站路径或流式连接。" if compatible else
                "当前为全局模式，实际控制 GLOBAL。" if result["mode"] == "global" else
                "当前为直连模式，优选前请改为规则或全局模式。" if result["mode"] == "direct" else "规则模式使用所选代理组。")
        if result.get("group_reason"):
            note += " " + result["group_reason"]
        if "nearby_regions" in result:
            nearby = result["nearby_regions"]
            note += " 邻近地区：" + "、".join(nearby) + "。" if nearby else " 未识别到邻近地区，自动范围不会扩大。"
        connection = "连接成功" if compatible else f"连接成功，HTTP 代理端口 {result['proxy_port']}"
        self.message.set(f"{connection}，读取 {len(self.selector_groups)} 个代理组。{note}")

    def start_worker(self, operation, identity, work) -> None:
        def worker():
            try:
                self.results.put((operation, identity, True, work()))
            except (OSError, ValueError, RuntimeError) as error:
                self.results.put((operation, identity, False, str(error)))
        try:
            threading.Thread(target=worker, name="nearroute-client-" + operation, daemon=True).start()
        except RuntimeError as error:
            self.results.put((operation, identity, False, str(error)))

    def inspect_connection(self) -> None:
        if self.closed or self.loading or self.applying:
            return
        fields = self.fields()
        fields["group"] = fields["group"] or core.DEFAULTS["group"]
        self.loading = True
        self.verified_identity = None
        self.message.set("正在读取控制接口；只读检查，不测速或切换节点…")
        self.update_buttons()
        identity = self.identity()
        self.start_worker("inspect", identity, lambda: core.inspect_controller(fields))

    def import_auth(self) -> None:
        if (self.closed or self.loading or self.applying or self.owner.exiting
                or self.owner.restore_busy or self.owner.manual_busy):
            return
        controller = self.fields()["controller"]
        self.applying = self.importing = self.owner.client_busy = True
        self.verified_identity = None
        self.message.set("等待 Windows 管理员确认，读取所选猫猫云的本机控制认证；不修改客户端或节点…")
        self.update_buttons()
        self.start_worker("auth", self.identity(), lambda: windows_app.import_maomao_auth(controller))

    def save(self) -> None:
        if (self.closed or self.loading or self.applying or self.owner.exiting
                or self.owner.restore_busy or self.owner.manual_busy):
            return
        fields = self.fields()
        if self.verified_identity != self.identity() or fields["group"] not in self.selector_groups:
            self.message.set("请先测试当前连接，并选择读取到的代理组。")
            return
        self.applying = self.owner.client_busy = True
        self.owner.cancel_service_start()
        self.message.set("正在保存客户端设置…")
        self.update_buttons()
        helpers = list(self.owner.started_helpers)

        def apply_connection():
            if not wait_for_helper_startup(helpers):
                raise RuntimeError("后台仍在启动，请稍后重试保存连接。")
            return core.configure_client(fields, expected_context=self.original_context)

        self.start_worker("save", self.identity(), apply_connection)

    def poll_results(self) -> None:
        if self.closed:
            return
        try:
            while True:
                operation, identity, success, result = self.results.get_nowait()
                if operation == "auth":
                    self.applying = self.importing = self.owner.client_busy = False
                    if identity != self.identity():
                        self.message.set("连接参数已改变；本次认证未应用到当前草稿，请重新导入。")
                    elif success:
                        self.secret_env.set(result)
                        self.message.set("认证已导入。请测试连接并选择代理组，然后保存连接。")
                    else:
                        self.message.set("认证未导入：" + result)
                elif operation in ("inspect", "discover"):
                    self.loading = False
                    if identity != self.identity():
                        self.message.set("连接参数已改变，请重新测试连接。")
                    elif operation == "discover" and success:
                        self.discovery_identity = identity
                        self.discovered_clients = {candidate["controller"]: candidate for candidate in result["candidates"]}
                        self.address_entry.configure(values=list(self.discovered_clients))
                        candidate = self.discovered_clients.get(result.get("selected_controller"))
                        if candidate is not None and candidate.get("verified"):
                            self.controller.set(candidate["controller"])
                            self.select_discovered_client()
                        else:
                            self.message.set(result["message"])
                    elif success:
                        self.apply_inspection(result, identity)
                    else:
                        self.verified_identity = None
                        self.message.set("连接未通过：" + result)
                else:
                    self.applying = self.owner.client_busy = False
                    if success:
                        self.original_context = core.state_context(result)
                        self.owner.client_saved(result)
                        self._saved_fields = self._draft_values()
                        self.message.set("客户端连接已保存；可继续修改或返回概览。")
                    else:
                        self.message.set("连接未保存：" + result)
                self.update_buttons()
        except queue.Empty:
            pass
        self.after_id = self.window.after(150, self.poll_results)

    def close(self) -> None:
        if self.closed or self.applying:
            return
        self.owner.show_tab("overview")

    def discard_changes(self) -> None:
        if self.closed or self.applying:
            return
        self.dispose()
        self.owner.show_tab("overview")

    def dispose(self) -> None:
        if self.closed:
            return
        for variable, token in self._variable_traces:
            variable.trace_remove("write", token)
        self._variable_traces.clear()
        self.closed = True
        if self.after_id is not None:
            self.window.after_cancel(self.after_id)
            self.after_id = None
        self.window.destroy()
        if getattr(self.owner, "client_dialog", None) is self:
            self.owner.client_dialog = None


class DiagnosticDialog:
    def __init__(self, owner: Dashboard, *, parent: ttk.Frame):
        self.owner, self.closed = owner, False
        self.summary = diagnostic_summary()
        self.results = queue.SimpleQueue()
        self.after_id = None
        self.reading_codex = False
        self.window = ttk.Frame(parent)
        self.window.pack(fill="both", expand=True)
        form = ttk.Frame(self.window, padding=(20, 16))
        form.pack(fill="both", expand=True)
        form.columnconfigure(0, weight=1)
        form.rowconfigure(4, weight=1)
        ttk.Label(form, text="诊断信息", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        description = ttk.Label(form, text="查看本安装的运行摘要；仅手动复制，不会自动发送。", style="Muted.TLabel", wraplength=800)
        description.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        self.version_note = tk.StringVar(value=self.runtime_note(json.loads(self.summary)))
        runtime_label = ttk.Label(form, textvariable=self.version_note, style="Muted.TLabel", wraplength=680)
        runtime_label.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        opt_in = ttk.Frame(form)
        opt_in.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        opt_in.columnconfigure(0, weight=1)
        privacy_label = ttk.Label(opt_in, text="仅此次读取配置路径或本机 Codex 默认日志，预览近 2 小时脱敏时间线。\n仅含时间、错误类别、重试次数与传输方式，不含原始日志、提示词、令牌或路径。", style="Muted.TLabel", wraplength=650)
        privacy_label.grid(row=0, column=0, sticky="ew")
        self.codex_button = ttk.Button(opt_in, text="读取 Codex 断流摘要", command=self.read_codex_summary)
        self.codex_button.grid(row=0, column=1, sticky="ne", padx=(16, 0))
        self.codex_message = tk.StringVar(value="")
        codex_feedback = ttk.Label(opt_in, textvariable=self.codex_message, style="Muted.TLabel", wraplength=800)
        codex_feedback.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.text = tk.Text(form, wrap="word", height=8, font=("Consolas", 10), background="#ffffff", foreground="#334155", relief="flat", borderwidth=0, highlightthickness=1, highlightbackground="#e2e8f0", padx=16, pady=14, spacing3=3)
        self.text.grid(row=4, column=0, sticky="nsew")
        self.text.insert("1.0", self.summary)
        self.text.configure(state="disabled")
        scrollbar = ttk.Scrollbar(form, command=self.text.yview)
        scrollbar.grid(row=4, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scrollbar.set)
        actions = ttk.Frame(form)
        actions.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.copy_message = tk.StringVar(value="")
        ttk.Label(actions, textvariable=self.copy_message, style="Muted.TLabel").pack(side="left")
        ttk.Button(actions, text="复制摘要", style="Primary.TButton", command=self.copy_summary).pack(side="right")
        ttk.Button(actions, text="返回概览", command=self.close).pack(side="right", padx=(0, 10))
        def resize_description(event):
            if event.widget is self.window:
                for label in (description, runtime_label, codex_feedback):
                    label.configure(wraplength=max(320, event.width - 56))
                privacy_label.configure(wraplength=max(320, event.width - 260))

        self.window.bind("<Configure>", resize_description, add="+")

    @staticmethod
    def runtime_note(summary):
        runtime, status = summary["runtime"], summary["status"]
        installed = summary["installed_version"] or "未知"
        gui = runtime["gui_runtime_version"] or "未知"
        helper = status["helper_runtime_version"] or "未知"
        started = display_time(status["started_at"])
        text = f"安装文件 {installed} · 窗口 {gui} · 后台快照 {helper}\n后台启动：{started}"
        hints = []
        if runtime["gui_matches_installed"] is False:
            hints.append("窗口与安装文件版本不同")
        if runtime["helper_matches_installed"] is False:
            hints.append("后台与安装文件版本不同")
        elif runtime["helper_matches_installed"] is None and status["current"]:
            hints.append("当前后台版本未知")
        return text + ("\n" + "；".join(hints) + "。可从托盘选择“退出近舟”后重新打开。" if hints else "")

    def read_codex_summary(self):
        if self.closed or self.reading_codex:
            return
        self.reading_codex = True
        self.codex_button.state(["disabled"])
        self.codex_message.set("正在读取本地断流摘要…")
        results, config_path = self.results, ROOT / "config.json"

        def work():
            source = "default"
            try:
                try:
                    config = read_json(config_path)
                except FileNotFoundError:
                    config = {}
                configured = config.get("codex_log_db", "")
                configured = configured.strip() if isinstance(configured, str) else ""
                if configured:
                    source, log_path = "configured", configured
                else:
                    codex_home = os.environ.get("CODEX_HOME")
                    log_path = str((Path(codex_home) if codex_home else Path.home() / ".codex") / "logs_2.sqlite")
                result = core.codex_connection_summary(log_path)
            except Exception:
                # This preview boundary must never expose an exception's log contents or path.
                result = {"state": "unreadable", "window_seconds": 7200, "error_count": 0,
                          "errors": [], "transports": [], "truncated": False}
            result["source"] = source
            snapshot = diagnostic_summary(event_times=[error["at"] for error in result["errors"]])
            results.put((snapshot, result))

        try:
            threading.Thread(target=work, daemon=True).start()
        except RuntimeError:
            snapshot = diagnostic_summary(event_times=[])
            source = "configured" if json.loads(snapshot)["config"].get("codex_log_configured") else "default"
            results.put((snapshot, {"state": "unreadable", "window_seconds": 7200, "error_count": 0,
                                    "errors": [], "transports": [], "truncated": False, "source": source}))
        self.after_id = self.window.after(100, self.poll_codex_summary)

    def poll_codex_summary(self):
        self.after_id = None
        if self.closed:
            return
        try:
            snapshot, result = self.results.get_nowait()
        except queue.Empty:
            self.after_id = self.window.after(100, self.poll_codex_summary)
            return
        self.reading_codex = False
        self.codex_button.state(["!disabled"])
        summary = json.loads(snapshot)
        events = summary["events"].get("near_error_events", [])
        for error in result["errors"]:
            at = datetime.fromisoformat(error["at"])
            nearby = []
            for event in events:
                seconds = (at - datetime.fromisoformat(event["time"])).total_seconds()
                if abs(seconds) <= 120:
                    nearby.append({**event, "seconds_from_event": round(seconds, 3)})
            error["nearby_events"] = nearby
        summary["codex_connection"] = result
        self.summary = json.dumps(summary, ensure_ascii=False, indent=2)
        self.version_note.set(self.runtime_note(summary))
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", self.summary)
        self.text.configure(state="disabled")
        self.copy_message.set("")
        messages = {
            "ok": "已读取并刷新近舟状态。已对照错误前后的本地事件，不能据此判断原因或重试结果。",
            "not_configured": "未配置日志路径，未读取 Codex 日志。",
            "missing": "本机日志暂不存在。",
            "busy": "日志正忙，请稍后重试。",
            "unreadable": "无法读取日志摘要。",
            "unsupported": "当前日志格式暂不支持。",
        }
        message = messages[result["state"]]
        if result["truncated"]:
            message += " 摘要已截断；错误数仅为已读取范围内的观察数，不代表近 2 小时完整错误数或最终失败数。"
        if summary["events"].get("near_error_events_truncated"):
            message += " 近舟事件对照已截断。"
        self.codex_message.set(message)

    def copy_summary(self):
        self.copy_message.set("")
        try:
            self.window.clipboard_clear()
            self.window.clipboard_append(self.summary)
        except tk.TclError:
            messagebox.showerror("复制失败", "无法访问系统剪贴板，请稍后重试。", parent=self.window)
            return
        self.copy_message.set("已复制诊断摘要。")

    def close(self):
        if self.closed:
            return
        self.owner.show_tab("overview")

    def dispose(self):
        if self.closed:
            return
        self.closed = True
        if self.after_id is not None:
            self.window.after_cancel(self.after_id)
            self.after_id = None
        if getattr(self.owner, "diagnostic_dialog", None) is self:
            self.owner.diagnostic_dialog = None
        self.window.destroy()


class UpdateDialog:
    """Release notes and preferences; all network work belongs to the owner."""

    def __init__(self, owner: Dashboard, *, parent: ttk.Frame):
        self.owner = owner
        self.closed = False
        self.window = ttk.Frame(parent)
        self.window.pack(fill="both", expand=True)
        form = ttk.Frame(self.window, padding=(20, 16))
        form.pack(fill="both", expand=True)
        form.columnconfigure(0, weight=1)
        form.rowconfigure(2, weight=1)
        header = ttk.Frame(form)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="版本与更新", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.check_button = ttk.Button(header, text="立即检查", command=lambda: owner.check_updates(manual=True))
        self.check_button.grid(row=0, column=1, sticky="e", padx=(16, 0))
        self.summary = tk.StringVar()
        summary_label = ttk.Label(header, textvariable=self.summary, style="Muted.TLabel", wraplength=650)
        summary_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(form, text="更新说明", style="PageSection.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 8))
        notes_frame = ttk.Frame(form)
        notes_frame.grid(row=2, column=0, sticky="nsew")
        notes_frame.columnconfigure(0, weight=1)
        notes_frame.rowconfigure(0, weight=1)
        self.notes = tk.Text(notes_frame, wrap="word", height=8, font=("Microsoft YaHei UI", 10), background="#ffffff", foreground="#334155", relief="flat", borderwidth=0, highlightthickness=1, highlightbackground="#e2e8f0", padx=14, pady=12, spacing3=5)
        self.notes.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(notes_frame, command=self.notes.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.notes.configure(yscrollcommand=scrollbar.set)
        self.auto_check = tk.BooleanVar(value=owner.update_preferences["auto_check"])
        self.mode = tk.StringVar(value=owner.update_preferences["mode"])
        preferences = ttk.Frame(form, style="Card.TFrame", padding=12)
        preferences.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        preferences.columnconfigure(0, weight=1)
        ttk.Label(preferences, text="自动更新", style="Surface.TLabel", font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(preferences, text="每 6 小时检查新版本", style="Surface.TCheckbutton", variable=self.auto_check).grid(row=0, column=1, sticky="e", padx=(12, 0))
        modes = ttk.Frame(preferences, style="Surface.TFrame")
        modes.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        for label, value in (("提醒后由我安装", "notify"), ("自动下载，提醒安装", "download"), ("空闲时自动安装并重启", "install")):
            button = ttk.Radiobutton(modes, text=label, value=value, variable=self.mode, style="Surface.TRadiobutton")
            button.pack(side="left", padx=(0, 16), pady=3)
            if value != "notify" and not owner.update_can_install:
                button.state(["disabled"])
        update_help = ("支持在软件内更新，本地源码有改动时会保留并提示。" if owner.update_can_install
                       else "当前目录不能安全地在软件内更新，请使用官方安装目录。")
        help_label = ttk.Label(preferences, text=update_help, style="Surface.TLabel", foreground="#64748b", wraplength=620)
        help_label.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        actions = ttk.Frame(form)
        actions.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="诊断信息", command=self.open_diagnostics).pack(side="left")
        self.install_button = ttk.Button(actions, style="Primary.TButton", command=owner.request_update_install)
        self.install_button.pack(side="right")
        ttk.Button(actions, text="保存设置", command=self.save).pack(side="right", padx=(0, 8))
        ttk.Button(actions, text="放弃修改", command=self.discard_changes).pack(side="right", padx=(0, 8))
        ttk.Button(actions, text="返回概览", command=self.close).pack(side="right", padx=(0, 8))
        self.message = tk.StringVar(value="")
        ttk.Label(form, textvariable=self.message, style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=(6, 0))
        def resize_description(event):
            if event.widget is self.window:
                for label in (summary_label, help_label):
                    label.configure(wraplength=max(320, event.width - 88))

        self.window.bind("<Configure>", resize_description, add="+")
        self.refresh()

    def open_diagnostics(self):
        self.owner.open_diagnostics()

    def has_unsaved_changes(self) -> bool:
        return (self.auto_check.get() != self.owner.update_preferences["auto_check"]
                or self.mode.get() != self.owner.update_preferences["mode"])

    def refresh(self) -> None:
        owner = self.owner
        self.summary.set(owner.update_text.get())
        release = owner.update_release
        self.notes.configure(state="normal")
        self.notes.delete("1.0", tk.END)
        self.notes.insert("1.0", release.notes if release else "检查后将在这里显示新版本的更新说明。")
        self.notes.configure(state="disabled")
        busy = bool(owner.update_busy) or owner.exiting
        self.check_button.state(["disabled"] if busy else ["!disabled"])
        self.install_button.configure(text=("无法安全更新" if not owner.update_can_install else "安装并重启" if owner.update_prepared else "下载并安装"))
        self.install_button.state(["disabled"] if busy or release is None else ["!disabled"])

    def save(self) -> None:
        try:
            self.owner.update_preferences = updater.save_preferences(self.auto_check.get(), self.mode.get(), root=ROOT)
        except (updater.UpdateError, OSError, ValueError) as error:
            messagebox.showerror("未能保存更新设置", str(error), parent=self.window)
            return
        self.owner.update_next_check = 0.0
        self.owner.notify("更新设置已保存。", 10)
        self.message.set("更新设置已保存。")

    def close(self) -> None:
        if self.closed:
            return
        self.owner.show_tab("overview")

    def discard_changes(self) -> None:
        if self.closed:
            return
        self.dispose()
        self.owner.show_tab("overview")

    def dispose(self) -> None:
        if self.closed:
            return
        self.closed = True
        if getattr(self.owner, "update_dialog", None) is self:
            self.owner.update_dialog = None
        self.window.destroy()


class Dashboard:
    def __init__(self, root: tk.Tk, *, start_service: bool = True, enable_tray: bool = True, enable_updates: bool = True):
        self.root = root
        self.closed = False
        self.results: queue.SimpleQueue[tuple[bool, str]] = queue.SimpleQueue()
        self.notice_until = 0.0
        self.node_signature = ""
        self.restore_busy = False
        self.client_busy = False
        self.client_dialog = None
        self.node_context = None
        self.node_mode = None
        self.manual_busy = False
        self.manual_results: queue.SimpleQueue = queue.SimpleQueue()
        self.manual_request_id = None
        self.manual_request_name = ""
        self.manual_request_started = 0.0
        self.manual_attempt = None
        self.window_properties_configured = False
        self.exiting = False
        self.hidden_to_tray = False
        self.tray = None
        self.tray_actions: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.tray_after_id = None
        self.service_after_id = None
        self.started_helpers = []
        self.settings_dialog: SettingsDialog | None = None
        self.started_at = time.monotonic()
        self.after_id = None
        self.updates_enabled = enable_updates and start_service
        self.update_results: queue.SimpleQueue = queue.SimpleQueue()
        self.update_busy = None
        self.update_release = updater.cached_release(root=ROOT)
        self.update_prepared = None
        self.update_requested_install = False
        self.update_dialog = None
        self.diagnostic_dialog = None
        self.active_tab = "overview"
        self.update_next_check = 0.0
        self.update_can_install = updater.can_update(root=ROOT)
        self.update_preferences = updater.load_preferences(root=ROOT)
        update_result = updater.read_update_result(root=ROOT)
        failed_version = update_result.get("version") if update_result.get("success") is False else None
        self.update_auto_install_attempted = "v" + failed_version if isinstance(failed_version, str) else None

        root.title(WINDOW_TITLE)
        if os.name == "nt":
            try:
                root.iconbitmap(default=str(ICON_PATH))
            except tk.TclError:
                pass  # A missing icon must not prevent opening the dashboard.
        root.geometry("1120x780")
        root.minsize(920, 650)
        root.configure(background="#f3f6fb")
        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        apply_theme(root)

        self.tabs = ttk.Notebook(root, padding=0)
        self.tabs.pack(fill="both", expand=True, padx=8, pady=8)
        self.pages = {}
        for key, label in (("overview", "线路概览"), ("client", "客户端连接"),
                           ("settings", "测速设置"), ("updates", "版本与更新"), ("diagnostics", "诊断信息")):
            page = ttk.Frame(self.tabs)
            self.pages[key] = page
            self.tabs.add(page, text=label)
        self.tabs.bind("<<NotebookTabChanged>>", self.tab_changed)
        container = ttk.Frame(self.pages["overview"], padding=(16, 8))
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(3, weight=1)
        self.current = tk.StringVar(value="当前节点：等待代理客户端连接")
        self.health = tk.StringVar(value="控制接口：等待检测")
        self.timings = tk.StringVar(value="最近测速：—    下次测速：—    最近切换：—")
        for field in ("node_title", "node_meta", "connection_value", "node_count_value", "error_value",
                      "scan_time_value", "switch_time_value", "next_scan_value", "update_time_value"):
            setattr(self, field, tk.StringVar(value="—"))
        self.node_title.set("等待代理客户端连接")
        hero = ttk.Frame(container, style="Card.TFrame", padding=(18, 12))
        hero.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        hero.columnconfigure(0, weight=1)
        node = ttk.Frame(hero, style="Surface.TFrame")
        node.grid(row=0, column=0, sticky="nsew", padx=(0, 20))
        ttk.Label(node, text="近舟 NearRoute · 所选节点", style="SurfaceMuted.TLabel").grid(row=0, column=0, sticky="w")
        node.columnconfigure(0, weight=1)
        node_label = ttk.Label(node, textvariable=self.node_title, style="Node.TLabel", width=1, wraplength=600)
        node_label.grid(row=1, column=0, sticky="ew", pady=(2, 4))
        node_label.bind("<Configure>", lambda event: node_label.configure(wraplength=max(180, event.width)))
        meta_label = ttk.Label(node, textvariable=self.node_meta, style="SurfaceMuted.TLabel", width=1, wraplength=600)
        meta_label.grid(row=2, column=0, sticky="ew")
        meta_label.bind("<Configure>", lambda event: meta_label.configure(wraplength=max(180, event.width)))
        automatic = ttk.Frame(hero, style="Surface.TFrame")
        automatic.grid(row=0, column=1, sticky="ne")
        self.badge = tk.StringVar(value="正在读取状态")
        self.badge_label = ttk.Label(automatic, textvariable=self.badge, style="Status.TLabel")
        self.badge_label.pack(anchor="e", pady=(0, 13))
        controls = ttk.Frame(automatic, style="Surface.TFrame")
        controls.pack(anchor="e")
        self.enable_button = ttk.Button(controls, text="启用优选", command=self.enable)
        self.enable_button.pack(side="left", padx=(0, 8))
        self.pause_button = ttk.Button(controls, text="暂停优选", command=self.pause)
        self.pause_button.pack(side="left")

        metrics = ttk.Frame(container)
        metrics.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        for index, title, value, caption in (
                (0, "控制接口", self.connection_value, self.scan_time_value),
                (1, "可选节点", self.node_count_value, self.switch_time_value),
                (2, "近期网络错误", self.error_value, self.update_time_value)):
            metrics.columnconfigure(index, weight=1, uniform="metrics")
            card = ttk.Frame(metrics, style="Card.TFrame", padding=(16, 9))
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 6, 0 if index == 2 else 6))
            card.columnconfigure(0, weight=1)
            ttk.Label(card, text=title, style="SurfaceMuted.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Label(card, textvariable=value, style="Metric.TLabel").grid(row=0, column=1, sticky="e")
            ttk.Label(card, textvariable=caption, style="SurfaceMuted.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 0))

        table_card = ttk.Frame(container, style="Card.TFrame", padding=(16, 10))
        table_card.grid(row=3, column=0, sticky="nsew")
        table_card.columnconfigure(0, weight=1)
        table_card.rowconfigure(2, weight=1)
        table_heading = ttk.Frame(table_card, style="Surface.TFrame")
        table_heading.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        table_heading.columnconfigure(0, weight=1)
        ttk.Label(table_heading, text="节点列表", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(table_heading, textvariable=self.next_scan_value, style="SurfaceMuted.TLabel").grid(row=0, column=1, padx=12)
        self.restore_button = ttk.Button(table_heading, text="恢复原节点", command=self.restore)
        self.restore_button.grid(row=0, column=2, padx=(0, 8))
        self.scan_button = ttk.Button(table_heading, text="立即测速", command=self.scan, style="Primary.TButton")
        self.scan_button.grid(row=0, column=3)
        self.message = tk.StringVar(value="服务正在启动，请稍候…")
        message_label = ttk.Label(table_card, textvariable=self.message, style="SurfaceMuted.TLabel", width=1, wraplength=1000)
        message_label.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        message_label.bind("<Configure>", lambda event: message_label.configure(wraplength=max(200, event.width)))

        table_frame = ttk.Frame(table_card, style="Surface.TFrame")
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("name", "score", "median", "success", "jitter", "samples", "eligible")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse", height=7)
        headings = ("节点", "综合分 / ms", "中位延迟 / ms", "成功率", "波动 / ms", "样本", "状态 / 原因")
        widths = (245, 100, 110, 80, 90, 60, 200)
        for key, title, width in zip(columns, headings, widths):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=55, anchor="w" if key in ("name", "eligible") else "center", stretch=key in ("name", "eligible"))
        self.table.tag_configure("selected", background="#edf4ff", foreground="#1d55a6")
        self.table.tag_configure("unavailable", foreground="#91a0b5")
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.node_menu = tk.Menu(root, tearoff=False)
        self.node_menu.add_command(label="切换到此节点", command=self.switch_selected_node)
        self.table.bind("<Double-1>", self.switch_clicked_node)
        self.table.bind("<Return>", self.switch_selected_node)
        self.table.bind("<Button-3>", self.show_node_menu)
        table_footer = ttk.Frame(table_card, style="Surface.TFrame")
        table_footer.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        table_footer.columnconfigure(0, weight=1)
        ttk.Label(table_footer, text="HTTP 探测延迟 · 双击 / Enter / 右键切换节点 · 关闭窗口后在托盘运行", style="SurfaceMuted.TLabel").grid(row=0, column=0, sticky="w")

        footer = ttk.Frame(container)
        footer.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        footer.columnconfigure(0, weight=1)
        self.update_text = tk.StringVar(value=f"窗口版本 {core.RUNTIME_VERSION or '未知'}")
        if self.update_release is not None:
            self.update_text.set(f"发现新版本 {self.update_release.version}，可在“版本与更新”中查看。")
        self.update_label = ttk.Label(footer, textvariable=self.update_text, style="Muted.TLabel", wraplength=650, width=1)
        self.update_label.grid(row=0, column=0, sticky="ew")
        self.update_label.bind("<Configure>", lambda event: self.update_label.configure(wraplength=max(200, event.width)))
        self.policy = tk.StringVar(value="正在读取测速设置…")
        self.policy_button = ttk.Button(footer, text="测速策略", style="Quiet.TButton", command=self.toggle_policy)
        self.policy_button.grid(row=0, column=1, padx=(8, 0))
        self.update_button = ttk.Button(footer, text="版本与更新", command=self.open_updates, style="Quiet.TButton")
        self.update_button.grid(row=0, column=2, padx=(4, 0))
        self.exit_button = ttk.Button(footer, text="退出程序", command=self.request_exit, style="Quiet.TButton")
        self.exit_button.grid(row=0, column=3, padx=(4, 0))
        self.runtime_text = tk.StringVar()
        self.runtime_label = ttk.Label(footer, textvariable=self.runtime_text, style="RuntimeWarning.TLabel", width=1, wraplength=1000)
        self.runtime_label.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        self.runtime_label.grid_remove()
        self.runtime_label.bind("<Configure>", lambda event: self.runtime_label.configure(wraplength=max(200, event.width - 20)))
        self.policy_label = ttk.Label(footer, textvariable=self.policy, style="Muted.TLabel", wraplength=1000, width=1)
        self.policy_label.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        self.policy_label.grid_remove()
        self.policy_label.bind("<Configure>", lambda event: self.policy_label.configure(wraplength=max(200, event.width)))
        try:
            if start_service:
                self.service_after_id = root.after(100, self.ensure_service)
            if enable_tray and os.name == "nt":
                self.tray = TrayIcon(WINDOW_TITLE, str(ROOT), self.tray_actions.put)
                if not self.tray.start():
                    self.tray.stop()
                    self.tray = None
                    self.notify("系统托盘暂不可用，点击 × 将最小化到任务栏。", 15)
            self.poll_tray()
            if self.updates_enabled:
                if (self.update_can_install and self.update_release is not None
                        and self.update_release.tag == self.update_auto_install_attempted):
                    self.update_text.set(f"版本 {self.update_release.version} 上次安装未完成，请打开“版本与更新”手动重试。")
                elif update_result and self.update_release is None:
                    self.update_text.set(str(update_result.get("message") or "更新已完成。"))
            self.refresh()
        except Exception:
            self.close()
            raise

    def notify(self, text: str, duration: float = 8.0) -> None:
        self.message.set(text)
        self.notice_until = time.monotonic() + duration

    def open_updates(self) -> None:
        self.show_tab("updates")

    def open_diagnostics(self) -> None:
        self.show_tab("diagnostics")

    def show_tab(self, key) -> None:
        if self.closed or self.exiting:
            return
        self.ensure_page(key)
        self.active_tab = key
        self.tabs.select(self.pages[key])

    def tab_changed(self, _event=None) -> None:
        if self.closed or self.exiting:
            return
        selected = self.tabs.select()
        key = next((key for key, page in self.pages.items() if str(page) == selected), None)
        if key is not None:
            try:
                self.ensure_page(key)
                self.active_tab = key
            except (OSError, ValueError, updater.UpdateError) as error:
                self.show_tab("overview")
                self.notify(f"页面暂时无法打开：{error}", 15)

    def ensure_page(self, key) -> None:
        panels = {"client": ("client_dialog", ClientDialog), "settings": ("settings_dialog", SettingsDialog),
                  "updates": ("update_dialog", UpdateDialog), "diagnostics": ("diagnostic_dialog", DiagnosticDialog)}
        if key in panels:
            attribute, factory = panels[key]
            panel = getattr(self, attribute)
            if panel is None or panel.closed:
                setattr(self, attribute, factory(self, parent=self.pages[key]))

    def has_unsaved_page_changes(self) -> bool:
        return any(panel is not None and not panel.closed and panel.has_unsaved_changes()
                   for panel in (getattr(self, "client_dialog", None), getattr(self, "settings_dialog", None),
                                 getattr(self, "update_dialog", None)))

    def update_worker(self, operation: str, work) -> None:
        """Workers publish plain values; only refresh is allowed to touch Tk."""
        self.update_busy = operation

        def worker() -> None:
            try:
                self.update_results.put((operation, True, work()))
            except (updater.UpdateError, OSError, ValueError, subprocess.SubprocessError) as error:
                self.update_results.put((operation, False, str(error)))

        try:
            threading.Thread(target=worker, name=f"nearroute-update-{operation}", daemon=True).start()
        except RuntimeError as error:
            self.update_results.put((operation, False, str(error)))
        if self.update_dialog is not None:
            self.update_dialog.refresh()

    def check_updates(self, *, manual: bool = False) -> None:
        if self.closed or self.exiting or self.update_busy:
            return
        if not manual and not updater.check_due(root=ROOT):
            return
        self.update_text.set("正在检查新版本…")
        self.update_worker("check", lambda: updater.check_for_update(root=ROOT))

    def request_update_install(self) -> None:
        if self.closed or self.exiting or self.update_busy or self.update_release is None:
            return
        if not self.update_can_install:
            self.update_text.set("当前目录不能安全地在软件内更新，请使用官方安装目录。")
            if self.update_dialog is not None:
                self.update_dialog.refresh()
            return
        if self.restore_busy or self.manual_busy or getattr(self, "client_busy", False):
            self.notify("正在处理节点操作，请完成后再安装更新。", 15)
            return
        if self.has_unsaved_page_changes():
            self.update_text.set("有未保存的页面设置，请先保存或放弃修改，再安装更新。")
            if self.update_dialog is not None:
                self.update_dialog.refresh()
            return
        if not messagebox.askyesno("安装近舟更新", "安装更新会停止后台优选并重启近舟。保留现有设置和当前代理节点，是否继续？", parent=self.root):
            return
        self.update_requested_install = True
        if self.update_prepared is not None:
            self.begin_update_install()
        else:
            release = self.update_release
            self.update_text.set(f"正在下载并校验版本 {release.version}…")
            self.update_worker("prepare", lambda: updater.prepare_update(release, root=ROOT))

    def begin_update_install(self) -> None:
        if self.closed or self.exiting or self.update_busy or self.restore_busy or self.manual_busy or getattr(self, "client_busy", False):
            return
        if self.update_prepared is None or not self.update_can_install:
            return
        if self.has_unsaved_page_changes():
            self.update_requested_install = False
            self.update_text.set("更新已下载，请先保存或放弃未保存的页面设置，再安装更新。")
            if self.update_dialog is not None:
                self.update_dialog.refresh()
            return
        self.exiting = True  # Also blocks daemon starts and all conflicting node actions.
        self.cancel_service_start()
        self.update_text.set("正在准备安装，近舟即将重新启动…")
        prepared = self.update_prepared
        helpers = list(self.started_helpers)

        def handoff():
            if not wait_for_helper_startup(helpers):
                raise updater.UpdateError("后台仍在启动，请稍后重试安装。")
            updater.start_installer(prepared, parent_pid=os.getpid(), python_exe=python_executable(), root=ROOT)

        self.update_worker("install", handoff)

    def automatic_install_ready(self, status: dict) -> bool:
        if self.closed or self.exiting or self.restore_busy or self.manual_busy or self.update_busy or getattr(self, "client_busy", False):
            return False
        if ((getattr(self, "active_tab", "overview") != "overview" and not getattr(self, "hidden_to_tray", False))
                or self.has_unsaved_page_changes()):
            return False
        if status_is_stale(status) or status.get("phase") not in ("monitoring", "paused", "waiting", "stopped"):
            return False
        recovery = status.get("transport_recovery_until")
        if recovery:
            try:
                until = datetime.fromisoformat(str(recovery).replace("Z", "+00:00"))
                if until.tzinfo is None:
                    until = until.replace(tzinfo=CHINA_TIME)
                if until > datetime.now(CHINA_TIME):
                    return False
            except ValueError:
                return False
        return True

    def poll_updates(self, status: dict) -> None:
        if not getattr(self, "updates_enabled", False) or self.closed:
            return
        try:
            while True:
                operation, success, result = self.update_results.get_nowait()
                self.update_busy = None
                if not success:
                    if operation == "install":
                        self.exiting = False
                        self.update_prepared = None
                    self.update_requested_install = False
                    self.update_text.set(f"更新未完成：{result}")
                    self.update_next_check = time.monotonic() + 1800
                elif operation == "check":
                    previous = self.update_release
                    self.update_release = result
                    if result is None:
                        try:
                            version = updater.current_version(root=ROOT)
                            self.update_text.set(f"已安装最新版本 {version}")
                        except (updater.UpdateError, OSError, ValueError):
                            self.update_text.set("未发现可用更新；安装版本信息暂不可读取。")
                    else:
                        if previous is None or previous.tag != result.tag:
                            self.update_prepared = None
                        if self.update_prepared is None:
                            self.update_auto_prepare_attempted = None
                        action = "查看并安装" if self.update_can_install else "查看更新说明"
                        if self.update_can_install and self.update_auto_install_attempted == result.tag:
                            self.update_text.set(f"版本 {result.version} 上次安装未完成，请打开“版本与更新”手动重试。")
                        else:
                            self.update_text.set(f"发现新版本 {result.version}，点击“版本与更新”{action}。")
                        if (previous is None or previous.tag != result.tag) and self.tray is not None:
                            self.tray.show_notification("近舟发现新版本", f"{result.version} 已发布，打开近舟的“版本与更新”{action}。")
                elif operation == "prepare":
                    self.update_prepared = result
                    suffix = "等待节点操作结束后安装并重启。" if self.update_requested_install else "可安装并重启。"
                    self.update_text.set(f"新版本 {self.update_release.version} 已下载并校验，{suffix}")
                elif operation == "install":
                    self.close()
                    return
                if self.update_dialog is not None:
                    self.update_dialog.refresh()
        except queue.Empty:
            pass
        if self.exiting or self.update_busy:
            return
        now = time.monotonic()
        if self.update_preferences["auto_check"] and now >= self.update_next_check:
            self.update_next_check = now + 300
            try:
                self.check_updates()
            except (updater.UpdateError, OSError, ValueError) as error:
                self.update_text.set(f"更新检查未完成：{error}")
                self.update_next_check = now + 1800
            if self.update_busy:
                return
        mode = self.update_preferences["mode"]
        release = self.update_release
        if release is None or not self.update_can_install:
            return
        auto_prepare = (self.update_preferences["auto_check"] and mode in ("download", "install")
                        and (mode != "install" or self.update_auto_install_attempted != release.tag))
        if self.update_prepared is None and auto_prepare and getattr(self, "update_auto_prepare_attempted", None) != release.tag:
            self.update_auto_prepare_attempted = release.tag
            self.update_text.set(f"正在下载并校验版本 {release.version}…")
            self.update_worker("prepare", lambda: updater.prepare_update(release, root=ROOT))
        elif self.update_prepared is not None:
            auto_install = self.update_preferences["auto_check"] and mode == "install" and getattr(self, "update_auto_install_attempted", None) != release.tag
            if self.update_requested_install or (auto_install and self.automatic_install_ready(status)):
                self.update_auto_install_attempted = release.tag
                self.begin_update_install()

    def toggle_policy(self):
        if self.policy_label.winfo_ismapped():
            self.policy_label.grid_remove()
            self.policy_button.configure(text="测速策略")
        else:
            self.policy_label.grid()
            self.policy_button.configure(text="收起策略")

    def ensure_service(self) -> bool:
        if self.closed or self.exiting or self.restore_busy or getattr(self, "client_busy", False):
            return False
        try:
            process = start_daemon()
            self.started_helpers = [p for p in self.started_helpers if p.poll() is None]
            self.started_helpers.append(process)
            return True
        except (OSError, ValueError) as error:
            self.notify(f"服务启动失败：{error}", 30)
            return False

    def open_client(self) -> None:
        if self.exiting or self.restore_busy or self.manual_busy or self.client_busy:
            return
        try:
            self.show_tab("client")
        except (OSError, ValueError) as error:
            messagebox.showerror("无法打开客户端连接", str(error), parent=self.root)

    def client_saved(self, saved: dict) -> None:
        same_context = tuple(core.state_context(saved)) == self.node_context
        if not same_context:
            self.node_context = None
            self.node_mode = None
            self.manual_attempt = None
            self.manual_request_id = None
            self.node_signature = ""
            self.render_nodes("—", [])
            self.current.set("当前节点：等待客户端状态")
            self.health.set("控制接口：等待检测")
            self.node_title.set("等待客户端状态")
            self.node_meta.set("连接后显示当前线路")
            self.connection_value.set("等待检测")
            for field in ("node_count_value", "error_value", "scan_time_value", "switch_time_value",
                          "next_scan_value", "update_time_value"):
                getattr(self, field).set("—")
        self.policy.set(policy_summary(saved))
        message = ("客户端设置已保存，当前节点和优选开关保持。" if same_context else
                   "客户端连接已保存，自动优选保持暂停。可点击“立即测速”检查，再启用自动优选。"
                   if not saved["enabled"] else "客户端连接设置已保存，原有优选开关保持。")
        self.notify(message, 25)

    def open_settings(self) -> None:
        if self.restore_busy or self.exiting or getattr(self, "client_busy", False):
            return
        try:
            self.show_tab("settings")
        except (OSError, ValueError) as error:
            messagebox.showerror("无法打开设置", str(error), parent=self.root)

    def settings_saved(self, saved: dict) -> None:
        if self.closed or self.exiting or getattr(self, "client_busy", False):
            return
        self.policy.set(policy_summary(saved))
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            (STATE / "scan.request").touch()
            if self.ensure_service():
                suffix = "自动切换保持暂停。" if not saved.get("enabled", True) else ""
                self.notify("设置已保存，已请求重新测速。" + suffix, 15)
            else:
                self.notify("设置已保存，后台暂未启动；可点击“立即测速”重试。", 20)
        except OSError as error:
            self.notify(f"设置已保存，但未能请求测速：{error}", 20)

    def enable(self) -> None:
        if self.restore_busy or self.exiting or getattr(self, "client_busy", False):
            return
        try:
            update_enabled(True)
            if self.ensure_service():
                self.notify("已启用自动优选，服务将按计划检查线路。")
        except (OSError, ValueError) as error:
            messagebox.showerror("未能启用", str(error), parent=self.root)

    def pause(self) -> None:
        if self.restore_busy or self.exiting or getattr(self, "client_busy", False):
            return
        try:
            update_enabled(False)
            self.notify("已暂停自动切换，当前节点继续使用。")
        except (OSError, ValueError) as error:
            messagebox.showerror("未能暂停", str(error), parent=self.root)

    def scan(self) -> None:
        if self.restore_busy or self.exiting or getattr(self, "client_busy", False):
            return
        try:
            if not self.ensure_service():
                return
            STATE.mkdir(parents=True, exist_ok=True)
            (STATE / "scan.request").touch()
            self.notify("已请求完整测速，结果会自动刷新。", 15)
        except OSError as error:
            messagebox.showerror("未能开始测速", str(error), parent=self.root)

    def switch_selected_node(self, _event=None) -> str:
        selected = self.table.selection()
        if selected:
            self.switch_node(selected[0])
        return "break"

    def switch_clicked_node(self, event) -> str:
        name = self.table.identify_row(event.y)
        if name:
            self.table.selection_set(name)
            self.table.focus(name)
            self.switch_node(name)
        return "break"

    def show_node_menu(self, event) -> str:
        name = self.table.identify_row(event.y)
        if not name:
            return "break"
        self.table.selection_set(name)
        self.table.focus(name)
        self.table.focus_set()
        blocked = self.closed or self.exiting or self.restore_busy or self.manual_busy or getattr(self, "client_busy", False)
        self.node_menu.entryconfigure(0, state="disabled" if blocked else "normal")
        try:
            self.node_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.node_menu.grab_release()
        return "break"

    def switch_node(self, name: str) -> None:
        if self.closed or self.exiting or self.restore_busy or self.manual_busy or getattr(self, "client_busy", False):
            return
        context = self.node_context
        mode = self.node_mode
        if context is None or mode is None:
            self.notify("客户端状态尚未确认，请刷新节点后再切换。", 15)
            return
        self.manual_busy = True
        self.manual_request_name = name
        self.manual_request_started = time.monotonic()
        self.manual_request_id = None
        attempt = self.manual_attempt = {"request_id": None}
        if not self.ensure_service():
            self.manual_busy = False
            self.manual_attempt = None
            self.notify("后台未启动，未提交手动切换；可重试。", 20)
            return
        self.notify(f"正在切换到“{name}”…", 30)

        def worker() -> None:
            try:
                request_id = core.request_manual_selection(name, expected_context=context, expected_mode=mode)
                attempt["request_id"] = request_id
                if attempt is not self.manual_attempt or self.closed or self.exiting:
                    try:
                        core.cancel_manual_selection(request_id)
                    except OSError:
                        self.manual_results.put((attempt, True, request_id))
                    return
                self.manual_results.put((attempt, True, request_id))
            except (OSError, ValueError) as error:
                self.manual_results.put((attempt, False, str(error)))

        try:
            threading.Thread(target=worker, name="manual-node-request", daemon=True).start()
        except RuntimeError:
            self.manual_busy = False
            self.manual_attempt = None
            self.notify("无法启动手动切换请求，可重试。", 20)

    def poll_manual_selection(self, status: dict) -> None:
        try:
            while True:
                attempt, success, result = self.manual_results.get_nowait()
                if attempt is not self.manual_attempt:
                    if success:
                        try:
                            core.cancel_manual_selection(result)
                        except OSError:
                            self.notify("旧切换请求清理未完成，请留意当前节点。", 20)
                    continue
                if success:
                    self.manual_request_id = result
                else:
                    self.manual_busy = False
                    self.manual_attempt = None
                    self.notify(f"未能请求切换：{result}。可重试。", 20)
        except queue.Empty:
            pass
        result = status.get("manual_selection_result") or {}
        if self.manual_busy and self.manual_request_id and result.get("request_id") == self.manual_request_id:
            name = self.manual_request_name
            self.manual_busy = False
            self.manual_attempt = None
            self.manual_request_id = None
            if result.get("ok") and result.get("name") == name:
                self.notify(f"已手动切换到“{name}”。暂时尊重手动选择。", 20)
            elif result.get("ok"):
                self.notify("返回的节点与请求不一致，请查看当前节点后重试。", 20)
            else:
                self.notify(f"手动切换失败：{result.get('message') or '请检查客户端连接'}。可重试。", 20)
        if self.manual_busy and time.monotonic() - self.manual_request_started >= 30:
            attempt = self.manual_attempt
            self.manual_busy = False
            self.manual_attempt = None
            request_id = self.manual_request_id or (attempt["request_id"] if attempt is not None else None)
            self.manual_request_id = None
            try:
                cancelled = bool(request_id and core.cancel_manual_selection(request_id))
                description = "已取消未处理请求，可重试。" if cancelled else "可重试；已开始的切换可能仍会完成。"
            except OSError:
                description = "请求清理未完成，请查看当前节点后重试。"
            self.notify("手动切换等待超时，" + description, 20)

    def restore(self) -> None:
        if self.restore_busy or self.exiting or getattr(self, "manual_busy", False) or getattr(self, "client_busy", False):
            return
        try:
            update_enabled(False)
        except (OSError, ValueError) as error:
            messagebox.showerror("未能暂停自动切换", str(error), parent=self.root)
            return
        self.restore_busy = True
        self.cancel_service_start()
        self.restore_button.state(["disabled"])
        self.enable_button.state(["disabled"])
        self.pause_button.state(["disabled"])
        self.scan_button.state(["disabled"])
        self.exit_button.state(["disabled"])
        self.notify("正在恢复原节点，自动切换已暂停…", 90)

        def worker() -> None:
            try:
                if not wait_for_helper_startup(self.started_helpers):
                    self.results.put((False, "后台仍在启动，自动切换已暂停，请稍后重试恢复原节点。"))
                    return
                result = subprocess.run(
                    [python_executable(), str(OPTIMIZER), "restore"],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, creationflags=NO_WINDOW,
                    timeout=60, check=False,
                )
                if result.returncode == 0:
                    self.results.put((True, "恢复操作已完成，自动切换保持暂停。当前节点和详细结果请见服务状态。"))
                else:
                    self.results.put((False, "恢复未完成，自动切换已暂停。请检查代理客户端是否运行，并查看服务状态或日志。"))
            except (OSError, subprocess.TimeoutExpired):
                self.results.put((False, "恢复操作未完成，自动切换已暂停。请检查代理客户端连接后重试。"))

        threading.Thread(target=worker, name="restore-node", daemon=True).start()

    def refresh_runtime_notice(self, status) -> None:
        try:
            installed = updater.current_version(root=ROOT)
        except (updater.UpdateError, OSError, ValueError):
            installed = None
        text = "" if self.exiting else runtime_version_notice(
            status, installed_version=installed, gui_version=core.RUNTIME_VERSION)
        self.runtime_text.set(text)
        if text:
            self.runtime_label.grid()
        else:
            self.runtime_label.grid_remove()

    def refresh(self) -> None:
        if self.closed:
            return
        try:
            while True:
                _success, message = self.results.get_nowait()
                self.restore_busy = False
                self.restore_button.state(["!disabled"])
                self.scan_button.state(["!disabled"])
                self.notify(message, 15)
        except queue.Empty:
            pass
        try:
            status = read_json(STATUS)
        except (OSError, ValueError):
            status = {}
        try:
            config = read_json(CONFIG)
            enabled = bool(config.get("enabled", True))
            self.policy.set(policy_summary(config))
        except (OSError, ValueError):
            config = {}
            enabled = bool(status.get("enabled", True))
            self.policy.set(policy_summary({}))
        selector_only = config.get("controller_api_mode") == "selector"
        context = tuple(core.state_context(core.DEFAULTS | config))
        if status.get("controller_context") != list(context):
            status = {}
            self.node_context = None
            self.node_mode = None
        else:
            self.node_context = context
            mode, group = status.get("mode"), status.get("group")
            self.node_mode = (core.active_mode(status, group)
                              if status.get("phase") != "starting"
                              and status.get("controller_available") is True
                              and isinstance(mode, str)
                              and (mode.lower() in ("rule", "global") or
                                   (mode.lower() == "unknown" and selector_only
                                    and (status.get("controller_api_mode") == "selector"
                                         or status.get("verification_scope") == "selector")))
                              and isinstance(group, str) and group else None)
        self.poll_updates(status)
        if self.closed:
            return
        self.refresh_runtime_notice(status)
        self.poll_manual_selection(status)
        stale = bool(status) and status_is_stale(status)
        phase = str(status.get("phase", "starting"))
        starting = phase == "starting"
        stopped = phase in ("stopped", "stopping")
        connected = bool(status.get("controller_available", False)) and not stale and not stopped and not starting
        running = bool(status.get("pid")) and not stopped and not stale
        client_busy = getattr(self, "client_busy", False)
        busy = self.restore_busy or client_busy
        self.enable_button.state(["disabled"] if busy or (enabled and running) else ["!disabled"])
        self.pause_button.state(["!disabled"] if enabled and not busy else ["disabled"])
        self.scan_button.state(["disabled"] if busy else ["!disabled"])
        self.restore_button.state(["disabled"] if busy or self.exiting or self.manual_busy else ["!disabled"])
        self.exit_button.state(["disabled"] if busy or self.exiting or self.manual_busy else ["!disabled"])
        if self.exiting:
            for button in (self.enable_button, self.pause_button, self.scan_button, self.restore_button):
                button.state(["disabled"])
        if client_busy:
            importing = getattr(getattr(self, "client_dialog", None), "importing", False)
            badge, color = "正在导入客户端认证" if importing else "正在保存客户端连接", "#1b66b0"
        elif self.manual_busy:
            badge, color = "正在手动切换", "#1b66b0"
        elif stale:
            badge, color = "后台状态未更新", "#8b6508"
        elif stopped:
            badge, color = "后台已停止", "#8b6508"
        elif starting:
            badge, color = "正在连接代理客户端", "#1b66b0"
        elif not enabled:
            badge, color = "已暂停自动切换", "#8b6508"
        elif not connected:
            badge, color = "等待代理客户端", "#8b6508"
        elif phase in ("scanning", "testing", "measuring"):
            badge, color = "正在测速", "#1b66b0"
        else:
            badge, color = "自动优选已启用", "#267042"
        self.badge.set(badge)
        tint = "#eaf6ef" if color == "#267042" else "#fff5df" if color == "#8b6508" else "#eaf2ff"
        self.badge_label.configure(foreground=color, background=tint)
        selected = str(status.get("selected") or "—")
        group = str(status.get("group") or "—")
        last_known = stale or stopped or starting or status.get("controller_available") is False
        node_label = (("上次记录的组内选择" if last_known else "组内选择") if selector_only else
                      "上次记录节点" if last_known else "当前节点")
        self.current.set(f"{node_label}：{selected}    代理组：{group}")
        self.node_title.set(selected if selected != "—" else "等待代理客户端连接")
        scope_note = "  ·  兼容模式：Codex 实际路径未验证" if selector_only else ""
        self.node_meta.set(f"{node_label}  ·  代理组：{group}{scope_note}")
        nodes = [item for item in (status.get("nodes") or []) if isinstance(item, dict)]
        try:
            scores_match = status.get("nodes_probe_identity") == core.probe_identity(core.validate_config(config))
        except (TypeError, ValueError):
            scores_match = False
        if not scores_match:
            nodes = [{"name": item.get("name"), "eligible": False,
                      "reason": "等待按当前测试目标重新测速"} for item in nodes]
        usable = sum(bool(item.get("eligible", False)) for item in nodes)
        errors = status.get("network_errors_recent", 0)
        connection_label = "状态待更新" if stale or stopped or starting else ("已连接" if connected else "未连接")
        self.health.set(f"控制接口：{connection_label}    最近网络错误：{errors}    可选节点：{usable} / {len(nodes)}")
        self.connection_value.set(connection_label)
        self.node_count_value.set(f"{usable} / {len(nodes)}")
        self.error_value.set(str(errors))
        self.scan_time_value.set("最近测速  " + display_time(status.get("last_scan_at") if scores_match else None))
        self.switch_time_value.set("最近切换  " + display_time(status.get("last_switch_at")))
        self.next_scan_value.set("下次测速  " + display_time(status.get("next_scan_at")))
        self.update_time_value.set("状态更新  " + display_time(status.get("updated_at")))
        self.timings.set(
            f"最近测速：{display_time(status.get('last_scan_at') if scores_match else None)}    "
            f"下次测速：{display_time(status.get('next_scan_at'))}    "
            f"最近切换：{display_time(status.get('last_switch_at'))}    "
            f"状态更新：{display_time(status.get('updated_at'))}"
        )
        if self.manual_busy:
            self.message.set(f"正在切换到“{self.manual_request_name}”…")
        elif time.monotonic() >= self.notice_until:
            if stale:
                description = "后台状态未更新，当前显示上次记录；可点击“立即测速”重试。"
            else:
                description = status.get("message") or "等待服务状态…"
                last_error = status.get("last_error")
                if last_error:
                    description = f"{description}  ·  {last_error}"
            if not status and time.monotonic() - self.started_at > 60:
                description = "暂未收到服务状态，可点击“立即测速”重试。"
            self.message.set(str(description)[:800])
        self.render_nodes(selected, nodes)
        self.after_id = self.root.after(2000, self.refresh)

    def render_nodes(self, selected: str, nodes: list[dict]) -> None:
        nodes = list(nodes)
        if selected != "—" and not any(row.get("name") == selected for row in nodes):
            nodes.insert(0, {"name": selected, "eligible": False, "reason": "当前节点不在本轮测速列表中"})
        signature = json.dumps([selected, nodes], ensure_ascii=False, sort_keys=True)
        if signature != self.node_signature:
            old_selection = self.table.selection()
            old_focus = self.table.focus()
            old_y, old_x = self.table.yview()[0], self.table.xview()[0]
            existing = set(self.table.get_children())
            visible = set()
            for index, node in enumerate(nodes):
                name = str(node.get("name") or "—")
                if name in visible:
                    continue
                visible.add(name)
                eligible = bool(node.get("eligible", False))
                reason = str(node.get("reason") or ("可选" if eligible else "暂不可选"))
                if name == selected:
                    reason = f"当前使用 · {reason}"
                samples = node.get("samples")
                sample_count = len(samples) if isinstance(samples, list) else samples
                values = (
                    name, number(node.get("score_ms")), number(node.get("median_ms")),
                    percentage(node.get("success_rate")), number(node.get("jitter_ms")),
                    "—" if sample_count is None else sample_count, reason,
                )
                tags = ("selected",) if name == selected else (() if eligible else ("unavailable",))
                if name in existing:
                    self.table.item(name, values=values, tags=tags)
                    self.table.move(name, "", index)
                else:
                    self.table.insert("", index, iid=name, values=values, tags=tags)
            for name in existing - visible:
                self.table.delete(name)
            self.table.selection_set([name for name in old_selection if name in visible])
            if old_focus in visible:
                self.table.focus(old_focus)
            self.table.yview_moveto(old_y)
            self.table.xview_moveto(old_x)
            self.node_signature = signature

    def hide_to_tray(self) -> None:
        if self.closed or self.exiting:
            return
        if self.tray is not None and self.tray.available:
            try:
                self.root.withdraw()
                self.hidden_to_tray = True
                return
            except tk.TclError:
                self.show_window()
        self.notify("系统托盘暂不可用，窗口已最小化到任务栏；可从任务栏重新打开。", 15)
        self.root.iconify()

    def show_window(self) -> None:
        if self.closed:
            return
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.hidden_to_tray = False

    def poll_tray(self) -> None:
        if self.closed:
            return
        try:
            while True:
                action = self.tray_actions.get_nowait()
                if action == "open":
                    self.show_window()
                elif action == "exit":
                    self.request_exit()
                elif action == "tray_unavailable":
                    if self.hidden_to_tray:
                        self.show_window()
                    self.notify("托盘图标暂不可用，已保留窗口以便操作。", 15)
                elif action == "exited":
                    self.close()
                    return
                elif action == "exit_failed":
                    self.exiting = False
                    self.show_window()
                    self.notify("后台尚未停止，已保留窗口。请稍后再点击退出程序。", 20)
        except queue.Empty:
            pass
        self.tray_after_id = self.root.after(200, self.poll_tray)

    def request_exit(self) -> None:
        if self.closed or self.exiting:
            return
        if getattr(self, "client_busy", False):
            self.show_window()
            self.notify("正在保存客户端连接，请完成后再退出。", 15)
            return
        if self.restore_busy:
            self.show_window()
            self.notify("正在恢复原节点，请等待恢复操作完成后再退出。", 15)
            return
        if getattr(self, "manual_busy", False):
            self.show_window()
            self.notify("正在手动切换节点，请等待结果后再退出。", 15)
            return
        self.exiting = True
        self.cancel_service_start()
        self.show_window()
        self.notify("正在停止后台优选并退出…", 45)

        def worker() -> None:
            try:
                if not wait_for_helper_startup(self.started_helpers):
                    self.tray_actions.put("exit_failed")
                    return
                result = subprocess.run(
                    [python_executable(), str(OPTIMIZER), "stop"], cwd=ROOT,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=NO_WINDOW, timeout=45, check=False,
                )
                self.tray_actions.put("exited" if result.returncode == 0 else "exit_failed")
            except (OSError, subprocess.TimeoutExpired):
                self.tray_actions.put("exit_failed")

        try:
            threading.Thread(target=worker, name="nearroute-exit", daemon=True).start()
        except RuntimeError:
            self.exiting = False
            self.notify("退出操作暂未启动，已保留窗口，请稍后重试。", 20)

    def cancel_service_start(self) -> None:
        if self.service_after_id is not None:
            try:
                self.root.after_cancel(self.service_after_id)
            except tk.TclError:
                pass
            self.service_after_id = None

    def close(self) -> None:
        """Dispose the UI; user exit first stops the helper via request_exit."""
        if self.closed:
            return
        self.closed = True
        for name in ("client_dialog", "settings_dialog", "update_dialog", "diagnostic_dialog"):
            panel = getattr(self, name, None)
            if panel is not None and not panel.closed:
                try:
                    panel.dispose()
                except tk.TclError:
                    pass
        for callback in (self.after_id, self.tray_after_id, self.service_after_id):
            if callback is not None:
                try:
                    self.root.after_cancel(callback)
                except tk.TclError:
                    pass
        try:
            if self.tray is not None:
                self.tray.stop()
        finally:
            try:
                if getattr(self, "window_properties_configured", False):
                    try:
                        clear_window(self.root)
                    except OSError:
                        pass
                self.root.destroy()
            except tk.TclError:
                pass


def main() -> None:
    if updater.is_installing(root=ROOT):
        return
    instance = WindowInstance()
    if not instance.acquire():
        return
    root = None
    dashboard = None
    try:
        taskbar_error = None
        try:
            configure_taskbar()
        except OSError as error:
            taskbar_error = str(error)
        root = tk.Tk()
        dashboard = Dashboard(root)
        try:
            configure_window(root)
            dashboard.window_properties_configured = True
        except OSError as error:
            taskbar_error = str(error)
        if taskbar_error:
            dashboard.notify(f"任务栏图标设置未完成：{taskbar_error}", 20)
        root.mainloop()
    finally:
        try:
            if dashboard is not None:
                dashboard.close()
            elif root is not None:
                try:
                    root.destroy()
                except tk.TclError:
                    pass
        finally:
            instance.close()


if __name__ == "__main__":
    main()
