"""Codex-aware node selection for the local Maomaoyun Mihomo controller.

No credentials, subscription URLs, encrypted client files, or Codex prompts are
needed. Controller selection changes affect new connections in the existing
main proxy group. Established connections are never deleted by this program.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
from contextlib import contextmanager
import ctypes
import hashlib
import http.client
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import ssl
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from functools import partial

import client_detection
import windows_app

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
CONFIG = ROOT / "config.json"
try:
    RUNTIME_VERSION = (ROOT / "VERSION").read_text(encoding="utf-8-sig").strip()
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", RUNTIME_VERSION):
        RUNTIME_VERSION = None
except (OSError, UnicodeError):
    RUNTIME_VERSION = None
PROBE_HOST = "chatgpt.com"
PROBE_PATH = "/backend-api/codex/responses"
PROBE_URL = "https://" + PROBE_HOST + PROBE_PATH
PROBE_REQUEST_TIMEOUT = 7.0
TZ = timezone(timedelta(hours=8))
DEFAULTS = {
    "enabled": True,
    "controller": "http://127.0.0.1:9790",
    "controller_secret_env": "",
    "controller_api_mode": "standard",
    "group": "猫猫云",
    "auto_detect_client": True,
    "auto_select_group": True,
    "probe_url": PROBE_URL,
    "probe_expected_statuses": [405],
    "scan_interval_seconds": 300,
    "health_interval_seconds": 15,
    "scan_regions": ["JP", "TW", "SG", "HK"],
    "scan_mode": "regions",
    "scan_nodes": [],
    "prefer_japan": True,
    "japan_poor_latency_ms": 500,
    "japan_fallback_score_ratio": 0.6,
    "probe_timeout_ms": 4000,
    "concurrency": 6,
    "shortlist_size": 12,
    "confirmation_rounds": 2,
    "min_improvement_ms": 60,
    "min_improvement_ratio": 0.25,
    "switch_cooldown_seconds": 1800,
    "manual_selection_grace_seconds": 600,
    "max_candidate_attempts": 3,
    "node_quarantine_seconds": 1800,
    "history_seconds": 1800,
    "history_samples": 12,
    "verification_idle_seconds": 5,
    "codex_log_db": str(Path.home() / ".codex" / "logs_2.sqlite"),
}


def iso(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts, TZ).isoformat(timespec="seconds")


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        tmp.unlink(missing_ok=True)


def load_config():
    if not CONFIG.exists():
        with config_update_lock():
            if not CONFIG.exists():
                atomic_json(CONFIG, DEFAULTS)
    raw = read_json(CONFIG, None)
    return validate_config(raw)


@contextmanager
def _file_lock(path):
    """Serialize local file updates across GUI/helper processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if not stream.tell():
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def config_update_lock(config_path=None):
    path = CONFIG if config_path is None else Path(config_path)
    return _file_lock(path.parent / "state" / "config.lock")


def _manual_request_lock():
    # Controller requests run after the helper has claimed and released the queue.
    return _file_lock(STATE / "manual-request.lock")


