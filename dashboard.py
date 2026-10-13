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
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import optimizer as core


ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
CONFIG = ROOT / "config.json"
STATUS = STATE / "status.json"
OPTIMIZER = ROOT / "optimizer.py"
CHINA_TIME = timezone(timedelta(hours=8))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WINDOW_TITLE = "近舟 NearRoute"


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
    data = read_json(CONFIG) if CONFIG.exists() else {}
    data["enabled"] = enabled
    atomic_write_config(data)


def save_settings(fields: dict) -> dict:
    # Read again at save time so toggles or unrelated external edits are retained.
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


def start_daemon() -> None:
    if not OPTIMIZER.is_file():
        raise FileNotFoundError("未找到 optimizer.py，请先完成安装。")
    subprocess.Popen(
        [python_executable(), str(OPTIMIZER), "run"],
        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
        close_fds=True,
    )


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


class Dashboard:
    def __init__(self, root: tk.Tk, *, start_service: bool = True):
        self.root = root
        self.closed = False
        self.results: queue.SimpleQueue[tuple[bool, str]] = queue.SimpleQueue()
        self.notice_until = 0.0
        self.node_signature = ""
        self.restore_busy = False
        self.settings_dialog: SettingsDialog | None = None
        self.started_at = time.monotonic()
        self.after_id = None

        root.title(WINDOW_TITLE)
        root.geometry("1080x710")
        root.minsize(900, 610)
        root.configure(background="#f4f6f9")
        root.protocol("WM_DELETE_WINDOW", self.close)
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

        footer_label = ttk.Label(
            container,
            text="延迟为 HTTP 线路探测结果，不是模型首字时间。切换作用于当前主代理组；已建立连接不主动关闭。\n关闭窗口后，后台优选仍会继续运行。恢复原节点会同时暂停自动切换。",
            style="Muted.TLabel", wraplength=1010,
        )
        footer_label.grid(row=6, column=0, sticky="ew", pady=(14, 0))
        self.wrapping_labels.append(footer_label)
        container.bind("<Configure>", self.resize_text)
        if start_service:
            root.after(100, self.ensure_service)
        self.refresh()

    def notify(self, text: str, duration: float = 8.0) -> None:
        self.message.set(text)
        self.notice_until = time.monotonic() + duration

    def resize_text(self, event: tk.Event) -> None:
        for label in self.wrapping_labels:
            label.configure(wraplength=max(400, event.width - 48))

    def ensure_service(self) -> bool:
        try:
            start_daemon()
            return True
        except (OSError, ValueError) as error:
            self.notify(f"服务启动失败：{error}", 30)
            return False

    def open_settings(self) -> None:
        if self.restore_busy:
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
        if self.restore_busy:
            return
        try:
            update_enabled(True)
            if self.ensure_service():
                self.notify("已启用自动优选，服务将按计划检查线路。")
        except (OSError, ValueError) as error:
            messagebox.showerror("未能启用", str(error), parent=self.root)

    def pause(self) -> None:
        if self.restore_busy:
            return
        try:
            update_enabled(False)
            self.notify("已暂停自动切换，当前节点继续使用。")
        except (OSError, ValueError) as error:
            messagebox.showerror("未能暂停", str(error), parent=self.root)

    def scan(self) -> None:
        if self.restore_busy:
            return
        try:
            if not self.ensure_service():
                return
            STATE.mkdir(parents=True, exist_ok=True)
            (STATE / "scan.request").touch()
            self.notify("已请求完整测速，结果会自动刷新。", 15)
        except OSError as error:
            messagebox.showerror("未能开始测速", str(error), parent=self.root)

    def restore(self) -> None:
        if self.restore_busy:
            return
        try:
            update_enabled(False)
        except (OSError, ValueError) as error:
            messagebox.showerror("未能暂停自动切换", str(error), parent=self.root)
            return
        self.restore_busy = True
        self.restore_button.state(["disabled"])
        self.enable_button.state(["disabled"])
        self.pause_button.state(["disabled"])
        self.scan_button.state(["disabled"])
        self.settings_button.state(["disabled"])
        self.notify("正在恢复原节点，自动切换已暂停…", 90)

        def worker() -> None:
            try:
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
        try:
            config = read_json(CONFIG)
            enabled = bool(config.get("enabled", True))
            self.policy.set(policy_summary(config))
        except (OSError, ValueError):
            enabled = bool(status.get("enabled", True))
            self.policy.set(policy_summary({}))
        connected = bool(status.get("controller_available", False))
        phase = str(status.get("phase", "starting"))
        running = bool(status.get("pid")) and phase not in ("stopped", "stopping")
        self.enable_button.state(["disabled"] if self.restore_busy or (enabled and running) else ["!disabled"])
        self.pause_button.state(["!disabled"] if enabled and not self.restore_busy else ["disabled"])
        self.scan_button.state(["disabled"] if self.restore_busy else ["!disabled"])
        self.settings_button.state(["disabled"] if self.restore_busy else ["!disabled"])
        if not enabled:
            badge, color = "已暂停自动切换", "#8b6508"
        elif not connected:
            badge, color = "等待代理客户端", "#8b6508"
        elif phase in ("scanning", "testing", "measuring"):
            badge, color = "正在测速", "#1b66b0"
        elif phase in ("stopped", "stopping"):
            badge, color = "后台已停止", "#8b6508"
        else:
            badge, color = "自动优选已启用", "#267042"
        self.badge.set(badge)
        self.badge_label.configure(foreground=color)
        selected = str(status.get("selected") or "—")
        group = str(status.get("group") or "—")
        self.current.set(f"当前节点：{selected}    代理组：{group}")
        nodes = [item for item in (status.get("nodes") or []) if isinstance(item, dict)]
        usable = sum(bool(item.get("eligible", False)) for item in nodes)
        errors = status.get("network_errors_recent", 0)
        self.health.set(f"控制接口：{'已连接' if connected else '未连接'}    最近网络错误：{errors}    可选节点：{usable} / {len(nodes)}")
        self.timings.set(
            f"最近测速：{display_time(status.get('last_scan_at'))}    "
            f"下次测速：{display_time(status.get('next_scan_at'))}    "
            f"最近切换：{display_time(status.get('last_switch_at'))}"
        )
        if time.monotonic() >= self.notice_until:
            description = status.get("message") or "等待服务状态…"
            last_error = status.get("last_error")
            if last_error:
                description = f"{description}  ·  {last_error}"
            if not status and time.monotonic() - self.started_at > 60:
                description = "暂未收到服务状态，可点击“立即测速”重试。"
            self.message.set(str(description)[:800])
        signature = json.dumps([selected, nodes], ensure_ascii=False, sort_keys=True)
        if signature != self.node_signature:
            old_position = self.table.yview()[0]
            self.table.delete(*self.table.get_children())
            for node in nodes:
                name = str(node.get("name") or "—")
                eligible = bool(node.get("eligible", False))
                reason = str(node.get("reason") or ("可选" if eligible else "暂不可选"))
                if name == selected:
                    reason = f"当前使用 · {reason}"
                samples = node.get("samples")
                sample_count = len(samples) if isinstance(samples, list) else samples
                self.table.insert("", "end", values=(
                    name, number(node.get("score_ms")), number(node.get("median_ms")),
                    percentage(node.get("success_rate")), number(node.get("jitter_ms")),
                    "—" if sample_count is None else sample_count, reason,
                ), tags=("selected",) if name == selected else (() if eligible else ("unavailable",)))
            self.table.yview_moveto(old_position)
            self.node_signature = signature
        self.after_id = self.root.after(2000, self.refresh)

    def close(self) -> None:
        self.closed = True
        if self.settings_dialog is not None and not self.settings_dialog.closed:
            self.settings_dialog.close()
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
        self.root.destroy()


def main() -> None:
    instance = WindowInstance()
    if not instance.acquire():
        return
    try:
        root = tk.Tk()
        Dashboard(root)
        root.mainloop()
    finally:
        instance.close()


if __name__ == "__main__":
    main()
