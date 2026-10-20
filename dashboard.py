"""Native Windows control panel for NearRoute."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import optimizer as core
import updater
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
    else:
        scope = "地区 " + "、".join(cfg.get("scan_regions", []))
    preference = "日本优先" if cfg.get("prefer_japan", False) else "按线路评分择优"
    expected = ", ".join(str(value) for value in cfg.get("probe_expected_statuses", []))
    return (
        f"{preference} · 范围：{scope} · 健康检查 {cfg.get('health_interval_seconds')} 秒 / 测速 {cfg.get('scan_interval_seconds')} 秒\n"
        f"测试：{cfg.get('probe_url', '')} · 预期 HTTP {expected}"
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
    return subprocess.Popen(
        [python_executable(), str(OPTIMIZER), "run"],
        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
        close_fds=True,
    )


def wait_for_helper_startup(processes, timeout: float = 10.0) -> bool:
    """Wait until every helper we launched owns the lock or has exited.

    The helper publishes its PID after acquiring the mutex and clearing a stale
    stop request. Stopping before then could miss a still-starting child.
    """
    pending = list(processes)
    deadline = time.monotonic() + timeout
    while pending:
        try:
            active_pid = read_json(STATUS).get("pid")
        except (OSError, ValueError):
            active_pid = None
        pending = [p for p in pending if p.poll() is None and p.pid != active_pid]
        if not pending:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


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

    def __init__(self, owner: "Dashboard", *, load_nodes: bool = True, visible: bool = True):
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
        self.window = tk.Toplevel(owner.root)
        self.window.title("测速设置 · 近舟 NearRoute")
        self.window.geometry("840x760")
        self.window.minsize(760, 690)
        self.window.configure(background="#f4f6f9")
        self.window.transient(owner.root)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        if not visible:
            self.window.withdraw()
        self.url = tk.StringVar(value=str(cfg.get("probe_url", "")))
        self.expected = tk.StringVar(value=", ".join(str(value) for value in cfg.get("probe_expected_statuses", [])))
        self.mode = tk.StringVar(value=cfg.get("scan_mode", "regions"))
        self.regions = tk.StringVar(value=", ".join(cfg.get("scan_regions", [])))
        self.prefer_japan = tk.BooleanVar(value=cfg.get("prefer_japan", True))
        self.node_message = tk.StringVar(value=f"已保留 {len(self.selected_nodes)} 个已选节点，等待读取客户端。")

        form = ttk.Frame(self.window, padding=22)
        form.pack(fill="both", expand=True)
        form.columnconfigure(0, weight=1)
        form.rowconfigure(7, weight=1)
        ttk.Label(form, text="测速链接", font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.url).grid(row=1, column=0, sticky="ew", pady=(6, 10))
        response = ttk.Frame(form)
        response.grid(row=2, column=0, sticky="ew")
        ttk.Label(response, text="预期 HTTP 状态码").pack(side="left", padx=(0, 10))
        ttk.Entry(response, textvariable=self.expected, width=24).pack(side="left")
        ttk.Label(response, text="逗号分隔，例如 200,204", style="Muted.TLabel").pack(side="left", padx=(12, 0))
        help_label = ttk.Label(
            form, text="状态码须与目标地址匹配：常见连通性地址返回 204 或 200；Codex responses 地址的 GET 探测通常返回 405。修改链接不会自动修改状态码。",
            wraplength=790, style="Muted.TLabel",
        )
        help_label.grid(row=3, column=0, sticky="ew", pady=(8, 18))

        scope = ttk.Frame(form)
        scope.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(scope, text="测试节点范围", font=("Microsoft YaHei UI", 11, "bold")).pack(side="left", padx=(0, 16))
        for value, label in (("regions", "按地区"), ("nodes", "指定节点"), ("all", "全部节点")):
            ttk.Radiobutton(scope, text=label, variable=self.mode, value=value, command=self.update_mode).pack(side="left", padx=(0, 14))
        region_row = ttk.Frame(form)
        region_row.grid(row=5, column=0, sticky="ew")
        region_row.columnconfigure(1, weight=1)
        ttk.Label(region_row, text="地区代码").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.region_entry = ttk.Entry(region_row, textvariable=self.regions)
        self.region_entry.grid(row=0, column=1, sticky="ew")
        self.preset_button = ttk.Button(region_row, text="日本 / 台湾 / 新加坡 / 香港", command=lambda: self.regions.set("JP, TW, SG, HK"))
        self.preset_button.grid(row=0, column=2, padx=(10, 0))
        region_help = ttk.Label(
            form, text="可填任意两字母地区代码（如 US, DE）；UNKNOWN 包含无法识别地区的节点。地区来自节点名标记，不保证物理位置。",
            style="Muted.TLabel", wraplength=790,
        )
        region_help.grid(row=6, column=0, sticky="ew", pady=(6, 12))

        node_frame = ttk.Frame(form)
        node_frame.grid(row=7, column=0, sticky="nsew")
        node_frame.columnconfigure(0, weight=1)
        node_frame.rowconfigure(1, weight=1)
        node_actions = ttk.Frame(node_frame)
        node_actions.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(node_actions, text="指定节点（可多选）").pack(side="left", padx=(0, 12))
        self.refresh_button = ttk.Button(node_actions, text="刷新客户端节点", command=self.refresh_nodes)
        self.refresh_button.pack(side="left", padx=(0, 6))
        self.all_button = ttk.Button(node_actions, text="全选列表", command=self.select_all_nodes)
        self.all_button.pack(side="left", padx=(0, 6))
        self.clear_button = ttk.Button(node_actions, text="清空选择", command=self.clear_nodes)
        self.clear_button.pack(side="left")
        self.node_list = tk.Listbox(
            node_frame, selectmode=tk.EXTENDED, exportselection=False,
            font=("Microsoft YaHei UI", 10), height=8,
            background="#ffffff", foreground="#263445", selectbackground="#2364a1", selectforeground="#ffffff",
            relief="solid", borderwidth=1, activestyle="none",
        )
        self.node_list.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(node_frame, orient="vertical", command=self.node_list.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(node_frame, orient="horizontal", command=self.node_list.xview)
        horizontal.grid(row=2, column=0, sticky="ew")
        self.node_list.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        self.node_list.bind("<<ListboxSelect>>", self.selection_changed)
        node_status = ttk.Label(form, textvariable=self.node_message, style="Muted.TLabel", wraplength=790)
        node_status.grid(row=8, column=0, sticky="ew", pady=(6, 10))
        ttk.Checkbutton(form, text="日本优先（仍遵守当前测试范围和稳定性门槛）", variable=self.prefer_japan).grid(row=9, column=0, sticky="w")
        footer = ttk.Frame(form)
        footer.grid(row=10, column=0, sticky="ew", pady=(16, 0))
        ttk.Label(footer, text="保存后重新测速；已暂停时保持暂停。", style="Muted.TLabel").pack(side="left")
        ttk.Button(footer, text="保存设置", command=self.save).pack(side="right")
        ttk.Button(footer, text="取消", command=self.close).pack(side="right", padx=(0, 8))
        form.bind("<Configure>", lambda event: self.resize_text(event.width, (help_label, region_help, node_status)))
        self.render_nodes()
        self.update_mode()
        self.after_id = self.window.after(150, self.poll_results)
        if visible:
            self.window.grab_set()
        if load_nodes:
            self.refresh_nodes()

    @staticmethod
    def resize_text(width: int, labels: tuple) -> None:
        for label in labels:
            label.configure(wraplength=max(400, width - 44))

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

    def save(self) -> None:
        try:
            saved = save_settings(self.fields())
        except (OSError, ValueError) as error:
            messagebox.showerror("设置未保存", str(error), parent=self.window)
            return
        self.owner.settings_saved(saved)
        self.close()

    def close(self) -> None:
        self.closed = True
        if self.after_id is not None:
            self.window.after_cancel(self.after_id)
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()
        if getattr(self.owner, "settings_dialog", None) is self:
            self.owner.settings_dialog = None


class UpdateDialog:
    """Release notes and preferences; all network work belongs to the owner."""

    def __init__(self, owner: Dashboard):
        self.owner = owner
        self.closed = False
        self.window = tk.Toplevel(owner.root)
        self.window.title("近舟更新")
        self.window.geometry("660x530")
        self.window.minsize(520, 420)
        self.window.transient(owner.root)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        form = ttk.Frame(self.window, padding=20)
        form.pack(fill="both", expand=True)
        form.columnconfigure(0, weight=1)
        form.rowconfigure(2, weight=1)
        self.summary = tk.StringVar()
        ttk.Label(form, textvariable=self.summary, wraplength=610).grid(row=0, column=0, sticky="ew")
        ttk.Label(form, text="更新说明", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(14, 5))
        notes_frame = ttk.Frame(form)
        notes_frame.grid(row=2, column=0, sticky="nsew")
        notes_frame.columnconfigure(0, weight=1)
        notes_frame.rowconfigure(0, weight=1)
        self.notes = tk.Text(notes_frame, wrap="word", height=8, font=("Microsoft YaHei UI", 10))
        self.notes.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(notes_frame, command=self.notes.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.notes.configure(yscrollcommand=scrollbar.set)
        self.auto_check = tk.BooleanVar(value=owner.update_preferences["auto_check"])
        self.mode = tk.StringVar(value=owner.update_preferences["mode"])
        ttk.Checkbutton(form, text="自动检查新版本（每 6 小时）", variable=self.auto_check).grid(row=3, column=0, sticky="w", pady=(14, 4))
        modes = ttk.Frame(form)
        modes.grid(row=4, column=0, sticky="w")
        for label, value in (("提醒后由我安装", "notify"), ("自动下载，提醒安装", "download"), ("空闲时自动安装并重启", "install")):
            button = ttk.Radiobutton(modes, text=label, value=value, variable=self.mode)
            button.pack(anchor="w", pady=2)
            if value != "notify" and not owner.update_can_install:
                button.state(["disabled"])
        if not owner.update_can_install:
            ttk.Label(form, text="当前从源码运行，可查看版本并打开下载页；安装包支持应用内更新。", style="Muted.TLabel", wraplength=610).grid(row=5, column=0, sticky="ew", pady=(8, 0))
        actions = ttk.Frame(form)
        actions.grid(row=6, column=0, sticky="ew", pady=(16, 0))
        self.check_button = ttk.Button(actions, text="立即检查", command=lambda: owner.check_updates(manual=True))
        self.check_button.pack(side="left")
        self.install_button = ttk.Button(actions, command=owner.request_update_install)
        self.install_button.pack(side="left", padx=8)
        ttk.Button(actions, text="保存设置", command=self.save).pack(side="right")
        ttk.Button(actions, text="关闭", command=self.close).pack(side="right", padx=8)
        self.refresh()

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
        self.install_button.configure(text=("打开下载页" if not owner.update_can_install else "安装并重启" if owner.update_prepared else "下载并安装"))
        self.install_button.state(["disabled"] if busy or release is None else ["!disabled"])

    def save(self) -> None:
        try:
            self.owner.update_preferences = updater.save_preferences(self.auto_check.get(), self.mode.get(), root=ROOT)
        except (updater.UpdateError, OSError, ValueError) as error:
            messagebox.showerror("未能保存更新设置", str(error), parent=self.window)
            return
        self.owner.update_next_check = 0.0
        self.owner.notify("更新设置已保存。", 10)
        self.close()

    def close(self) -> None:
        self.closed = True
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
        self.update_next_check = 0.0
        self.update_can_install = updater.can_install(root=ROOT)
        self.update_preferences = updater.load_preferences(root=ROOT)

        root.title(WINDOW_TITLE)
        if os.name == "nt":
            try:
                root.iconbitmap(default=str(ICON_PATH))
            except tk.TclError:
                pass  # A missing icon must not prevent opening the dashboard.
        root.geometry("1080x710")
        root.minsize(900, 610)
        root.configure(background="#f4f6f9")
        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TFrame", background="#f4f6f9")
        style.configure("TLabel", background="#f4f6f9", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"), foreground="#182536")
        style.configure("Muted.TLabel", foreground="#64748b")
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(14, 7))
        style.configure("Treeview", font=("Microsoft YaHei UI", 9), rowheight=31)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

        container = ttk.Frame(root, padding=24)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(5, weight=1)
        self.wrapping_labels = []
        header = ttk.Frame(container)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=WINDOW_TITLE, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.badge = tk.StringVar(value="正在读取状态")
        self.badge_label = ttk.Label(header, textvariable=self.badge, style="Status.TLabel")
        self.badge_label.grid(row=0, column=1, sticky="e")
        self.policy = tk.StringVar(value="正在读取测速设置…")
        policy_label = ttk.Label(
            container,
            textvariable=self.policy,
            style="Muted.TLabel", wraplength=1010,
        )
        policy_label.grid(row=1, column=0, sticky="w", pady=(5, 18))
        self.wrapping_labels.append(policy_label)

        controls = ttk.Frame(container)
        controls.grid(row=2, column=0, sticky="ew", pady=(0, 16))
        self.enable_button = ttk.Button(controls, text="启用自动优选", command=self.enable)
        self.enable_button.pack(side="left", padx=(0, 8))
        self.pause_button = ttk.Button(controls, text="暂停自动切换", command=self.pause)
        self.pause_button.pack(side="left", padx=(0, 8))
        self.scan_button = ttk.Button(controls, text="立即测速", command=self.scan)
        self.scan_button.pack(side="left", padx=(0, 8))
        self.restore_button = ttk.Button(controls, text="恢复原节点", command=self.restore)
        self.restore_button.pack(side="left")
        self.settings_button = ttk.Button(controls, text="测速设置", command=self.open_settings)
        self.settings_button.pack(side="right")
        self.exit_button = ttk.Button(controls, text="退出程序", command=self.request_exit)
        self.exit_button.pack(side="right", padx=(0, 8))

        summary = ttk.Frame(container)
        summary.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        summary.columnconfigure(0, weight=1)
        self.current = tk.StringVar(value="当前节点：等待代理客户端连接")
        self.health = tk.StringVar(value="控制接口：等待检测")
        self.timings = tk.StringVar(value="最近测速：—    下次测速：—    最近切换：—")
        current_label = ttk.Label(summary, textvariable=self.current, wraplength=1010)
        current_label.grid(row=0, column=0, sticky="w")
        self.wrapping_labels.append(current_label)
        ttk.Label(summary, textvariable=self.health, style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        timing_label = ttk.Label(summary, textvariable=self.timings, style="Muted.TLabel", wraplength=1010)
        timing_label.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.wrapping_labels.append(timing_label)

        self.message = tk.StringVar(value="服务正在启动，请稍候…")
        message_label = ttk.Label(container, textvariable=self.message, wraplength=1010)
        message_label.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        self.wrapping_labels.append(message_label)

        table_frame = ttk.Frame(container)
        table_frame.grid(row=5, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("name", "score", "median", "success", "jitter", "samples", "eligible")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headings = ("节点", "综合分 / ms", "中位延迟 / ms", "成功率", "波动 / ms", "样本", "状态 / 原因")
        widths = (240, 100, 110, 80, 90, 65, 210)
        for key, title, width in zip(columns, headings, widths):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=55, anchor="w" if key in ("name", "eligible") else "center", stretch=key in ("name", "eligible"))
        self.table.tag_configure("selected", background="#e5f1ff", foreground="#174b80")
        self.table.tag_configure("unavailable", foreground="#8a919b")
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

        footer_label = ttk.Label(
            container,
            text="双击节点、按 Enter 或右键可手动切换；暂停自动优选时也可使用。延迟为 HTTP 线路探测结果。\n点击 × 收起到右下角托盘，双击托盘图标重新打开；退出程序会停止后台优选。",
            style="Muted.TLabel", wraplength=1010,
        )
        footer_label.grid(row=6, column=0, sticky="ew", pady=(14, 0))
        self.wrapping_labels.append(footer_label)
        update_row = ttk.Frame(container)
        update_row.grid(row=7, column=0, sticky="ew", pady=(8, 0))
        update_row.columnconfigure(0, weight=1)
        self.update_text = tk.StringVar(value=f"版本 {updater.current_version(root=ROOT)}")
        if self.update_release is not None:
            action = "查看并安装" if self.update_can_install else "查看更新与下载页"
            self.update_text.set(f"发现新版本 {self.update_release.version}，点击“版本与更新”{action}。")
        self.update_label = ttk.Label(update_row, textvariable=self.update_text, style="Muted.TLabel", wraplength=790)
        self.update_label.grid(row=0, column=0, sticky="w")
        self.wrapping_labels.append(self.update_label)
        self.update_button = ttk.Button(update_row, text="版本与更新", command=self.open_updates)
        self.update_button.grid(row=0, column=1, sticky="e")
        container.bind("<Configure>", self.resize_text)
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
                result = updater.read_update_result(root=ROOT)
                if result and self.update_release is None:
                    self.update_text.set(str(result.get("message") or "更新已完成。"))
            self.refresh()
        except Exception:
            self.close()
            raise

    def notify(self, text: str, duration: float = 8.0) -> None:
        self.message.set(text)
        self.notice_until = time.monotonic() + duration

    def open_updates(self) -> None:
        if self.exiting:
            return
        if self.update_dialog is not None and not self.update_dialog.closed:
            self.update_dialog.window.lift()
            return
        self.update_dialog = UpdateDialog(self)

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
            webbrowser.open(self.update_release.url)
            return
        if self.restore_busy or self.manual_busy:
            self.notify("正在处理节点操作，请完成后再安装更新。", 15)
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
        if self.closed or self.exiting or self.update_busy or self.restore_busy or self.manual_busy:
            return
        if self.update_prepared is None or not self.update_can_install:
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
        if self.closed or self.exiting or self.restore_busy or self.manual_busy or self.update_busy:
            return False
        if self.settings_dialog is not None and not self.settings_dialog.closed:
            return False
        if status.get("phase") not in ("monitoring", "paused", "waiting", "stopped"):
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
                    self.update_requested_install = False
                    self.update_text.set(f"更新未完成：{result}")
                    self.update_next_check = time.monotonic() + 1800
                elif operation == "check":
                    previous = self.update_release
                    self.update_release = result
                    if result is None:
                        self.update_text.set(f"当前已是最新版本 {updater.current_version(root=ROOT)}")
                    else:
                        if previous is None or previous.tag != result.tag:
                            self.update_prepared = None
                        action = "查看并安装" if self.update_can_install else "查看更新与下载页"
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
        auto_prepare = self.update_preferences["auto_check"] and mode in ("download", "install")
        if self.update_prepared is None and auto_prepare and getattr(self, "update_auto_prepare_attempted", None) != release.tag:
            self.update_auto_prepare_attempted = release.tag
            self.update_text.set(f"正在下载并校验版本 {release.version}…")
            self.update_worker("prepare", lambda: updater.prepare_update(release, root=ROOT))
        elif self.update_prepared is not None:
            auto_install = self.update_preferences["auto_check"] and mode == "install" and getattr(self, "update_auto_install_attempted", None) != release.tag
            if self.update_requested_install or auto_install:
                if self.automatic_install_ready(status):
                    self.update_auto_install_attempted = release.tag
                    self.begin_update_install()

    def resize_text(self, event: tk.Event) -> None:
        for label in self.wrapping_labels:
            label.configure(wraplength=max(400, event.width - 48))

    def ensure_service(self) -> bool:
        if self.closed or self.exiting or self.restore_busy:
            return False
        try:
            process = start_daemon()
            self.started_helpers = [p for p in self.started_helpers if p.poll() is None]
            self.started_helpers.append(process)
            return True
        except (OSError, ValueError) as error:
            self.notify(f"服务启动失败：{error}", 30)
            return False

    def open_settings(self) -> None:
        if self.restore_busy or self.exiting:
            return
        if self.settings_dialog is not None and not self.settings_dialog.closed:
            self.settings_dialog.window.lift()
            self.settings_dialog.window.focus_force()
            return
        try:
            self.settings_dialog = SettingsDialog(self)
        except (OSError, ValueError) as error:
            messagebox.showerror("无法打开设置", str(error), parent=self.root)

    def settings_saved(self, saved: dict) -> None:
        if self.closed or self.exiting:
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
        if self.restore_busy or self.exiting:
            return
        try:
            update_enabled(True)
            if self.ensure_service():
                self.notify("已启用自动优选，服务将按计划检查线路。")
        except (OSError, ValueError) as error:
            messagebox.showerror("未能启用", str(error), parent=self.root)

    def pause(self) -> None:
        if self.restore_busy or self.exiting:
            return
        try:
            update_enabled(False)
            self.notify("已暂停自动切换，当前节点继续使用。")
        except (OSError, ValueError) as error:
            messagebox.showerror("未能暂停", str(error), parent=self.root)

    def scan(self) -> None:
        if self.restore_busy or self.exiting:
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
        blocked = self.closed or self.exiting or self.restore_busy or self.manual_busy
        self.node_menu.entryconfigure(0, state="disabled" if blocked else "normal")
        try:
            self.node_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.node_menu.grab_release()
        return "break"

    def switch_node(self, name: str) -> None:
        if self.closed or self.exiting or self.restore_busy or self.manual_busy:
            return
        self.manual_busy = True
        self.manual_request_name = name
        self.manual_request_started = time.monotonic()
        self.manual_request_id = None
        attempt = self.manual_attempt = object()
        if not self.ensure_service():
            self.manual_busy = False
            self.manual_attempt = None
            self.notify("后台未启动，未提交手动切换；可重试。", 20)
            return
        self.notify(f"正在切换到“{name}”…", 30)

        def worker() -> None:
            try:
                request_id = core.request_manual_selection(name)
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
            request_id = self.manual_request_id
            self.manual_busy = False
            self.manual_attempt = None
            self.manual_request_id = None
            try:
                cancelled = bool(request_id and core.cancel_manual_selection(request_id))
                description = "已取消未处理请求，可重试。" if cancelled else "可重试；已开始的切换可能仍会完成。"
            except OSError:
                description = "请求清理未完成，请查看当前节点后重试。"
            self.notify("手动切换等待超时，" + description, 20)

    def restore(self) -> None:
        if self.restore_busy or self.exiting or getattr(self, "manual_busy", False):
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
        self.settings_button.state(["disabled"])
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
        self.poll_updates(status)
        if self.closed:
            return
        self.poll_manual_selection(status)
        try:
            config = read_json(CONFIG)
            enabled = bool(config.get("enabled", True))
            self.policy.set(policy_summary(config))
        except (OSError, ValueError):
            enabled = bool(status.get("enabled", True))
            self.policy.set(policy_summary({}))
        try:
            updated = datetime.fromisoformat(str(status.get("updated_at", "")).replace("Z", "+00:00"))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=CHINA_TIME)
            status_age = (datetime.now(CHINA_TIME) - updated).total_seconds()
            stale = status_age > STATUS_STALE_SECONDS
        except ValueError:
            stale = bool(status)
        phase = str(status.get("phase", "starting"))
        stopped = phase in ("stopped", "stopping")
        connected = bool(status.get("controller_available", False)) and not stale and not stopped
        running = bool(status.get("pid")) and not stopped and not stale
        self.enable_button.state(["disabled"] if self.restore_busy or (enabled and running) else ["!disabled"])
        self.pause_button.state(["!disabled"] if enabled and not self.restore_busy else ["disabled"])
        self.scan_button.state(["disabled"] if self.restore_busy else ["!disabled"])
        self.settings_button.state(["disabled"] if self.restore_busy else ["!disabled"])
        self.restore_button.state(["disabled"] if self.restore_busy or self.exiting or self.manual_busy else ["!disabled"])
        self.exit_button.state(["disabled"] if self.restore_busy or self.exiting or self.manual_busy else ["!disabled"])
        if self.exiting:
            for button in (self.enable_button, self.pause_button, self.scan_button, self.restore_button, self.settings_button):
                button.state(["disabled"])
        if self.manual_busy:
            badge, color = "正在手动切换", "#1b66b0"
        elif stale:
            badge, color = "后台状态未更新", "#8b6508"
        elif stopped:
            badge, color = "后台已停止", "#8b6508"
        elif not enabled:
            badge, color = "已暂停自动切换", "#8b6508"
        elif not connected:
            badge, color = "等待代理客户端", "#8b6508"
        elif phase in ("scanning", "testing", "measuring"):
            badge, color = "正在测速", "#1b66b0"
        else:
            badge, color = "自动优选已启用", "#267042"
        self.badge.set(badge)
        self.badge_label.configure(foreground=color)
        selected = str(status.get("selected") or "—")
        group = str(status.get("group") or "—")
        last_known = stale or stopped or status.get("controller_available") is False
        node_label = "上次记录节点" if last_known else "当前节点"
        self.current.set(f"{node_label}：{selected}    代理组：{group}")
        nodes = [item for item in (status.get("nodes") or []) if isinstance(item, dict)]
        usable = sum(bool(item.get("eligible", False)) for item in nodes)
        errors = status.get("network_errors_recent", 0)
        connection_label = "状态待更新" if stale or stopped else ("已连接" if connected else "未连接")
        self.health.set(f"控制接口：{connection_label}    最近网络错误：{errors}    可选节点：{usable} / {len(nodes)}")
        self.timings.set(
            f"最近测速：{display_time(status.get('last_scan_at'))}    "
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
                if self.settings_dialog is not None and not self.settings_dialog.closed:
                    self.settings_dialog.window.grab_release()
                    self.settings_dialog.window.withdraw()
                update_dialog = getattr(self, "update_dialog", None)
                if update_dialog is not None and not update_dialog.closed:
                    update_dialog.window.withdraw()
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
        if self.settings_dialog is not None and not self.settings_dialog.closed:
            window = self.settings_dialog.window
            window.deiconify()
            window.lift()
            window.grab_set()
        update_dialog = getattr(self, "update_dialog", None)
        if update_dialog is not None and not update_dialog.closed:
            update_dialog.window.deiconify()
            update_dialog.window.lift()

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
                    creationflags=NO_WINDOW, timeout=35, check=False,
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
        update_dialog = getattr(self, "update_dialog", None)
        if update_dialog is not None and not update_dialog.closed:
            try:
                update_dialog.close()
            except tk.TclError:
                pass
        if self.settings_dialog is not None and not self.settings_dialog.closed:
            try:
                self.settings_dialog.close()
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