def request_manual_selection(name, expected_context=None, expected_mode=None):
    """Queue a user choice for the helper, which serializes all controller PUTs."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("请选择要切换的节点")
    cfg = load_config()
    if expected_mode is not None:
        if not isinstance(expected_mode, (list, tuple)) or len(expected_mode) != 2:
            raise ValueError("节点列表的代理模式无效，请刷新后重新选择")
        mode, group = expected_mode
        valid_group = group is None if mode == "direct" else isinstance(group, str) and bool(group.strip())
        allowed_modes = ("rule", "global", "direct", "unknown") if cfg["controller_api_mode"] == "selector" else ("rule", "global", "direct")
        if mode not in allowed_modes or not valid_group:
            raise ValueError("节点列表的代理模式无效，请刷新后重新选择")
        expected_mode = list(expected_mode)
    _check_expected_context(cfg, expected_context)
    request_id = uuid.uuid4().hex
    with _manual_request_lock():
        atomic_json(STATE / "select.request.json", {
            "request_id": request_id, "name": name,
            **controller_identity(cfg), "group": cfg["group"],
            "controller_api_mode": cfg["controller_api_mode"],
            **({"expected_mode": expected_mode} if expected_mode is not None else {}),
        })
    return request_id


def cancel_manual_selection(request_id):
    """Remove only this request if the helper has not consumed it yet."""
    path = STATE / "select.request.json"
    with _manual_request_lock():
        if read_json(path, {}).get("request_id") != request_id:
            return False
        path.unlink(missing_ok=True)
        return True


def validate_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("config.json 不是有效的 JSON 对象，自动切换已停止")
    cfg = DEFAULTS | raw
    try:
        url = cfg["controller"]
        if not isinstance(url, str) or not url or any(ord(char) <= 32 or ord(char) >= 127 for char in url) or "\\" in url:
            raise ValueError
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or parsed.username is not None or parsed.password is not None or "?" in url or "#" in url
                or parsed.netloc.endswith(":")
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("控制接口必须为本机 HTTP 地址，不能含账号密码、查询参数、片段或无效端口") from None
    secret_env = cfg["controller_secret_env"]
    if not isinstance(secret_env, str) or (secret_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", secret_env)):
        raise ValueError("密钥环境变量须为合法变量名或留空，不要填写密钥本身")
    if cfg["controller_api_mode"] not in ("standard", "selector"):
        raise ValueError("controller_api_mode 必须为 standard 或 selector")
    if not isinstance(cfg["group"], str) or not cfg["group"].strip():
        raise ValueError("请选择非空的代理组名称")
    if type(cfg["enabled"]) is not bool:
        raise ValueError("enabled 必须为 true 或 false")
    if type(cfg["prefer_japan"]) is not bool:
        raise ValueError("prefer_japan 必须为 true 或 false")
    for key in ("auto_detect_client", "auto_select_group"):
        if type(cfg[key]) is not bool:
            raise ValueError(key + " 必须为 true 或 false")
    if cfg["scan_mode"] not in ("adaptive", "regions", "nodes", "all"):
        raise ValueError("scan_mode 必须为 adaptive、regions、nodes 或 all")
    regions = cfg["scan_regions"]
    if not isinstance(regions, list) or any(not isinstance(region, str) or not re.fullmatch(r"[A-Za-z]{2}|UNKNOWN", region.upper()) for region in regions):
        raise ValueError("scan_regions 必须为两字母地区码或 UNKNOWN 的列表")
    cfg["scan_regions"] = list(dict.fromkeys("GB" if r.upper() == "UK" else r.upper() for r in regions))
    nodes = cfg["scan_nodes"]
    if not isinstance(nodes, list) or any(not isinstance(n, str) or not n.strip() for n in nodes):
        raise ValueError("scan_nodes 必须为完整节点名的列表")
    cfg["scan_nodes"] = list(dict.fromkeys(nodes))
    if cfg["scan_mode"] == "regions" and not regions:
        raise ValueError("按地区扫描时，请至少选择一个地区")
    if cfg["scan_mode"] == "nodes" and not nodes:
        raise ValueError("手动选点时，请至少选择一个节点")
    parse_probe_url(cfg["probe_url"])
    statuses = cfg["probe_expected_statuses"]
    if (not isinstance(statuses, list) or not 1 <= len(statuses) <= 10
            or any(type(s) is not int or not 100 <= s <= 599 for s in statuses)):
        raise ValueError("预期 HTTP 状态码必须为 1 至 10 个 100–599 之间的整数")
    cfg["probe_expected_statuses"] = list(dict.fromkeys(statuses))
    bounds = {
        "scan_interval_seconds": (60, 3600), "health_interval_seconds": (10, 300),
        "probe_timeout_ms": (1000, 15000), "concurrency": (1, 8),
        "shortlist_size": (3, 30), "confirmation_rounds": (2, 5),
        "switch_cooldown_seconds": (30, 3600), "manual_selection_grace_seconds": (60, 3600),
        "max_candidate_attempts": (1, 5), "node_quarantine_seconds": (60, 86400),
        "history_seconds": (300, 7200), "history_samples": (3, 50),
        "verification_idle_seconds": (1, 20), "min_improvement_ms": (0, 10000),
        "min_improvement_ratio": (0, 0.95),
        "japan_poor_latency_ms": (100, 10000), "japan_fallback_score_ratio": (0.1, 0.9),
    }
    for key, (low, high) in bounds.items():
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("配置数值超出允许范围: " + key)
    return cfg


def parse_probe_url(url):
    try:
        if not isinstance(url, str) or not url or any(ord(c) <= 32 or ord(c) >= 127 for c in url):
            raise ValueError
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "\\" in url):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise ValueError("测试链接须为完整 HTTP/HTTPS URL，不能含账号密码、片段或空白；中文路径请先进行 URL 编码") from None


REGION_LABELS = {
    "JP": ("日本", "东京", "東京", "大阪", "Japan", "Tokyo", "Osaka"),
    "TW": ("台湾", "臺灣", "台灣", "台北", "臺北", "Taiwan", "Taipei"),
    "SG": ("新加坡", "狮城", "獅城", "Singapore"),
    "HK": ("香港", "Hong Kong", "Hong-Kong", "HongKong"),
    "US": ("美国", "美國", "United States", "USA", "America"),
    "GB": ("英国", "英國", "United Kingdom", "Britain", "UK"),
    "DE": ("德国", "德國", "Germany"), "FR": ("法国", "法國", "France"),
    "KR": ("韩国", "韓國", "Korea", "Seoul"), "RU": ("俄罗斯", "俄羅斯", "Russia"),
    "AU": ("澳大利亚", "澳大利亞", "Australia"), "CA": ("加拿大", "Canada"),
    "CN": ("中国", "中國", "大陆", "大陸", "China"), "MO": ("澳门", "澳門", "Macau", "Macao"),
    "MY": ("马来西亚", "馬來西亞", "Malaysia"), "TH": ("泰国", "泰國", "Thailand"),
    "VN": ("越南", "Vietnam"), "PH": ("菲律宾", "菲律賓", "Philippines"),
    "ID": ("印度尼西亚", "印度尼西亞", "印尼", "Indonesia"), "IN": ("印度", "India"),
    "KH": ("柬埔寨", "Cambodia"), "AE": ("迪拜", "阿联酋", "阿聯酋", "Dubai", "UAE"),
    "BR": ("巴西", "Brazil"), "GR": ("希腊", "希臘", "Greece"),
    "ES": ("西班牙", "Spain"), "IT": ("意大利", "義大利", "Italy"),
    "PT": ("葡萄牙", "Portugal"), "BE": ("比利时", "比利時", "Belgium"),
    "AR": ("阿根廷", "Argentina"), "MX": ("墨西哥", "Mexico"),
    "IE": ("爱尔兰", "愛爾蘭", "Ireland"), "UA": ("乌克兰", "烏克蘭", "Ukraine"),
    "TR": ("土耳其", "Turkey", "Turkiye"), "PK": ("巴基斯坦", "Pakistan"),
    "NL": ("荷兰", "荷蘭", "Netherlands"), "SE": ("瑞典", "Sweden"),
    "CH": ("瑞士", "Switzerland"), "FI": ("芬兰", "芬蘭", "Finland"),
    "NO": ("挪威", "Norway"), "NZ": ("新西兰", "紐西蘭", "New Zealand"),
}
_REGION_PATTERNS = {
    code: re.compile("|".join(
        re.escape(label) if any(ord(c) > 127 for c in label)
        else r"(?<![A-Za-z0-9])" + re.escape(label) + r"(?![A-Za-z0-9])"
        for label in (code, *labels)), re.IGNORECASE)
    for code, labels in REGION_LABELS.items()
}
_FLAG_PATTERN = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")


def node_region(name):
    """Use the first location label; labels do not prove physical location.

    A CN flag paired with an explicit Taiwan label is a provider convention.
    All other flags identify their region, including regions outside defaults.
    """
    matches = []
    for code, pattern in _REGION_PATTERNS.items():
        match = pattern.search(name)
        if match:
            matches.append((match.start(), -len(match.group()), code))
    for match in _FLAG_PATTERN.finditer(name):
        code = "".join(chr(ord(c) - 0x1F1E6 + 65) for c in match.group())
        if code == "CN" and any(item[2] == "TW" for item in matches):
            continue
        matches.append((match.start(), -2, code))
    return min(matches)[2] if matches else None


def is_japan_node(name):
    return node_region(name) == "JP"


def filter_scan_candidates(names, allowed_regions):
    allowed = set(allowed_regions)
    return [name for name in names if (node_region(name) or "UNKNOWN") in allowed]


def scan_candidates(names, cfg):
    if cfg["scan_mode"] == "adaptive":
        return filter_scan_candidates(names, client_detection.nearby_regions(names, node_region))
    if cfg["scan_mode"] == "all":
        return list(names)
    if cfg["scan_mode"] == "nodes":
        selected = set(cfg["scan_nodes"])
        return [name for name in names if name in selected]
    return filter_scan_candidates(names, cfg["scan_regions"])


def controller_identity(cfg):
    return {key: cfg[key] for key in ("controller", "controller_secret_env")}


def same_controller(record, cfg):
    # Missing ownership is unknown, including records made by older versions.
    return all(key in record and record[key] == value for key, value in controller_identity(cfg).items())


def state_context(cfg):
    context = (cfg["controller"], cfg["controller_secret_env"], cfg["group"])
    return context + ("selector",) if cfg.get("controller_api_mode") == "selector" else context


def _check_expected_context(cfg, expected_context):
    if expected_context is not None and (
            not isinstance(expected_context, (list, tuple)) or tuple(expected_context) != state_context(cfg)):
        raise ValueError("代理客户端设置已改变，请刷新后重试")


def _check_client_environment(cfg):
    name = cfg["controller_secret_env"]
    if name and not _controller_secret(cfg):
        if re.fullmatch(r"NEARROUTE_MAOMAO_SECRET_[0-9]+", name):
            raise ValueError("未找到有效的猫猫云认证，请在客户端连接中点击“导入猫猫云认证”")
        raise ValueError("当前进程未找到非空的密钥环境变量 " + name + "，请设置后重新启动近舟")


def _controller_secret(cfg):
    name = cfg.get("controller_secret_env", "")
    managed = re.fullmatch(r"NEARROUTE_MAOMAO_SECRET_([0-9]+)", name)
    if windows_app.IS_WINDOWS and managed:
        # Import refreshes HKCU while an existing helper retains its old process
        # environment. Managed credentials are read afresh, including revocation.
        if int(managed[1]) != (urllib.parse.urlsplit(cfg["controller"]).port or 80):
            return ""
        try:
            credential = windows_app._read_user_environment(name)
            return credential if re.fullmatch(r"[0-9a-fA-F]{48}", credential) else ""
        except (OSError, ValueError, TypeError, ImportError):
            return ""
    return os.environ.get(name, "")


def owned_records(data):
    return data.get("records", []) if data.get("schema_version") == 2 else []


def record_document(data, records):
    if data.get("schema_version") == 2:
        result = dict(data)
    else:
        result = {"schema_version": 2}
        if data:
            result["legacy"] = data
    result["records"] = records
    return result


def save_owned_record(path, record, latest=False):
    data = read_json(path, {})
    records = [item for item in owned_records(data)
               if not (same_controller(item, record) and item["group"] == record["group"])]
    data = record_document(data, records + [record])
    if latest:
        data = {key: value for key, value in data.items() if key in ("schema_version", "records", "legacy")} | record
    atomic_json(path, data)


def retire_owned_record(path, cfg, group):
    data = read_json(path, {})
    records = owned_records(data)
    remaining = [item for item in records if not (same_controller(item, cfg) and item["group"] == group)]
    if len(remaining) == len(records):
        return
    if same_controller(data, cfg) and data.get("group") == group:
        data = {key: value for key, value in data.items() if key in ("schema_version", "records", "legacy")}
    atomic_json(path, record_document(data, remaining))


def current_selection(cfg):
    records = owned_records(read_json(STATE / "selection.json", {}))
    matching = [item for item in records if same_controller(item, cfg)
                and item.get("configured_group", item["group"]) == cfg["group"]]
    return max(matching, key=lambda item: item.get("last_switch_at", 0), default={})


def pending_record(data):
    # Early journals named the controller but did not yet name an auth variable.
    return {"controller_secret_env": "", **data} if data.get("controller") else data


def probe_identity(cfg):
    return hashlib.sha256(json.dumps([state_context(cfg), cfg["probe_url"], sorted(cfg["probe_expected_statuses"])]).encode()).hexdigest()


def selection_policy(cfg):
    return (cfg["scan_mode"], tuple(cfg["scan_regions"]), tuple(cfg["scan_nodes"]),
            cfg["prefer_japan"], cfg["japan_poor_latency_ms"], cfg["japan_fallback_score_ratio"],
            probe_identity(cfg), state_context(cfg))


def select_shortlist(fresh, current, limit, prefer_japan=True):
    successful = [(name, delay) for name, delay in fresh.items()
                  if isinstance(delay, (int, float)) and not isinstance(delay, bool)
                  and math.isfinite(delay) and delay > 0]
    ordered = [name for name, delay in sorted(successful, key=lambda x: (x[1], x[0]))]
    if prefer_japan:
        japan = [name for name in ordered if is_japan_node(name)]
        other = [name for name in ordered if not is_japan_node(name)]
        japan_slots = max(1, limit * 2 // 3)
        chosen = japan[:japan_slots] + other[:limit - japan_slots]
        chosen += [name for name in ordered if name not in chosen][:max(0, limit - len(chosen))]
    else:
        chosen = ordered[:limit]
    # A manually selected out-of-scope node must never leak back into the scan.
    if current in fresh and current not in chosen:
        chosen.append(current)
    return chosen


def apply_region_policy(stats, prefer_japan=True, poor_latency_ms=500, fallback_ratio=0.6):
    rows = [dict(row) for row in stats]
    if not prefer_japan:
        return rows
    viable = [row for row in rows if row.get("eligible") and row.get("score_ms") is not None
              and math.isfinite(row["score_ms"])]
    japanese = [row for row in viable if is_japan_node(row["name"])]
    if not japanese:
        return rows
    best = min(japanese, key=lambda row: (row["score_ms"], row["name"]))
    alternatives = [row for row in viable if not is_japan_node(row["name"])
                    and row["score_ms"] <= best["score_ms"] * fallback_ratio]
    slow = best.get("median_ms") is not None and best["median_ms"] >= poor_latency_ms
    unreliable = best.get("samples", 0) >= 5 and best.get("success_rate", 0) < 0.9
    allowed = {row["name"] for row in alternatives
               if slow or (unreliable and row.get("success_rate", 0) >= 0.95)}
    for row in rows:
        if row.get("eligible") and not is_japan_node(row["name"]) and row["name"] not in allowed:
            row.update(eligible=False, reason="日本优先，其他地区备用", region_standby=True)
    return rows


def summarize_samples(name, samples, min_samples=3):
    valid = sorted(float(x) for x in samples if isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x) and x > 0)
    rate = len(valid) / len(samples) if samples else 0.0
    median = statistics.median(valid) if valid else None
    jitter = valid[math.ceil(0.9 * len(valid)) - 1] - median if valid else None
    score = median + 0.5 * jitter + (1 - rate) * 2000 if valid else None
    eligible = len(valid) >= min_samples and rate >= 0.8
    reason = "可用" if eligible else ("样本不足" if len(valid) < min_samples else "近期探测失败较多")
    return {"name": name, "median_ms": median, "jitter_ms": jitter,
            "success_rate": rate, "samples": len(samples), "score_ms": score,
            "eligible": eligible, "reason": reason}


def choose_candidate(stats, current, last_switch_at, now, min_improvement_ms=60,
                     min_improvement_ratio=0.25, cooldown_seconds=1800, current_failed=False):
    viable = [s for s in stats if s.get("eligible") and s.get("score_ms") is not None
              and math.isfinite(s["score_ms"])]
    if current_failed:
        viable = [s for s in viable if s["name"] != current]
    if not viable:
        return None, "没有通过稳定性门槛的候选节点"
    best = min(viable, key=lambda s: (s["score_ms"], s["name"]))
    old = next((s for s in stats if s["name"] == current and s.get("eligible")), None)
    if best["name"] == current:
        return None, "当前节点已是本轮最优"
    if current_failed:
        return best["name"], "当前节点不可用，故障切换"
    region_standby = old is None and any(s["name"] == current and s.get("region_standby") for s in stats)
    if last_switch_at and now - last_switch_at < cooldown_seconds:
        return None, "等待地区优选切换冷却结束" if region_standby else "处于切换冷却期"
    if old is None:
        if region_standby:
            reason = "日本线路已符合优先条件，切回日本" if is_japan_node(best["name"]) else "改用符合地区备用条件的节点"
            return best["name"], reason
        return best["name"], "当前节点暂无可比较评分，改用符合稳定性门槛的节点"
    improvement = old["score_ms"] - best["score_ms"]
    ratio = improvement / old["score_ms"] if old["score_ms"] else 0
    if improvement + 1e-9 < min_improvement_ms or ratio + 1e-9 < min_improvement_ratio:
        return None, "提升不足切换门槛，保持当前连接路径"
    return best["name"], "候选节点延迟和稳定性评分更优"


def flatten_candidates(proxies, group):
    found, visited = [], set()
    excluded = {"DIRECT", "REJECT", "REJECTDROP", "PASS", "COMPATIBLE"}
    def visit(name):
        if name in visited:
            return
        visited.add(name)
        item = proxies.get(name)
        if not item:
            return
        normalized_type = str(item.get("type", "")).upper().replace("-", "").replace("_", "")
        normalized_name = name.upper().replace("-", "").replace("_", "")
        if normalized_type in excluded or normalized_name in excluded:
            return
        if isinstance(item.get("all"), list):
            for child in item["all"]:
                visit(child)
        else:
            found.append(name)
    visit(group)
    return found


def resolve_leaf(proxies, name):
    seen = set()
    while name not in seen:
        seen.add(name)
        item = proxies.get(name, {})
        if "all" not in item:
            return name
        name = item.get("now")
        if not name:
            return None
    return None


class ControllerError(RuntimeError):
    pass


class ConfigReadUnsupported(ControllerError):
    unsupported_config_read = True

    def __init__(self):
        super().__init__(client_detection.CONFIG_READ_UNSUPPORTED_MESSAGE)


class PathChanged(ControllerError):
    pass


class ModeChanged(PathChanged):
    pass


def active_mode(general, group):
    if general.get("controller_api_mode") == "selector":
        return "unknown", group
    return general.get("mode", "rule").lower(), group


def active_path(general, proxies, group):
    selection = proxies[group].get("now") if group else None
    leaf = resolve_leaf(proxies, selection) if group else "DIRECT"
    return (*active_mode(general, group), selection, leaf)


class _NoControllerRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Controller:
    def __init__(self, cfg):
        self.base = cfg["controller"].rstrip("/")
        self.secret = _controller_secret(cfg)
        self.timeout_ms = int(cfg["probe_timeout_ms"])
        self.probe_url = cfg["probe_url"]
        self.api_mode = cfg.get("controller_api_mode", "standard")
        self.expected_statuses = cfg.get("probe_expected_statuses", [405])

    def request(self, path, method="GET", body=None, timeout=5):
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = "Bearer " + self.secret
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        # Each worker has an independent opener. Local controller calls bypass system proxies.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoControllerRedirect())
        try:
            with opener.open(req, timeout=timeout) as res:
                content = res.read(4 * 1024 * 1024)
                return json.loads(content) if content else {}
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 405 and method == "GET" and path == "/configs":
                raise ConfigReadUnsupported() from None
            raise ControllerError("控制接口 HTTP " + str(exc.code)) from None
        except (OSError, ValueError, http.client.HTTPException):
            raise ControllerError("无法连接代理客户端控制接口") from None

    def snapshot(self, cfg, *, allow_direct=False):
        if cfg.get("controller_api_mode") == "selector":
            proxies = self.request("/proxies")["proxies"]
            group = cfg["group"]
            if proxies.get(group, {}).get("type") != "Selector":
                raise ControllerError("未找到可控制的代理组: " + group)
            # This is a capability marker, not a fabricated General response.
            return {"controller_api_mode": "selector"}, proxies, group
        general = dict(self.request("/configs"))
        # Only our explicit compatibility branch can establish this marker.
        general.pop("controller_api_mode", None)
        proxies = self.request("/proxies")["proxies"]
        mode = general.get("mode", "rule").lower()
        if mode == "direct":
            if allow_direct:
                return general, proxies, None
            raise ControllerError("代理客户端当前为直连模式，请切换回规则或全局模式")
        group = "GLOBAL" if mode == "global" else cfg["group"]
        if proxies.get(group, {}).get("type") != "Selector":
            raise ControllerError("未找到可控制的主代理组: " + group)
        return general, proxies, group

    def delay(self, name):
        fields = {"url": self.probe_url, "timeout": self.timeout_ms}
        if self.api_mode == "selector":
            fields["expected"] = ",".join(str(code) for code in self.expected_statuses)
        query = urllib.parse.urlencode(fields)
        try:
            result = self.request("/proxies/" + urllib.parse.quote(name, safe="") + "/delay?" + query,
                                  timeout=self.timeout_ms / 1000 * 2 + 3)
            value = result.get("delay")
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 else None
        except ControllerError:
            return None

    def select(self, group, name):
        self.request("/proxies/" + urllib.parse.quote(group, safe=""), "PUT", {"name": name})


def local_proxy_endpoint(general):
    port = general.get("mixed-port") or general.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ControllerError("代理客户端须开启本机 HTTP 或 mixed 代理端口，近舟才能验证实际线路")
    # Mihomo ignores bind-address when allow-lan is disabled.
    if not general.get("allow-lan"):
        return "127.0.0.1", port
    bind = general.get("bind-address", "*")
    if bind in ("*", ""):
        return "127.0.0.1", port
    if bind == "localhost":
        return "127.0.0.1", port
    if isinstance(bind, str) and bind.startswith("[") and bind.endswith("]"):
        bind = bind[1:-1]
    try:
        address = ipaddress.ip_address(bind) if isinstance(bind, str) else None
    except ValueError:
        address = None
    if address is not None:
        if address.is_unspecified:
            return ("::1" if address.version == 6 else "127.0.0.1"), port
        if address.is_loopback:
            return str(address), port
    raise ControllerError("HTTP 代理端口未绑定本机回环地址，请在客户端使用 127.0.0.1、[::1] 或通配地址")


def inspect_controller(fields, *, timeout=5, adaptive=False):
    """Read a local Clash-compatible client's groups without changing its state."""
    cfg = validate_config(fields)
    _check_client_environment(cfg)
    api = Controller(cfg)
    if adaptive:
        version = api.request("/version", timeout=timeout)
        if not isinstance(version, dict) or not isinstance(version.get("version"), str) or not version["version"]:
            raise ControllerError("接口未返回有效的 Clash／Mihomo 版本")
    selector_only = cfg["controller_api_mode"] == "selector"
    general = {"controller_api_mode": "selector"} if selector_only else api.request("/configs", timeout=timeout)
    proxy_response = api.request("/proxies", timeout=timeout)
    if not isinstance(general, dict) or not isinstance(proxy_response, dict):
        raise ControllerError("控制接口未返回有效的代理配置")
    proxies = proxy_response.get("proxies")
    if not isinstance(proxies, dict):
        raise ControllerError("控制接口未返回有效的代理组列表")
    for name, item in proxies.items():
        if (not isinstance(name, str) or not isinstance(item, dict)
                or ("all" in item and (not isinstance(item["all"], list)
                                      or any(not isinstance(member, str) for member in item["all"])))
                or (item.get("type") == "Selector" and "all" not in item)):
            raise ControllerError("控制接口返回的代理组或节点格式无效")
    mode = "unknown" if selector_only else general.get("mode", "rule")
    allowed_modes = ("rule", "global", "direct", "unknown") if selector_only else ("rule", "global", "direct")
    if not isinstance(mode, str) or mode.lower() not in allowed_modes:
        raise ControllerError("代理客户端返回了不支持的代理模式")
    mode = mode.lower()
    port = None if selector_only else local_proxy_endpoint(general)[1]
    groups = sorted(name for name, item in proxies.items()
                    if isinstance(item, dict) and item.get("type") == "Selector")
    active_group = None if mode == "direct" else ("GLOBAL" if mode == "global" else cfg["group"])
    if active_group not in groups:
        active_group = None
    chains = []
    target_host = urllib.parse.urlsplit(cfg["probe_url"]).hostname
    if adaptive and active_group is None and mode == "rule":
        try:
            connections = api.request("/connections", timeout=timeout)
            for connection in connections.get("connections", []):
                if not isinstance(connection, dict):
                    continue
                metadata = connection.get("metadata", {})
                if isinstance(metadata, dict) and metadata.get("host", "").lower().rstrip(".") == target_host:
                    chain = connection.get("chains", [])
                    if isinstance(chain, list) and all(isinstance(name, str) for name in chain):
                        chains.append(chain)
        except (ControllerError, AttributeError, TypeError):
            pass  # Active connection evidence is optional; ambiguity stays a user choice.
    recommendation = client_detection.recommend_group(proxies, cfg["group"], connection_chains=chains,
                                                      target_host=target_host, mode=mode)
    group = active_group or recommendation["recommended_group"]
    regions = client_detection.nearby_regions(flatten_candidates(proxies, group), node_region) if group else []
    return {"mode": mode, "groups": groups, "active_group": active_group, "proxy_port": port,
            **recommendation, "nearby_regions": regions,
            **({"controller_api_mode": "selector", "verification_scope": "selector"} if selector_only else {})}


def discover_clients(fields):
    """Discover compatible local endpoints using GET requests only."""
    return client_detection.discover_clients(validate_config(fields), inspect_controller)


class _ProbeCancelled(Exception):
    pass


class _ProbeBudget:
    def __init__(self, cancel):
        self.cancel = cancel
        self.deadline = 0

    def remaining(self):
        if self.cancel and self.cancel():
            raise _ProbeCancelled()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("线路验证请求超时")
        return remaining


class _ProbeReader(io.RawIOBase):
    def __init__(self, source, sock, budget):
        self.source, self.sock, self.budget = source, sock, budget

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(self.budget.remaining())
        count = self.source.readinto1(buffer)
        self.budget.remaining()
        return count

    def close(self):
        try:
            self.source.close()
        finally:
            super().close()


class ProbeHTTPResponse(http.client.HTTPResponse):
    def __init__(self, sock, *args, budget, **kwargs):
        super().__init__(sock, *args, **kwargs)
        self.fp = io.BufferedReader(_ProbeReader(self.fp, sock, budget))

    def _read_and_discard_trailer(self):
        # The stdlib tolerates EOF here; a health probe must require the
        # terminating blank line before declaring a chunked response complete.
        while True:
            line = self.fp.readline(http.client._MAXLINE + 1)
            if len(line) > http.client._MAXLINE:
                raise http.client.LineTooLong("trailer line")
            if not line:
                raise http.client.IncompleteRead(b"")
            if line in (b"\r\n", b"\n"):
                return

    def _read_status(self):
        # http.client skips only 100; other informational responses also
        # precede the final result. 101 switches protocols and must stop here.
        for _ in range(100):
            version, status, reason = super()._read_status()
            if not (100 <= status < 200) or status == 101:
                return version, status, reason
            http.client._read_headers(self.fp)
        raise http.client.HTTPException("got more than 100 interim responses")


def verify_current_path(general, idle_seconds=0, cancel=None, *, probe_url=PROBE_URL, expected_statuses=(405,)):
    """Verify actual HTTP status over the active proxy, optionally reusing one TLS socket.

    The default Codex POST-only endpoint returns 405 to GET; custom targets
    use their configured status codes. Redirects are not followed. This checks
    the connection, not account access or complete model generation.
    """
    try:
        host, port = local_proxy_endpoint(general)
    except ControllerError as exc:
        return {"ok": False, "reason": str(exc)}
    target = parse_probe_url(probe_url)
    if target.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=PROBE_REQUEST_TIMEOUT, context=ssl.create_default_context())
        conn.set_tunnel(target.hostname, target.port or 443)
        request_path = urllib.parse.urlunsplit(("", "", target.path or "/", target.query, ""))
    else:
        conn = http.client.HTTPConnection(host, port, timeout=PROBE_REQUEST_TIMEOUT)
        request_path = probe_url
    budget = _ProbeBudget(cancel)
    original_connect, original_tunnel = conn.connect, conn._tunnel

    def refresh_timeout():
        conn.timeout = budget.remaining()
        if conn.sock is not None:
            conn.sock.settimeout(conn.timeout)

    def connect():
        conn.timeout = budget.remaining()
        original_connect()
        refresh_timeout()

    def tunnel():
        refresh_timeout()
        original_tunnel()
        # The stdlib TLS handshake starts after CONNECT returns.
        refresh_timeout()

    conn.connect, conn._tunnel = connect, tunnel
    conn.response_class = partial(ProbeHTTPResponse, budget=budget)
    headers = {"Accept": "*/*", "User-Agent": "NearRoute/1.0", "Connection": "keep-alive"}
    if target.scheme == "http":
        headers["Proxy-Connection"] = "keep-alive"
    delays, sock, response = [], None, None
    try:
        for index in range(3 if idle_seconds else 1):
            if index:
                deadline = time.monotonic() + idle_seconds
                while time.monotonic() < deadline:
                    if cancel and cancel():
                        return {"ok": False, "reason": "验证已取消", "cancelled": True}
                    time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            budget.deadline = time.monotonic() + PROBE_REQUEST_TIMEOUT
            started = time.perf_counter()
            refresh_timeout()
            conn.request("GET", request_path, headers=headers)
            response = conn.getresponse()
            delays.append(round((time.perf_counter() - started) * 1000, 1))
            if response.status not in expected_statuses:
                return {"ok": False, "reason": "测试链接返回非预期 HTTP " + str(response.status), "http_status": response.status}
            body = response.read(65537)
            budget.remaining()
            if len(body) > 65536:
                return {"ok": False, "reason": "测试响应超过 64 KiB，请选择轻量健康检查链接"}
            if response.length is not None and response.length > 0:
                raise http.client.IncompleteRead(body, response.length)
            if idle_seconds:
                if conn.sock is None or (sock is not None and conn.sock is not sock):
                    return {"ok": False, "reason": "连接未能持续复用"}
                sock = conn.sock
        return {"ok": True, "http_status": response.status, "request_ms": delays,
                "same_connection": bool(idle_seconds),
                "same_tls_connection": bool(idle_seconds) and target.scheme == "https", "idle_test_seconds": idle_seconds * 2}
    except _ProbeCancelled:
        return {"ok": False, "reason": "验证已取消", "cancelled": True}
    except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
        if cancel and cancel():
            return {"ok": False, "reason": "验证已取消", "cancelled": True}
        return {"ok": False, "reason": "线路请求失败 (" + type(exc).__name__ + ")"}
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            conn.close()


MUTEX_NAME = "Local\\CodexNodeOptimizer_" + hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:16]


class InstanceLock:
    def __init__(self):
        self.handle = None
        self.kernel = None
    def acquire(self):
        if os.name != "nt":
            raise RuntimeError("后台运行目前仅支持 Windows")
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        ctypes.set_last_error(0)
        self.handle = self.kernel.CreateMutexW(None, False, MUTEX_NAME)
        if not self.handle:
            raise OSError("无法创建单实例锁")
        if ctypes.get_last_error() == 183:
            self.close()
            return False
        return True
    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def event(kind, **details):
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "events.jsonl"
    if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
        os.replace(path, STATE / "events.previous.jsonl")
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"at": iso(), "event": kind} | details, ensure_ascii=False, allow_nan=False) + "\n")


class LogSignals:
    """Counts recent transport errors without retaining log bodies."""
    def __init__(self, path):
        self.enabled = bool(path)
        self.path = Path(path)
        self.last_id = None
        self.last_new_error_at = None
        self.recent = collections.deque()
        self._unpaired = {}

    @staticmethod
    def _responses_request(body):
        method = re.search(r'\bmethod\s*=\s*[\"\']?([A-Za-z]+)\b', body)
        if method and method.group(1).upper() != "POST":
            return False
        failed_url = re.search(r'error sending request for url \((https?://[^\s)]+)\)', body, re.I)
        structured_url = re.search(r'\burl\s*=\s*[\"\']?(https?://[^\s\"\']+)', body, re.I)
        match = failed_url or structured_url
        if not match:
            return False
        try:
            url = urllib.parse.urlsplit(match.group(1).rstrip(',)}]'))
            return (url.scheme == "https" and url.hostname == "chatgpt.com"
                    and url.path == "/backend-api/codex/responses")
        except ValueError:
            return False

    @staticmethod
    def _error_key(process, thread, body, kind):
        turn = re.search(r'\bturn_id\s*=\s*[\"\']?([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\b', body)
        retry = re.search(r'\bretries\s*=\s*(\d+)\b', body)
        if process and thread and turn and retry:
            return process, thread, turn.group(1), int(retry.group(1)), kind
        return None

    @staticmethod
    def _retry_stream_kind(body):
        field = re.search(r'\bsampling_error\s*=\s*', body)
        if not field or "stream disconnected - retrying sampling request (" not in body[:field.start()]:
            return None
        error = re.split(r'\s+[A-Za-z_][A-Za-z_0-9]*\s*=', body[field.end():], maxsplit=1)[0]
        error = error.strip().strip('\"\'')
        prefix = "stream disconnected before completion: "
        if error == prefix + "tls handshake eof":
            return "tls"
        if error == prefix + "websocket closed by server before response.completed":
            return "websocket"
        if error.startswith(prefix + "IO error: ") and error.endswith("(os error 10054)"):
            return "reset"
        return None

    def poll(self):
        self.last_new_error_at = None
        if not self.enabled:
            return 0
        now = time.time()
        cutoff = now - 180
        self.recent = collections.deque(stamp for stamp in self.recent if stamp >= cutoff)
        self._unpaired = {key: [item for item in pending if item[0] >= cutoff]
                          for key, pending in self._unpaired.items() if any(item[0] >= cutoff for item in pending)}
        db = None
        try:
            uri = self.path.absolute().as_uri() + "?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=0.15)
            highest = db.execute("SELECT MAX(id) FROM logs").fetchone()[0] or 0
            if self.last_id is not None and highest > self.last_id:
                rows = db.execute("""SELECT ts,process_uuid,thread_id,target,feedback_log_body FROM logs
                    WHERE id>? AND id<=? AND
                    ((target LIKE '%responses_retry%' AND
                      (feedback_log_body LIKE '%error sending request%' OR feedback_log_body LIKE '%error decoding response body%'))
                     OR (target='codex_http_client::client' AND feedback_log_body LIKE '%error sending request%')
                     OR (target='codex_core::responses_retry' AND
                         (feedback_log_body LIKE '%websocket closed by server before response.completed%'
                          OR feedback_log_body LIKE '%(os error 10054)%'
                          OR feedback_log_body LIKE '%tls handshake eof%')))
                    ORDER BY id""", (self.last_id, highest)).fetchall()
                for stamp, process, thread, target, raw in rows:
                    body = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', raw or '')
                    source = "client" if target == "codex_http_client::client" else "retry"
                    if source == "client" and not self._responses_request(body):
                        continue
                    lower = body.lower()
                    kind = ("decode" if "error decoding response body" in lower else
                            "send" if "error sending request" in lower else
                            self._retry_stream_kind(body) if target == "codex_core::responses_retry" else None)
                    if kind is None:
                        continue
                    stamp = float(stamp)
                    key = self._error_key(process, thread, body, kind)
                    if key is not None:
                        pending = self._unpaired.setdefault(key, [])
                        duplicate = next((i for i, item in enumerate(pending)
                                          if item[1] != source and abs(stamp - item[0]) <= 2), None)
                        if duplicate is not None:
                            pending.pop(duplicate)
                            if not pending:
                                del self._unpaired[key]
                            continue
                        pending.append((stamp, source))
                    if stamp >= cutoff:
                        self.recent.append(stamp)
                    self.last_new_error_at = max(stamp, self.last_new_error_at or stamp)
            self.last_id = highest
        except (OSError, sqlite3.Error):
            pass
        finally:
            if db is not None:
                db.close()
        return len(self.recent)


def codex_connection_summary(path, *, now=None):
    """Return an opt-in, read-only summary of recent trusted Codex log records.

    Counts are observations, with paired client/retry records counted once.
    When truncated, the count covers only the records read, not the full window.
    No log text, request identifiers, or paths leave this function.
    """
    result = {"state": "ok", "window_seconds": 7200, "error_count": 0,
              "errors": [], "transports": [], "truncated": False}
    if not path:
        result["state"] = "not_configured"
        return result
    db = None
    try:
        path = Path(path)
        if not path.exists():
            result["state"] = "missing"
            return result
        current = time.time() if now is None else (now.timestamp() if isinstance(now, datetime) else float(now))
        if not math.isfinite(current):
            raise ValueError
        db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.15)
        deadline = time.monotonic() + 0.75
        db.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
        required = {"id", "ts", "ts_nanos", "level", "target", "feedback_log_body", "process_uuid", "thread_id"}
        columns = {row[1] for row in db.execute("PRAGMA table_info(logs)")}
        index = db.execute('PRAGMA index_info("idx_logs_ts")').fetchall()
        if not required <= columns or not index or index[0][2] != "ts":
            result["state"] = "unsupported"
            return result
        targets = ("codex_core::responses_retry", "codex_http_client::client",
                   "codex_http_client::request", "codex_http_client::feedback_tags")
        rows = db.execute("""SELECT ts,ts_nanos,level,target,feedback_log_body,process_uuid,thread_id
            FROM logs INDEXED BY idx_logs_ts WHERE ts>=? AND ts<=? AND target IN (?,?,?,?)
            ORDER BY ts DESC,ts_nanos DESC,id DESC LIMIT ?""",
            (math.floor(current - 7200), current, *targets, 5001)).fetchall()
        result["truncated"] = len(rows) > 5000
        observations = []
        unpaired = {}
        transports = {}
        for stamp, nanos, level, target, raw, process, thread in rows[:5000]:
            if (type(stamp) is not int or type(nanos) is not int or not 0 <= nanos < 1000000000
                    or not 0 <= current - stamp - nanos / 1000000000 <= 7200 or not isinstance(raw, str)):
                continue
            body = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', raw)
            if target in targets[2:]:
                for match in re.finditer(r'\btransport\s*=\s*[\"\']?(responses_http|responses_websocket)(?=[\"\']?(?:\s|[,}\]]|$))', body):
                    transports.setdefault(match.group(1), (stamp, nanos))
                continue
            if not isinstance(level, str) or level.upper() not in ("WARN", "ERROR"):
                continue
            source = "client" if target == targets[1] else "retry"
            lower = body.lower()
            kind = None
            if source == "client":
                if "error sending request" in lower and LogSignals._responses_request(body):
                    kind = "decode" if "error decoding response body" in lower else "send"
            else:
                kind = LogSignals._retry_stream_kind(body)
                field = re.search(r'\b(sampling_error|compact_error)\s*=\s*', body)
                error = body.strip()
                if field:
                    template = ("stream disconnected - retrying sampling request (" if field.group(1) == "sampling_error"
                                else "remote compaction v2 stream failed; retrying request after delay")
                    if template not in body[:field.start()]:
                        continue
                    error = re.split(r'\s+[A-Za-z_][A-Za-z_0-9]*\s*=', body[field.end():], maxsplit=1)[0].strip().strip('\"\'')
                    prefix = "stream disconnected before completion: Transport error: network error: "
                    if error.startswith(prefix):
                        error = error[len(prefix):]
                for phrase, candidate in (("error decoding response body", "decode"), ("error sending request", "send")):
                    if (error.startswith(phrase) if field else
                            error == phrase or (LogSignals._responses_request(body) and error.endswith(phrase))):
                        kind = candidate
                        break
            if kind is None:
                continue
            at = datetime.fromtimestamp(stamp, TZ).replace(microsecond=nanos // 1000).isoformat(timespec="milliseconds")
            record = {"at": at, "kind": kind, "retry": None, "max_retries": None, "retry_wait_ms": None}
            for field, output in (("retries", "retry"), ("max_retries", "max_retries")):
                match = re.search(r'\b' + field + r'\s*=\s*(\d{1,6})(?!\d)', body)
                if match:
                    record[output] = int(match.group(1))
            delay = re.search(r'stream disconnected - retrying sampling request \((\d{1,6})/(\d{1,6}) in (\d{1,9}(?:\.\d{1,3})?)ms\)', body)
            if delay:
                record["retry"] = record["retry"] if record["retry"] is not None else int(delay.group(1))
                record["max_retries"] = record["max_retries"] if record["max_retries"] is not None else int(delay.group(2))
                record["retry_wait_ms"] = float(delay.group(3))
            key = LogSignals._error_key(process, thread, body, kind)
            if key is not None:
                pending = unpaired.setdefault(key, [])
                paired = next((i for i, item in enumerate(pending) if item[1] != source and abs(stamp - item[0]) <= 2), None)
                if paired is not None:
                    _, _, previous = pending.pop(paired)
                    for field in ("retry", "max_retries", "retry_wait_ms"):
                        if previous[field] is None:
                            previous[field] = record[field]
                    continue
                pending.append((stamp, source, record))
            observations.append(record)
        result["error_count"] = len(observations)
        result["errors"] = observations[:20]
        result["transports"] = [{"transport": transport,
                                 "last_seen": datetime.fromtimestamp(stamp, TZ).replace(microsecond=nanos // 1000).isoformat(timespec="milliseconds")}
                                for transport, (stamp, nanos) in transports.items()]
        result["truncated"] = result["truncated"] or len(observations) > 20
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", 0) or 0
        # Python 3.10 does not expose sqlite_errorcode on SQLite exceptions.
        transient = code & 255 in (5, 6, 9) or (not code and str(exc).split(":", 1)[0] in (
            "database is locked", "database table is locked", "database schema is locked", "interrupted"))
        result["state"] = "busy" if transient else "unreadable"
    except (OSError, TypeError, ValueError, OverflowError):
        result["state"] = "unreadable"
    finally:
        if db is not None:
            try:
                db.set_progress_handler(None, 0)
            finally:
                db.close()
    return result


class Optimizer:
    def __init__(self, dry_run=False):
        STATE.mkdir(parents=True, exist_ok=True)
        self.cfg = load_config()
        self.api = Controller(self.cfg)
        self.state_context = state_context(self.cfg)
        self.history = read_json(STATE / "history.json", {})
        self.record = current_selection(self.cfg)
        self.quarantine = read_json(STATE / "quarantine.json", {})
        self.signals = LogSignals(self.cfg["codex_log_db"])
        self.status = read_json(STATE / "status.json", {})
        if self.status.get("controller_context") != list(self.state_context):
            self.status = {}
        self.status.update({"pid": os.getpid(), "started_at": iso(), "runtime_version": RUNTIME_VERSION,
                            "enabled": self.cfg["enabled"], "phase": "starting", "message": "正在连接代理客户端",
                            "last_error": None, "controller_available": False, "health": None})
        self._published_status = {}
        self._last_status_at = 0.0
        self.failures = 0
        self.dry_run = dry_run
        self.next_scan = 0.0
        self.last_scan = 0.0
        self.manual_until = self.record.get("manual_until", 0.0)
        self.status["manual_until"] = iso(self.manual_until) if time.time() < self.manual_until else None
        self.manual_revision = 0
        self.scan_display_names = []
        self.last_health = 0.0
        self.last_signal_poll = 0.0
        self.recovery_until = 0.0
        self.recovery_scan_pending = False
        self.observed_selection = None
        self.tentative = False
        self.last_client_detection = -math.inf
        self.sync_probe_history()
        self.prune_probe_history()
        self.write_status()

    def stopping(self):
        return (STATE / "stop.request").exists()

    def write_status(self, **updates):
        if "nodes" in updates:
            # Only newly computed rows can establish their probe provenance.
            if self.status.get("nodes_probe_identity") != self.history_probe:
                self.status["last_scan_at"] = None
            self.status["nodes_probe_identity"] = self.history_probe
        self.status.update(updates)
        self.status["controller_api_mode"] = self.cfg.get("controller_api_mode", "standard")
        self.status["verification_scope"] = "selector" if self.cfg.get("controller_api_mode") == "selector" else "path"
        self.status["runtime_version"] = RUNTIME_VERSION
        self.status["controller_context"] = list(self.state_context)
        self.status["updated_at"] = iso()
        self.status.pop("storage_error", None)
        try:
            atomic_json(STATE / "status.json", self.status)
        except OSError as error:
            # The UI snapshot can be retried on the next update. Durable selection
            # journals use atomic_json directly and must still fail on write errors.
            self.status["storage_error"] = str(error)
            return False
        self._published_status = self.status.copy()
        self._last_status_at = time.time()
        return True

    def write_idle_status(self, **updates):
        # Coalesce idle heartbeats only; changed state is still published at once.
        self.status.update(updates)
        if self.status != self._published_status or time.time() - self._last_status_at >= 15:
            self.write_status()

    def reload_config(self):
        previous = self.cfg
        self.cfg = load_config()
        # A selection transaction must finish on the controller it started on.
        if not self.tentative:
            self.api = Controller(self.cfg)
            self.sync_runtime_context()
        self.status["enabled"] = self.cfg["enabled"]
        if (selection_policy(self.cfg) != selection_policy(previous)
                or any(self.cfg[key] != previous[key]
                       for key in ("scan_interval_seconds", "switch_cooldown_seconds"))):
            self.next_scan = 0
        if self.cfg["codex_log_db"] != previous["codex_log_db"]:
            self.signals = LogSignals(self.cfg["codex_log_db"])

    def adapt_client(self):
        """Recover an unavailable client/group while this helper owns the writer lock."""
        cfg = dict(self.cfg)
        if (self.dry_run or self.tentative or self.stopping()
                or not (cfg["auto_detect_client"] or cfg["auto_select_group"])
                or time.monotonic() - self.last_client_detection < 30
                or (STATE / "pending-selection.json").exists()
                or (STATE / "select.request.json").exists()):
            return False
        self.last_client_detection = time.monotonic()
        if cfg["auto_detect_client"]:
            result = discover_clients(cfg)
        else:
            info = inspect_controller(cfg, timeout=0.6, adaptive=True)
            result = {"candidates": [{"controller": cfg["controller"], "verified": True, **info}],
                      "selected_controller": cfg["controller"], "message": ""}
        endpoint = result.get("selected_controller")
        info = next((item for item in result["candidates"]
                     if item["controller"] == endpoint and item.get("verified")), None)
        self.status["client_detection_message"] = result.get("message", "")
        if info is None or info.get("mode") == "direct":
            return False
        group = cfg["group"]
        if info.get("mode") != "global" and group not in info["groups"]:
            if not cfg["auto_select_group"] or not info.get("recommended_group"):
                self.status["client_detection_message"] = "无法唯一判断代理组，请在连接设置中选择"
                return False
            group = info["recommended_group"]
        fields = {"controller": endpoint, "group": group,
                  "controller_secret_env": cfg["controller_secret_env"] if endpoint == cfg["controller"] else "",
                  "controller_api_mode": info.get("controller_api_mode", "standard")}
        if state_context(cfg | fields) == state_context(cfg):
            self.status["client_detection_message"] = ""
            return False
        # Never move an unfinished rollback or a queued user choice to another client.
        with _manual_request_lock(), config_update_lock():
            current = load_config()
            if (current != cfg or self.stopping() or self.tentative
                    or (STATE / "pending-selection.json").exists()
                    or (STATE / "select.request.json").exists()):
                return False
            candidate = validate_config(current | fields)
            atomic_json(CONFIG, candidate)
        self.reload_config()
        self.write_status(phase="starting" if self.cfg["enabled"] else "paused", controller_available=False,
                          client_detection_message="已自动应用唯一可靠的代理连接", last_error=None,
                          message="已识别代理客户端，正在读取当前线路")
        event("client_detected", group_changed=cfg["group"] != group,
              controller_changed=cfg["controller"] != endpoint)
        return True

    def sync_runtime_context(self):
        context = state_context(self.cfg)
        if context != self.state_context:
            self.record = current_selection(self.cfg)
            self.manual_until = self.record.get("manual_until", 0.0)
            self.observed_selection = None
            self.failures = 0
            self.scan_display_names = []
            self.next_scan = self.last_scan = self.last_health = self.last_signal_poll = 0
            self.recovery_until = 0.0
            self.recovery_scan_pending = False
            self.status.update(selected=None, health=None, nodes=[],
                               manual_until=iso(self.manual_until) if time.time() < self.manual_until else None,
                               last_switch_at=None, last_health_at=None, consecutive_health_failures=0,
                               transport_recovery_until=None)
            self.state_context = context
        self.sync_probe_history()

    def sync_probe_history(self):
        fingerprint = probe_identity(self.cfg)
        if getattr(self, "history_probe", None) == fingerprint:
            return
        saved = read_json(STATE / "history-probe.json", {})
        if saved.get("fingerprint") != fingerprint:
            self.history.clear()
            self.quarantine.clear()
            self.failures = 0
            self.status.update(nodes=[], health=None, last_health_at=None)
            atomic_json(STATE / "history.json", self.history)
            atomic_json(STATE / "quarantine.json", self.quarantine)
        atomic_json(STATE / "history-probe.json", {"fingerprint": fingerprint})
        self.history_probe = fingerprint

    def verify_path(self, general, idle_seconds=0, cancel=None):
        if cancel is None:
            last_pause_check = None

            def cancel():
                nonlocal last_pause_check
                if self.stopping() or (STATE / "select.request.json").exists():
                    return True
                now = time.monotonic()
                if last_pause_check is None or now - last_pause_check >= 1:
                    last_pause_check = now
                    return not load_config()["enabled"]
                return False

        if self.cfg.get("controller_api_mode") == "selector" and general.get("controller_api_mode") == "selector":
            # Restricted controllers cannot prove the application route or a
            # reusable HTTPS stream. Only retest the selected independent leaf.
            result = {"verification_scope": "selector", "request_ms": []}
            _, proxies, group = self.api.snapshot(self.cfg)
            leaf = resolve_leaf(proxies, proxies[group].get("now"))
            if leaf not in flatten_candidates(proxies, group):
                return result | {"ok": False, "reason": "所选代理组当前未使用可测速的独立节点"}
            for check in range(3 if idle_seconds else 1):
                if check:
                    deadline = time.monotonic() + idle_seconds
                    while time.monotonic() < deadline:
                        if cancel():
                            return result | {"ok": False, "cancelled": True, "reason": "节点复测已取消"}
                        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                if cancel():
                    return result | {"ok": False, "cancelled": True, "reason": "节点复测已取消"}
                delay = self.api.delay(leaf)
                if cancel():
                    return result | {"ok": False, "cancelled": True, "reason": "节点复测已取消"}
                if not isinstance(delay, (int, float)) or isinstance(delay, bool) or not math.isfinite(delay) or delay <= 0:
                    return result | {"ok": False, "reason": "所选代理组节点探测失败；实际出站路径未验证"}
                result["request_ms"].append(delay)
            return result | {"ok": True, "reason": "所选代理组节点探测通过；实际出站路径未验证"}

        return verify_current_path(general, idle_seconds, cancel,
                                   probe_url=self.cfg["probe_url"], expected_statuses=self.cfg["probe_expected_statuses"])

    def poll_transport_signals(self, *, force=False):
        now = time.time()
        if not force and now - self.last_signal_poll < 5:
            return
        self.last_signal_poll = now
        errors = self.signals.poll()
        self.status["network_errors_recent"] = errors
        new_error = self.signals.last_new_error_at
        deadline = min(now, new_error) + 60 if new_error is not None else 0
        if now < deadline:
            self.recovery_until = max(self.recovery_until, deadline)
            self.recovery_scan_pending = True
        self.status["transport_recovery_until"] = iso(self.recovery_until) if now < self.recovery_until else None

    def transport_recovery_active(self, *, force=False):
        self.poll_transport_signals(force=force)
        return self.cfg["enabled"] and self.failures < 2 and time.time() < self.recovery_until

    def defer_recovery_scan(self, *, force=False):
        if not self.transport_recovery_active(force=force):
            return False
        self.next_scan = max(self.next_scan, self.recovery_until)
        self.recovery_scan_pending = True
        self.write_status(phase="monitoring", next_scan_at=iso(self.recovery_until),
                          message="Codex 正在恢复连接，暂缓后台测速和自动换点")
        return True

    def publish_active_path(self, general, proxies, group, **updates):
        selected = resolve_leaf(proxies, proxies[group].get("now")) if group else "DIRECT"
        self.write_status(mode=active_mode(general, group)[0], group=group, selected=selected,
                          controller_available=True, last_error=None, **updates)

    def require_proxy_endpoint(self, general, proxies, group, expected=None):
        if self.cfg.get("controller_api_mode") == "selector" and general.get("controller_api_mode") == "selector":
            # Explicit group-only operation has no local proxy/route evidence.
            return None
        message = None
        try:
            endpoint = local_proxy_endpoint(general)
        except ControllerError as error:
            endpoint = None
            message = str(error)
        if message is None and (expected is None or endpoint == expected):
            return endpoint
        self.failures = 0
        self.last_health = time.time() if endpoint is None else 0
        self.next_scan = self.last_health + self.cfg["health_interval_seconds"] if endpoint is None else 0
        message = message or "代理监听地址或端口已改变，已丢弃旧连接验证，等待重新检查"
        self.publish_active_path(
            general, proxies, group, health=None, verification=None, last_health_at=None,
            consecutive_health_failures=0, next_scan_at=iso(self.next_scan) if self.next_scan else iso(),
            phase="waiting" if endpoint is None else ("monitoring" if self.cfg["enabled"] else "paused"),
            message=message)
        raise PathChanged(message)

    def require_active_mode(self, expected, expected_path=None, *, expected_endpoint=None):
        snapshot = self.api.snapshot(self.cfg, allow_direct=True)
        general, proxies, group = snapshot
        mode_changed = active_mode(general, group) != expected
        path = active_path(general, proxies, group)
        if mode_changed or (expected_path is not None and path != expected_path):
            self.next_scan = 0
            self.observe_selection(path)
            self.failures = 0
            if mode_changed:
                self.scan_display_names = []
            message = ("代理模式已改变，本轮选点已取消，将按当前模式刷新" if mode_changed
                       else "检测到手动选点，本轮选点已取消，将按当前线路刷新")
            self.publish_active_path(general, proxies, group, health=None, last_health_at=None,
                                     phase="monitoring" if self.cfg["enabled"] else "paused",
                                     consecutive_health_failures=0, next_scan_at=iso(), message=message,
                                     **({"nodes": []} if mode_changed else {}))
            raise (ModeChanged if mode_changed else PathChanged)(message)
        if expected_endpoint is not None:
            self.require_proxy_endpoint(general, proxies, group, expected_endpoint)
        return snapshot

    def observe_paused_path(self):
        self.last_health = time.time()
        try:
            general, proxies, group = self.api.snapshot(self.cfg, allow_direct=True)
            self.publish_active_path(general, proxies, group, phase="paused", health=None,
                                     message="已暂停自动测速和切换")
        except (ControllerError, ValueError, OSError) as error:
            if isinstance(error, ControllerError):
                try:
                    if self.adapt_client():
                        return
                except (ControllerError, ValueError, OSError):
                    self.status["client_detection_message"] = "自动识别暂未成功，将稍后重试"
            detail = self.status.get("client_detection_message", "")
            self.write_status(phase="paused", controller_available=False, last_error=str(error),
                              message="已暂停；无法读取客户端当前状态" + ("；" + detail if detail else ""))

    def refresh_cancelled_mode(self, message="代理模式已改变，本轮选点已取消", *, clear_nodes=True):
        # GLOBAL can select the original rule group, so rollback may also change
        # its effective leaf. Publish a new snapshot after finishing recovery.
        self.next_scan = 0
        try:
            general, proxies, group = self.api.snapshot(self.cfg, allow_direct=True)
            observation = active_path(general, proxies, group)
            if observation != self.observed_selection:
                path = STATE / "external-manual.json"
                data = read_json(path, {})
                records = data.get("records", []) if data.get("schema_version") == 1 else []
                saved = next((item for item in records if item.get("context") == list(self.state_context)), {})
                if (saved.get("path") == list(self.observed_selection or ())
                        and saved.get("selection_transaction_id") == self.record.get("transaction_id")
                        and time.time() < saved.get("manual_until", 0)):
                    # Rollback changed this leaf, not the user's grace deadline.
                    saved["path"] = list(observation)
                    atomic_json(path, data)
                    self.observed_selection = observation
            self.publish_active_path(general, proxies, group, health=None,
                                     phase="monitoring" if self.cfg["enabled"] else "paused",
                                     next_scan_at=iso(), message=message,
                                     **({"nodes": []} if clear_nodes else {}))
        except (ControllerError, ValueError, OSError) as error:
            self.write_status(controller_available=False, selected=None, group=None, mode=None,
                              health=None, last_error=str(error),
                              phase="waiting" if self.cfg["enabled"] else "paused",
                              message="线路已改变，暂时无法确认当前线路")

    def process_manual_selection(self):
        if self.dry_run:
            return False
        path = STATE / "select.request.json"
        if not path.exists():
            return False
        with _manual_request_lock():
            if not path.exists():
                return False
            request = read_json(path, {})
            path.unlink()
        result = {"request_id": request.get("request_id"), "name": request.get("name"), "ok": False}
        try:
            self.reload_config()
            if not same_controller(request, self.cfg) or request.get("group") != self.cfg["group"]:
                raise ValueError("代理设置已改变，请重新选择节点")
            if request.get("controller_api_mode", "standard") != self.cfg.get("controller_api_mode", "standard"):
                raise ValueError("控制接口模式已改变，请重新选择节点")
            self.recover_pending()
            general, proxies, group = self.api.snapshot(self.cfg)
            mode = active_mode(general, group)
            if request.get("expected_mode") is not None and request["expected_mode"] != list(mode):
                raise ModeChanged("代理模式或主代理组已改变，请刷新后重新选择节点")
            name = request.get("name")
            if not isinstance(name, str) or name not in proxies[group].get("all", []) or name not in flatten_candidates(proxies, group):
                raise ValueError("该节点已失效或不能由当前主代理组直接选择，请刷新后重试")
            previous = proxies[group].get("now")
            self.protect_original(group, previous)
            transaction_id = uuid.uuid4().hex
            atomic_json(STATE / "pending-selection.json", {
                "group": group, "previous": previous, "candidate": name,
                "at": iso(), "transaction_id": transaction_id,
                "controller": self.cfg["controller"], "controller_secret_env": self.cfg["controller_secret_env"],
            })
            if previous != name:
                self.api.select(group, name)
            _, latest, _ = self.require_active_mode(mode)
            if latest.get(group, {}).get("now") != name:
                raise ControllerError("客户端未确认该节点，请重新选择")
            now = time.time()
            manual_until = now + self.cfg["manual_selection_grace_seconds"]
            record = {**controller_identity(self.cfg), "configured_group": self.cfg["group"],
                      "group": group, "name": name, "last_switch_at": now,
                      "manual_until": manual_until, "source": "manual", "transaction_id": transaction_id}
            save_owned_record(STATE / "selection.json", record, latest=True)
            self.record = record
            self.manual_until = manual_until
            self.recover_pending()
            self.manual_revision += 1
            self.observed_selection = active_path(general, latest, group)
            self.failures = 0
            self.next_scan = now + self.cfg["scan_interval_seconds"]
            result.update(ok=True, message="已手动切换；暂时尊重此选择")
            self.write_status(selected=name, mode=mode[0], group=group, controller_available=True, last_error=None,
                              health=None, last_health_at=None, verification=None, consecutive_health_failures=0,
                              phase="monitoring" if self.cfg["enabled"] else "paused",
                              last_switch_at=iso(now), manual_until=iso(self.manual_until),
                              next_scan_at=iso(self.next_scan), manual_selection_result=result, message=result["message"])
            event("manual_selection", group=group, previous=previous, selected=name)
            return True
        except (ControllerError, ValueError, OSError) as error:
            if isinstance(error, ModeChanged):
                try:
                    self.recover_pending()
                except (ControllerError, OSError) as recovery_error:
                    error = recovery_error
                finally:
                    self.refresh_cancelled_mode()
            result["message"] = str(error)
            self.write_status(manual_selection_result=result, message="手动切换失败：" + str(error))
            return False

    def prune_probe_history(self, *, force_history=False, force_quarantine=False):
        now = time.time()
        history = {}
        for name, samples in self.history.items():
            retained = [sample for sample in samples if now - sample["at"] <= self.cfg["history_seconds"]]
            if retained:
                history[name] = retained[-int(self.cfg["history_samples"]):]
        quarantine = {name: deadline for name, deadline in self.quarantine.items() if deadline > now}
        history_changed = history != self.history
        quarantine_changed = quarantine != self.quarantine
        self.history = history
        self.quarantine = quarantine
        if history_changed or force_history:
            atomic_json(STATE / "history.json", self.history)
        if quarantine_changed or force_quarantine:
            atomic_json(STATE / "quarantine.json", self.quarantine)

    def add_sample(self, name, delay):
        now = time.time()
        data = self.history.setdefault(name, [])
        data.append({"at": now, "delay": delay})
        self.history[name] = [x for x in data if now - x["at"] <= self.cfg["history_seconds"]][-int(self.cfg["history_samples"]):]

    def statistics(self, names):
        now = time.time()
        result = []
        for name in names:
            samples = [x["delay"] for x in self.history.get(name, []) if now - x["at"] <= self.cfg["history_seconds"]]
            row = summarize_samples(name, samples)
            if self.quarantine.get(name, 0) > now:
                row.update(eligible=False, reason="线路验证失败，暂时冷却")
            result.append(row)
        return sorted(result, key=lambda row: (not row["eligible"], row["score_ms"] if row["score_ms"] is not None else float("inf"), row["name"]))

    def measure_round(self, names, title, *, forced=False):
        policy = selection_policy(self.cfg)
        automatic = self.cfg["enabled"] and not forced
        results = {}
        if self.stopping() or self.process_manual_selection() or selection_policy(self.cfg) != policy:
            return results
        self.reload_config()
        last_config_check = time.monotonic()
        if selection_policy(self.cfg) != policy or (automatic and not self.cfg["enabled"]):
            return results
        if not forced and self.transport_recovery_active():
            return results
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=int(self.cfg["concurrency"]))
        pending = {executor.submit(self.api.delay, name): name for name in names}
        try:
            while pending:
                completed, _ = concurrent.futures.wait(pending, timeout=0.1,
                                                       return_when=concurrent.futures.FIRST_COMPLETED)
                if self.stopping():
                    break
                if self.process_manual_selection():
                    break
                now = time.monotonic()
                if now - last_config_check >= 1:
                    self.reload_config()
                    last_config_check = now
                if selection_policy(self.cfg) != policy or (automatic and not self.cfg["enabled"]):
                    break
                if not forced and self.transport_recovery_active():
                    break
                for future in completed:
                    name = pending.pop(future)
                    delay = future.result()
                    results[name] = delay
                    self.add_sample(name, delay)
                    if len(results) % 5 == 0 or len(results) == len(names):
                        self.write_status(phase="scanning", message=f"{title}：{len(results)}/{len(names)}", progress_done=len(results), progress_total=len(names),
                                          nodes=self.statistics(self.scan_display_names or names))
        finally:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
        self.prune_probe_history(force_history=True)
        return results

    def protect_original(self, group, current):
        path = STATE / "original-selection.json"
        data = read_json(path, {})
        if not any(same_controller(item, self.cfg) and item["group"] == group for item in owned_records(data)):
            save_owned_record(path, {**controller_identity(self.cfg), "group": group,
                                     "name": current, "saved_at": iso()})

    def rollback(self, group, candidate, original, api=None, *, pending=None):
        # Do not overwrite an intervening manual change in the client.
        api = api or self.api
        proxies = api.request("/proxies")["proxies"]
        current = proxies.get(group, {}).get("now")
        current_proxy = proxies.get(current, {})
        if current_proxy and (current != candidate or isinstance(current_proxy.get("all"), list)):
            return "superseded"
        if (proxies.get(group, {}).get("now") == candidate
                and candidate in flatten_candidates(proxies, candidate)):
            if original not in proxies.get(group, {}).get("all", []):
                raise ControllerError("原节点暂不可用，回滚记录已保留，请恢复节点后重试")
            api.select(group, original)
            confirmed = api.request("/proxies")["proxies"]
            if confirmed.get(group, {}).get("now") != original:
                raise ControllerError("客户端未确认回滚，回滚记录已保留，请重试")
            path = STATE / "pending-selection.json"
            if pending is not None and pending_record(read_json(path, {})) == pending:
                path.unlink(missing_ok=True)
            event("rollback", group=group, previous=candidate, restored=original)

    def observe_selection(self, observation, *, external_change=False):
        protection_path = STATE / "external-manual.json"
        context = list(self.state_context)
        if self.observed_selection is None and not external_change:
            data = read_json(protection_path, {})
            records = data.get("records", []) if data.get("schema_version") == 1 else []
            saved = next((item for item in records if item.get("context") == context), {})
            # Ownership stays in selection.json. A later tool selection supersedes
            # this grace even if the old external path subsequently reappears.
            if (saved.get("path") == list(observation)
                    and saved.get("selection_transaction_id") == self.record.get("transaction_id")
                    and time.time() < saved.get("manual_until", 0)):
                self.manual_until = saved["manual_until"]
                self.status["manual_until"] = iso(self.manual_until)
        elif external_change or observation != self.observed_selection:
            self.failures = 0
            self.next_scan = 0
            self.manual_until = time.time() + self.cfg["manual_selection_grace_seconds"]
            self.status.update(health=None, last_health_at=None, consecutive_health_failures=0,
                               manual_until=iso(self.manual_until), next_scan_at=iso())
            data = read_json(protection_path, {})
            records = data.get("records", []) if data.get("schema_version") == 1 else []
            saved = {"context": context, "path": list(observation), "manual_until": self.manual_until,
                     "selection_transaction_id": self.record.get("transaction_id")}
            atomic_json(protection_path, {"schema_version": 1,
                                         "records": [item for item in records if item.get("context") != context] + [saved]})
            event("external_selection_change", group=observation[1], selected=observation[3])
        self.observed_selection = observation

    def change_candidate(self, general, group, previous, candidate, *, expected_path=None):
        self.recover_pending()
        if self.defer_recovery_scan(force=True):
            return False
        policy = selection_policy(self.cfg)
        mode = active_mode(general, group)
        self.write_status(phase="verifying", message=("正在复测候选节点：" if mode[0] == "unknown" else "正在验证候选线路：") + candidate)
        self.tentative = True
        committed = False
        put_attempted = False
        try:
            self.reload_config()
            if (not self.cfg["enabled"] or self.stopping() or (STATE / "select.request.json").exists() or selection_policy(self.cfg) != policy
                    or not scan_candidates([candidate], self.cfg)):
                return False
            general, latest, _ = self.require_active_mode(mode, expected_path)
            if latest.get(group, {}).get("now") != previous:
                return False
            if (candidate not in latest[group].get("all", [])
                    or candidate not in flatten_candidates(latest, group)):
                return None  # Removed before PUT; no candidate validation was attempted.
            endpoint = self.require_proxy_endpoint(general, latest, group)
            if self.defer_recovery_scan(force=True):
                return False
            self.protect_original(group, previous)
            # Journal only a selection that passed the final guards, before PUT
            # so an applied request with a lost response remains recoverable.
            transaction_id = uuid.uuid4().hex
            pending = {"group": group, "previous": previous, "candidate": candidate, "at": iso(), "transaction_id": transaction_id,
                       "controller": self.cfg["controller"], "controller_secret_env": self.cfg["controller_secret_env"]}
            atomic_json(STATE / "pending-selection.json", pending)
            put_attempted = True
            self.api.select(group, candidate)
            last_pause_check = None

            def cancel_verification():
                nonlocal last_pause_check
                if self.stopping() or (STATE / "select.request.json").exists() or self.transport_recovery_active():
                    return True
                now = time.monotonic()
                if last_pause_check is None or now - last_pause_check >= 1:
                    last_pause_check = now
                    return not load_config()["enabled"]
                return False

            result = self.verify_path(general, self.cfg["verification_idle_seconds"],
                                      cancel=cancel_verification)
            general, latest, _ = self.require_active_mode(mode, expected_endpoint=endpoint)
            self.reload_config()
            if latest.get(group, {}).get("now") != candidate:
                event("selection_changed_during_validation", group=group)
                return False
            if (candidate not in latest[group].get("all", [])
                    or candidate not in flatten_candidates(latest, group)
                    or active_path(general, latest, group) != (*mode, candidate, candidate)):
                self.failures = 0
                self.status.update(last_health_at=None, consecutive_health_failures=0)
                raise PathChanged("候选线路在验证期间已改变，已丢弃旧线路的检查结果")
            if result["ok"] and self.transport_recovery_active(force=True):
                result = {"ok": False, "cancelled": True, "reason": "Codex 正在恢复连接，已取消普通自动换点"}
            if (result["ok"] and self.cfg["enabled"] and not self.stopping() and not (STATE / "select.request.json").exists()
                    and selection_policy(self.cfg) == policy
                    and bool(scan_candidates([candidate], self.cfg))):
                now = time.time()
                record = {**controller_identity(self.cfg), "configured_group": self.cfg["group"],
                          "group": group, "name": candidate, "last_switch_at": now,
                          "verification": result, "transaction_id": transaction_id}
                save_owned_record(STATE / "selection.json", record, latest=True)
                self.record = record
                committed = True
                self.observed_selection = active_path(general, latest, group)
                self.failures = 0
                self.last_health = now
                self.write_status(selected=candidate, mode=mode[0], group=group,
                                  health=result, last_health_at=iso(now), consecutive_health_failures=0,
                                  last_switch_at=iso(now), verification=result, last_error=None)
                event("switched", group=group, previous=previous, selected=candidate, verification=result)
                return True
            if not result["ok"] and not result.get("cancelled") and selection_policy(self.cfg) == policy:
                self.quarantine[candidate] = time.time() + self.cfg["node_quarantine_seconds"]
                self.prune_probe_history(force_quarantine=True)
            self.rollback(group, candidate, previous, pending=pending)
            event("candidate_rejected", node=candidate, reason=result.get("reason", "自动切换已暂停"))
            return False
        except Exception as error:
            # If the controller is unreachable, retain the journal for recovery.
            try:
                if put_attempted and not committed:
                    self.rollback(group, candidate, previous, pending=pending)
            finally:
                if isinstance(error, PathChanged):
                    self.refresh_cancelled_mode(str(error), clear_nodes=isinstance(error, ModeChanged))
            raise
        finally:
            self.tentative = False
            if self.state_context != state_context(self.cfg):
                self.api = Controller(self.cfg)
            self.sync_runtime_context()

    def recover_pending(self):
        path = STATE / "pending-selection.json"
        pending = pending_record(read_json(path, {}))
        if not pending:
            return
        if not pending.get("controller"):
            raise ControllerError("旧回滚记录缺少控制器归属；请停止后台，确认原控制器并手动恢复后，备份移走 state/pending-selection.json 再重试")
        # Completed selections already have a durable matching journal.
        records = owned_records(read_json(STATE / "selection.json", {}))
        completed = bool(pending.get("transaction_id")) and any(
            same_controller(item, pending) and item["group"] == pending["group"]
            and item.get("transaction_id") == pending["transaction_id"] for item in records)
        if not completed:
            if self.dry_run:
                raise ControllerError("存在未完成的节点切换；只测速模式不会自动恢复，请先执行 restore 或正常运行近舟后重试")
            api = self.api
            if not same_controller(pending, self.cfg):
                recovery_cfg = validate_config(self.cfg | {
                    "controller": pending["controller"],
                    "controller_secret_env": pending.get("controller_secret_env", "")})
                api = Controller(recovery_cfg)
            outcome = self.rollback(pending["group"], pending["candidate"], pending["previous"], api=api)
            if outcome == "superseded":
                # Retire the old claim before forgetting the unfinished handoff.
                retire_owned_record(STATE / "selection.json", pending, pending["group"])
                self.record = current_selection(self.cfg)
                if same_controller(pending, self.cfg):
                    general, proxies, group = self.api.snapshot(self.cfg, allow_direct=True)
                    if group == pending["group"]:
                        self.observe_selection(active_path(general, proxies, group), external_change=True)
        path.unlink(missing_ok=True)

    def scan(self, forced=False, *, automatic=None):
        try:
            self._scan(forced, automatic=automatic)
        except PathChanged:
            # The guard has already published the current active path.
            return

    def _scan(self, forced=False, *, automatic=None):
        self.reload_config()
        automatic = not forced and (self.cfg["enabled"] if automatic is None else automatic)

        def stop_if_paused():
            if not automatic or self.cfg["enabled"]:
                return False
            self.next_scan = 0
            self.write_status(phase="paused", next_scan_at=iso(), message="已暂停自动测速和切换")
            return True

        if stop_if_paused():
            return
        if not forced and self.defer_recovery_scan(force=True):
            return
        if not self.transport_recovery_active():
            self.recovery_scan_pending = False
        revision = self.manual_revision
        policy = selection_policy(self.cfg)
        general, proxies, group = self.api.snapshot(self.cfg, allow_direct=True)
        self.recover_pending()
        general, proxies, group = self.api.snapshot(self.cfg, allow_direct=True)
        if group is None:
            self.last_health = time.time()
            self.next_scan = self.last_health + self.cfg["health_interval_seconds"]
            self.publish_active_path(general, proxies, group, nodes=[], health=None,
                                     phase="monitoring" if self.cfg["enabled"] else "paused",
                                     next_scan_at=iso(self.next_scan),
                                     message="客户端当前为直连模式，等待切换回规则或全局模式")
            return
        mode = active_mode(general, group)
        path = active_path(general, proxies, group)
        previous = proxies[group].get("now")
        current = resolve_leaf(proxies, previous)
        all_names = flatten_candidates(proxies, group)
        names = scan_candidates(all_names, self.cfg)
        regions = (client_detection.nearby_regions(all_names, node_region)
                   if self.cfg["scan_mode"] == "adaptive" else self.cfg["scan_regions"])
        self.scan_display_names = list(names)
        if current and current not in names and current in all_names:
            self.scan_display_names.insert(0, current)
        if not names:
            self.write_status(nodes=[], scan_regions=regions, scan_node_count=0,
                              excluded_node_count=len(all_names))
            raise ControllerError("代理组中没有符合" + ("邻近地区" if self.cfg["scan_mode"] == "adaptive" else "地区范围") + "的节点；保留当前选择")
        self.observe_selection(path)
        endpoint = self.require_proxy_endpoint(general, proxies, group) if self.cfg["enabled"] and not self.dry_run else None
        self.write_status(controller_available=True, mode=mode[0], group=group, selected=current or previous,
                          phase="scanning", message="开始对所选范围测速", last_error=None,
                          nodes=self.statistics(self.scan_display_names), scan_regions=regions, scan_mode=self.cfg["scan_mode"], prefer_japan=self.cfg["prefer_japan"],
                          scan_node_count=len(names), excluded_node_count=len(all_names) - len(names),
                          region_counts=dict(collections.Counter(node_region(name) for name in names)))
        fresh = self.measure_round(names, "所选节点初测", forced=forced)
        if stop_if_paused():
            return
        if self.stopping() or self.manual_revision != revision or selection_policy(self.cfg) != policy:
            return
        if not forced and self.defer_recovery_scan():
            return
        self.require_active_mode(mode, path, expected_endpoint=endpoint)
        shortlist = select_shortlist(fresh, current, int(self.cfg["shortlist_size"]), self.cfg["prefer_japan"])
        for round_no in range(int(self.cfg["confirmation_rounds"])):
            if self.stopping() or self.manual_revision != revision or selection_policy(self.cfg) != policy:
                return
            self.measure_round(shortlist, f"候选复测 {round_no + 1}/{int(self.cfg['confirmation_rounds'])}", forced=forced)
            if stop_if_paused():
                return
            if not forced and self.defer_recovery_scan():
                return
            self.require_active_mode(mode, path, expected_endpoint=endpoint)
        if self.manual_revision != revision or self.process_manual_selection() or selection_policy(self.cfg) != policy:
            return
        _, proxies, _ = self.require_active_mode(mode, path, expected_endpoint=endpoint)
        def ranking():
            rows = self.statistics(names)
            selectable = set(proxies[group].get("all", [])) & set(flatten_candidates(proxies, group))
            for row in rows:
                if row["name"] not in selectable:
                    row.update(eligible=False, reason="该节点不在主代理组可直接选择的列表中")
                if self.failures >= 2 and row["name"] == current:
                    row.update(eligible=False, reason="所选组节点探测连续失败" if mode[0] == "unknown" else "当前实际连接连续失败")
            rows = apply_region_policy(rows, self.cfg["prefer_japan"], self.cfg["japan_poor_latency_ms"],
                                       self.cfg["japan_fallback_score_ratio"])
            return sorted(rows, key=lambda row: (not row["eligible"], row["score_ms"] if row["score_ms"] is not None else math.inf, row["name"]))
        stats = ranking()
        self.last_scan = time.time()
        self.next_scan = self.last_scan + self.cfg["scan_interval_seconds"]
        display_stats = stats + self.statistics([current]) if current and current not in names else stats
        self.write_status(nodes=display_stats, last_scan_at=iso(self.last_scan), next_scan_at=iso(self.next_scan), phase="monitoring")
        self.reload_config()
        if selection_policy(self.cfg) != policy:
            self.next_scan = 0
            self.write_status(message="地区策略已更新，将按新范围重新测速")
            return
        general, proxies, _ = self.require_active_mode(mode, path, expected_endpoint=endpoint)
        if self.dry_run or not self.cfg["enabled"]:
            self.write_status(phase="paused" if not self.cfg["enabled"] else "monitoring", message="测速完成；本轮仅测量，未切换")
            return
        if self.defer_recovery_scan():
            return
        if time.time() < self.manual_until and self.failures < 2:
            self.write_status(message="检测到手动选点，暂时尊重手动选择")
            return
        # A short active-path check detects 403/5xx hidden by old delay APIs.
        endpoint = self.require_proxy_endpoint(general, proxies, group, endpoint)
        health = self.verify_path(general)
        if health.get("cancelled"):
            return
        general, proxies, _ = self.require_active_mode(mode, path, expected_endpoint=endpoint)
        self.failures = 0 if health["ok"] else self.failures + 1
        self.write_status(health=health)
        if not health["ok"]:
            health = self.verify_path(general)
            if health.get("cancelled"):
                return
            general, proxies, _ = self.require_active_mode(mode, path, expected_endpoint=endpoint)
            self.failures = 0 if health["ok"] else self.failures + 1
            self.write_status(health=health)
        if self.defer_recovery_scan():
            return
        healthy_path = path if health["ok"] else None
        stats = ranking()
        tried = set()
        reason = "测速完成"
        attempts = 0
        while attempts < int(self.cfg["max_candidate_attempts"]):
            self.reload_config()
            if self.process_manual_selection():
                return
            if not self.cfg["enabled"] or selection_policy(self.cfg) != policy:
                self.next_scan = 0
                self.write_status(nodes=[], phase="monitoring" if self.cfg["enabled"] else "paused",
                                  message="测速策略或自动开关已更新，停止本轮切换")
                return
            initial_selection = not self.record.get("last_switch_at")
            candidate, reason = choose_candidate(
                [row for row in stats if row["name"] not in tried], current,
                self.record.get("last_switch_at", 0), time.time(),
                0 if initial_selection else self.cfg["min_improvement_ms"],
                0 if initial_selection else self.cfg["min_improvement_ratio"],
                self.cfg["switch_cooldown_seconds"], self.failures >= 2)
            if candidate is None or self.stopping():
                break
            changed = self.change_candidate(general, group, previous, candidate, expected_path=path)
            if changed is not None:
                attempts += 1
            if changed:
                healthy_path = self.observed_selection
                reason = ("已优选所选代理组；Codex 实际出站未验证" if mode[0] == "unknown"
                          else "已选用通过连接验证的低延迟节点")
                self.recover_pending()
                break
            self.recover_pending()
            if self.process_manual_selection():
                return
            if self.defer_recovery_scan():
                return
            tried.add(candidate)
            self.reload_config()
            if not self.cfg["enabled"]:
                break
            _, proxies, _ = self.require_active_mode(mode, path, expected_endpoint=endpoint)
            if proxies.get(group, {}).get("now") != previous:
                reason = "选择已被用户更改，停止本轮切换"
                break
            stats = ranking()
        if selection_policy(self.cfg) != policy:
            self.next_scan = 0
            self.write_status(nodes=[], phase="monitoring" if self.cfg["enabled"] else "paused",
                              next_scan_at=iso(), message="测速设置已更新，等待重新测速")
            return
        final_general, latest, _ = self.require_active_mode(mode, expected_endpoint=endpoint)
        final_path = active_path(final_general, latest, group)
        if final_path != self.observed_selection:
            self.observe_selection(final_path)
            self.status["verification"] = None
            self.last_health = 0.0
            reason = "检测到手动选点，暂时尊重手动选择"
        selected = final_path[3]
        selected_row = next((row for row in stats if row["name"] == selected), {})
        if (self.failures == 0 and healthy_path == final_path
                and (selected_row.get("eligible") or selected_row.get("region_standby"))
                and self.record.get("group") == group
                and self.record.get("name") == latest.get(group, {}).get("now")
                and self.record.get("last_switch_at")):
            # Healthy paths cannot make an ordinary switch during this cooldown.
            # Health failures, settings changes and explicit requests still scan sooner.
            self.next_scan = max(self.last_scan + self.cfg["scan_interval_seconds"],
                                 self.record["last_switch_at"] + self.cfg["switch_cooldown_seconds"])
        self.write_status(phase="monitoring" if self.cfg["enabled"] else "paused", mode=mode[0], group=group, selected=selected,
                          nodes=stats + self.statistics([selected]) if selected and selected not in names else stats,
                          message=reason, enabled=self.cfg["enabled"],
                          next_scan_at=iso(self.next_scan) if self.next_scan else iso())
        event("scan_completed", candidates=len(names), selected=selected, message=reason,
              scan_regions=regions, scan_mode=self.cfg["scan_mode"], prefer_japan=self.cfg["prefer_japan"])

    def tick_health(self):
        self.recover_pending()
        general, proxies, group = self.api.snapshot(self.cfg, allow_direct=True)
        path = active_path(general, proxies, group)
        self.observe_selection(path)
        if group is None:
            self.publish_active_path(general, proxies, group, health=None, nodes=[], phase="monitoring",
                                     consecutive_health_failures=self.failures,
                                     message="客户端当前为直连模式，等待切换回规则或全局模式")
            return
        endpoint = self.require_proxy_endpoint(general, proxies, group)
        result = self.verify_path(general)
        if result.get("cancelled"):
            return
        general, proxies, group = self.api.snapshot(self.cfg, allow_direct=True)
        latest_path = active_path(general, proxies, group)
        if latest_path != path:
            self.observe_selection(latest_path)
            self.next_scan = 0
            self.publish_active_path(general, proxies, group, health=None, phase="monitoring",
                                     last_health_at=None, consecutive_health_failures=0, next_scan_at=iso(),
                                     message="连接路径已改变，已丢弃上一条线路的检查结果",
                                     **({"nodes": []} if latest_path[:2] != path[:2] else {}))
            return
        self.require_proxy_endpoint(general, proxies, group, endpoint)
        self.failures = 0 if result["ok"] else self.failures + 1
        if self.failures >= 2 and time.time() - self.last_scan >= 30:
            self.next_scan = 0
        self.poll_transport_signals()
        errors = self.status.get("network_errors_recent", 0)
        self.write_status(controller_available=True, mode=latest_path[0], group=group, selected=latest_path[3],
                          health=result, network_errors_recent=errors, consecutive_health_failures=self.failures,
                          last_health_at=iso(), next_scan_at=iso(self.next_scan) if self.next_scan else iso(),
                          last_error=None if result["ok"] else result.get("reason"))

    def run(self):
        self.signals.poll()
        event("started", pid=os.getpid())
        while not self.stopping():
            try:
                self.reload_config()
                self.poll_transport_signals()
                if self.process_manual_selection():
                    continue
                requested = STATE / "scan.request"
                forced = requested.exists()
                if forced:
                    requested.unlink(missing_ok=True)
                recovery_scan_due = self.recovery_scan_pending and time.time() >= self.recovery_until
                scan_due = self.cfg["enabled"] and (time.time() >= self.next_scan or recovery_scan_due)
                health_due = (self.cfg["enabled"]
                              and time.time() - self.last_health >= self.cfg["health_interval_seconds"])
                # Check an observed but unverified path before starting another ordinary scan.
                check_new_path = (scan_due and self.observed_selection is not None
                                  and self.observed_selection[1] is not None
                                  and self.status.get("health") is None
                                  and self.failures < 2 and not recovery_scan_due)
                if forced or (scan_due and not check_new_path):
                    self.scan(forced=forced, automatic=not forced)
                elif health_due or check_new_path:
                    self.tick_health()
                    self.last_health = time.time()
                elif not self.cfg["enabled"]:
                    if time.time() - self.last_health >= self.cfg["health_interval_seconds"]:
                        self.observe_paused_path()
                    else:
                        self.write_idle_status(phase="paused", message="已暂停自动测速和切换")
                else:
                    self.write_idle_status(phase="monitoring")
            except PathChanged:
                # The guard already published the live controller state.
                pass
            except (ControllerError, ValueError, OSError) as exc:
                if isinstance(exc, ControllerError):
                    try:
                        if self.adapt_client():
                            continue
                    except (ControllerError, ValueError, OSError):
                        self.status["client_detection_message"] = "自动识别暂未成功，将稍后重试；也可在连接设置中手动检查"
                detail = self.status.get("client_detection_message", "")
                message = str(exc)
                if detail:
                    message = detail if detail.startswith(message) else message + "；" + detail
                self.write_status(phase="waiting", controller_available=False,
                                  message=message, last_error=str(exc))
                self.next_scan = time.time() + 15
                self.last_health = time.time()
            except Exception as exc:
                # Logs contain type only, never raw server responses or credentials.
                self.write_status(phase="error", message="后台检查遇到异常，将自动重试", last_error=type(exc).__name__)
                event("error", error_type=type(exc).__name__)
                self.next_scan = time.time() + 30
                self.last_health = time.time()
            for _ in range(5):
                if self.stopping():
                    break
                time.sleep(0.2)
        self.write_status(phase="stopped", message="后台程序已停止", pid=None)
        event("stopped")


def stop_daemon():
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "stop.request").touch()
    # A supported 15-second probe can keep a controller delay request open for 33 seconds.
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        lock = InstanceLock()
        if lock.acquire():
            lock.close()
            return True
        time.sleep(0.25)
    return False


def configure_client(fields, expected_context=None):
    """Save connection fields after ending transactions on the old controller.

    Discovery is a separate read-only action. This function validates local
    settings, serializes updates, and never sends controller requests.
    """
    allowed = {"controller", "controller_secret_env", "controller_api_mode", "group", "auto_detect_client", "auto_select_group"}
    if not isinstance(fields, dict) or set(fields) - allowed:
        raise ValueError("连接设置仅接受控制接口、密钥环境变量名和代理组")
    load_config()  # First-run creation must happen before the non-reentrant lock.
    with config_update_lock():
        current = load_config()
        _check_expected_context(current, expected_context)
        candidate = validate_config(current | fields)
        _check_client_environment(candidate)
        if state_context(candidate) == state_context(current):
            atomic_json(CONFIG, candidate)
            return candidate

    if not stop_daemon():
        raise RuntimeError("后台程序尚在退出，代理客户端设置未修改，请稍后重试")
    lock = InstanceLock()
    if not lock.acquire():
        raise RuntimeError("后台已重新启动，代理客户端设置未修改，请稍后重试")
    try:
        if (STATE / "pending-selection.json").exists():
            raise RuntimeError("原代理客户端仍有未完成的回滚记录；请先恢复原节点，再修改连接设置")
        with config_update_lock():
            current = load_config()
            _check_expected_context(current, expected_context)
            candidate = validate_config(current | fields)
            _check_client_environment(candidate)
            changed = state_context(candidate) != state_context(current)
            scan_request = STATE / "scan.request"
            had_pending_scan = False
            if changed:
                candidate["enabled"] = False
                atomic_json(STATE / "status.json", {
                    "controller_context": list(state_context(candidate)),
                    "enabled": False, "phase": "stopped", "pid": None,
                    "controller_available": False, "mode": None, "group": None,
                    "selected": None, "nodes": [], "health": None, "last_error": None,
                    "message": "代理客户端设置已保存，自动优选保持暂停", "updated_at": iso(),
                })
                had_pending_scan = scan_request.exists()
                scan_request.unlink(missing_ok=True)
            try:
                atomic_json(CONFIG, candidate)
            except OSError:
                if had_pending_scan:
                    scan_request.touch()
                raise
        return candidate
    finally:
        lock.close()


def restore_original():
    if not stop_daemon():
        raise RuntimeError("后台程序尚在退出，暂未恢复节点")
    lock = InstanceLock()
    if not lock.acquire():
        raise RuntimeError("后台已重新启动，暂未恢复节点，请稍后重试")
    try:
        return _restore_original_locked()
    finally:
        lock.close()


def _restore_original_locked():
    # Complete first-run creation before taking its non-reentrant update lock.
    load_config()
    with config_update_lock():
        cfg = load_config()
        cfg["enabled"] = False
        atomic_json(CONFIG, cfg)
    api = Controller(cfg)
    original = read_json(STATE / "original-selection.json", {})
    controlled = read_json(STATE / "selection.json", {})
    pending = pending_record(read_json(STATE / "pending-selection.json", {}))
    proxies = api.request("/proxies")["proxies"]
    superseded_group = None
    if same_controller(pending, cfg) and isinstance(pending.get("candidate"), str) and pending["candidate"]:
        pending_group = pending.get("group")
        current_name = proxies.get(pending_group, {}).get("now")
        current_proxy = proxies.get(current_name, {})
        if current_proxy and (current_name != pending["candidate"] or isinstance(current_proxy.get("all"), list)):
            # The snapshot proves this transaction no longer owns the path.
            # Retire older ownership first so retrying restore cannot reclaim it.
            retire_owned_record(STATE / "selection.json", cfg, pending_group)
            (STATE / "pending-selection.json").unlink(missing_ok=True)
            superseded_group = pending_group
            pending = {}
    restored = []
    for saved in owned_records(original):
        if not same_controller(saved, cfg):
            continue
        group = saved["group"]
        if group not in proxies or group == superseded_group:
            continue
        now = proxies.get(group, {}).get("now")
        pending_matches = same_controller(pending, cfg) and pending.get("group") == group
        owned = (any(same_controller(item, cfg) and item["group"] == group and item["name"] == now
                     for item in owned_records(controlled))
                 or (pending_matches and pending.get("candidate") == now))
        if now != saved["name"]:
            if (not owned or now not in flatten_candidates(proxies, now)
                    or saved["name"] not in proxies[group].get("all", [])):
                continue
            api.select(group, saved["name"])
            proxies = api.request("/proxies")["proxies"]
            if proxies.get(group, {}).get("now") != saved["name"]:
                raise ControllerError("客户端未确认原节点，恢复记录已保留，请重试")
        # Persist each success before attempting another group. If a write fails,
        # the baseline remains so a retry can retire it without repeating the PUT.
        retire_owned_record(STATE / "selection.json", cfg, group)
        if pending_matches:
            (STATE / "pending-selection.json").unlink(missing_ok=True)
            pending = {}
        retire_owned_record(STATE / "original-selection.json", cfg, group)
        restored.append(group)
    event("restore", restored_groups=restored)
    state = read_json(STATE / "status.json", {})
    context = list(state_context(cfg))
    if state.get("controller_context") != context:
        state = {}
    state.update(controller_context=context, enabled=False, phase="stopped", pid=None,
                 message="已恢复原节点" if restored else "已停止；当前节点已有手动变化或无需恢复", updated_at=iso())
    try:
        general, proxies, group = api.snapshot(cfg, allow_direct=True)
        selected = resolve_leaf(proxies, proxies[group].get("now")) if group else "DIRECT"
        state.update(mode=active_mode(general, group)[0], group=group, selected=selected,
                     controller_api_mode=cfg.get("controller_api_mode", "standard"),
                     verification_scope="selector" if cfg.get("controller_api_mode") == "selector" else "path",
                     controller_available=True, health=None, last_error=None)
    except (ControllerError, ValueError, OSError) as error:
        # Restoration has already committed; a status read cannot undo it.
        state.update(controller_available=False, mode=None, group=None, selected=None,
                     health=None, last_error=str(error),
                     message=state["message"] + "；暂时无法确认当前线路")
    atomic_json(STATE / "status.json", state)
    return restored


def main():
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="近舟 NearRoute 代理线路优选")
    parser.add_argument("command", choices=["run", "scan", "status", "stop", "restore"])
    parser.add_argument("--dry-run", action="store_true", help="只测速，不切换（仅 run 和 scan）")
    parser.add_argument("--ready-token", type=uuid.UUID, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.dry_run and args.command not in ("run", "scan"):
        parser.error("--dry-run 仅适用于 run 和 scan 命令")
    STATE.mkdir(parents=True, exist_ok=True)
    if args.command == "status":
        print(json.dumps(read_json(STATE / "status.json", {}), ensure_ascii=False, indent=2))
        return
    if args.command == "stop":
        if not stop_daemon():
            raise SystemExit("后台程序仍在退出，请稍后再试")
        return
    if args.command == "restore":
        restore_original()
        return
    from updater import is_installing
    if is_installing(ROOT):
        return
    lock = InstanceLock()
    if not lock.acquire():
        if args.command == "scan":
            if args.dry_run:
                raise SystemExit("后台正在运行；请先暂停后使用界面测速")
            (STATE / "scan.request").touch()
        return
    ready_file = STATE / f"startup-{args.ready_token.hex}.ready" if args.ready_token else None
    try:
        (STATE / "stop.request").unlink(missing_ok=True)
        if ready_file is not None:
            atomic_json(ready_file, {})
        optimizer = Optimizer(args.dry_run)
        if args.command == "scan":
            optimizer.scan(forced=True)
            optimizer.write_status(phase="stopped", pid=None)
            if sys.stdout:
                top = [n for n in optimizer.status.get("nodes", []) if n["eligible"]][:8]
                print(json.dumps({"message": optimizer.status.get("message"), "selected": optimizer.status.get("selected"), "top": top}, ensure_ascii=False, indent=2))
        else:
            optimizer.run()
    finally:
        try:
            if ready_file is not None:
                ready_file.unlink(missing_ok=True)
        finally:
            lock.close()


if __name__ == "__main__":
    main()
