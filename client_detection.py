"""Read-only discovery and suggestions for local Clash-compatible clients."""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import urllib.parse

NEARBY_REGIONS = ("JP", "TW", "SG", "HK", "PH")
COMMON_CONTROLLER_PORTS = (9090, 9097, 9790)
MAX_CONTROLLER_ADDRESSES = 24
CONFIG_READ_UNSUPPORTED_MESSAGE = (
    "已检测到控制接口，但它不支持读取运行配置（GET /configs 返回 HTTP 405）。"
    "近舟无法安全验证当前模式和代理端口，请选择支持完整控制接口的客户端。"
)
_BUILTINS = {"DIRECT", "REJECT", "REJECTDROP", "PASS", "COMPATIBLE"}


def nearby_regions(names, region_of):
    """Adapt to the current node list without expanding outside nearby regions."""
    available = {region_of(name) for name in names}
    return [region for region in NEARBY_REGIONS if region in available]


def _is_leaf(proxies, name):
    item = proxies.get(name)
    if not isinstance(name, str) or not isinstance(item, dict) or "all" in item:
        return False
    kind = item.get("type")
    normalized = lambda value: value.upper().replace("-", "").replace("_", "")
    return (isinstance(kind, str) and bool(kind) and normalized(kind) not in _BUILTINS
            and normalized(name) not in _BUILTINS)


def recommend_group(proxies, configured_group, *, connection_chains=(), target_host="chatgpt.com", mode="rule"):
    """Recommend only groups able to select an independent proxy directly."""
    if mode == "direct":
        return {"recommended_group": None, "group_reason": "当前为直连模式，切回规则或全局模式后再选择代理组。"}
    safe = []
    for name, item in proxies.items():
        if (not isinstance(name, str) or not isinstance(item, dict) or item.get("type") != "Selector"
                or not isinstance(item.get("all"), list)):
            continue
        if (mode == "global") != (name == "GLOBAL"):
            continue
        if any(_is_leaf(proxies, member) for member in item["all"] if isinstance(member, str)):
            safe.append(name)
    if mode == "global":
        return {"recommended_group": "GLOBAL" if "GLOBAL" in safe else None,
                "group_reason": "全局模式使用 GLOBAL。" if "GLOBAL" in safe else "GLOBAL 没有可直接选择的独立节点，请手动检查代理组。"}
    if configured_group in safe:
        return {"recommended_group": configured_group, "group_reason": "保留当前有效的手动选择组。"}
    active = {name for chain in connection_chains if isinstance(chain, (list, tuple))
              for name in chain if isinstance(name, str) and name in safe}
    if len(active) == 1:
        return {"recommended_group": next(iter(active)), "group_reason": f"从 {target_host} 当前连接链路识别到可手动选择的代理组。"}
    if len(active) > 1:
        return {"recommended_group": None, "group_reason": "当前连接经过多个可选择的代理组，请确认要控制的组。"}
    if len(safe) == 1:
        return {"recommended_group": safe[0], "group_reason": "仅发现一个可直接选择独立节点的手动代理组。"}
    return {"recommended_group": None, "group_reason": "存在多个可选择的代理组，请手动选择。" if safe else "未发现可直接选择独立节点的手动代理组。"}


def controller_listeners():
    """Read listener identity so one dual-stack kernel remains one candidate."""
    if os.name != "nt":
        return []
    script = r"""
$kernelIds = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessName -match '^(mihomo([-_].*)?|clash([-_].*)?|verge-mihomo([-_].*)?)$'
} | Select-Object -ExpandProperty Id)
$listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
    $kernelIds -contains $_.OwningProcess -and $_.LocalAddress -in @('127.0.0.1', '::1', '0.0.0.0', '::')
} | Select-Object OwningProcess, LocalPort, LocalAddress)
ConvertTo-Json -InputObject $listeners -Compress
"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            return []
        listeners = json.loads(result.stdout.decode("utf-8-sig"))
        if not isinstance(listeners, list):
            return []
        found = set()
        for listener in listeners:
            if not isinstance(listener, dict):
                continue
            pid, port, host = (listener.get(key) for key in ("OwningProcess", "LocalPort", "LocalAddress"))
            if (type(pid) is int and pid > 0 and type(port) is int and 1 <= port <= 65535
                    and host in ("127.0.0.1", "::1", "0.0.0.0", "::")):
                found.add((pid, port, "127.0.0.1" if host in ("127.0.0.1", "0.0.0.0") else "::1"))
        return [{"pid": pid, "port": port, "host": host} for pid, port, host in sorted(found)]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def _local_controller(address):
    if (not isinstance(address, str) or not address or "\\" in address or "?" in address or "#" in address
            or any(ord(char) <= 32 or ord(char) >= 127 for char in address)):
        return False
    try:
        url = urllib.parse.urlsplit(address)
        return (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")
                and url.username is None and url.password is None and not url.netloc.endswith(":")
                and (url.port is None or 1 <= url.port <= 65535))
    except ValueError:
        return False


def controller_addresses(configured):
    """Build a bounded candidate list; never scan arbitrary local ports."""
    addresses = [configured] if _local_controller(configured) else []
    current = urllib.parse.urlsplit(configured) if addresses else None
    kernels = {}
    for listener in controller_listeners():
        identity = listener["pid"], listener["port"]
        kernels.setdefault(identity, set()).add(listener["host"])
    known_ports = {port for _pid, port in kernels}
    for port in COMMON_CONTROLLER_PORTS:
        if port not in known_ports:
            address = f"http://127.0.0.1:{port}"
            if address not in addresses:
                addresses.append(address)
    for (_pid, port), hosts in kernels.items():
        host = "127.0.0.1" if "127.0.0.1" in hosts else "::1"
        if (current and (current.port or 80) == port and current.path in ("", "/")
                and (current.hostname in hosts or current.hostname == "localhost" and len(hosts) == 2)):
            address = configured
        else:
            address = f"http://{'[' + host + ']' if ':' in host else host}:{port}"
        if address not in addresses:
            addresses.append(address)
    return addresses[:MAX_CONTROLLER_ADDRESSES]


def discover_clients(cfg, inspect_func):
    """Inspect bounded candidates without writing configuration or changing routes."""
    configured = cfg.get("controller")
    addresses = controller_addresses(configured)

    def inspect(address):
        fields = dict(cfg, controller=address)
        if address != configured:
            fields["controller_secret_env"] = ""
        try:
            information = inspect_func(fields, timeout=0.6, adaptive=True)
            return dict(information, controller=address, verified=True, needs_auth=False)
        except (OSError, ValueError, RuntimeError) as error:
            if getattr(error, "unsupported_config_read", False) is True:
                return {"controller": address, "verified": False, "needs_auth": False, "unsupported": True}
            needs_auth = (getattr(error, "code", None) in (401, 403)
                          or bool(re.search(r"\bHTTP(?:\s+Error)?\s+(?:401|403)\b", str(error)))
                          or (isinstance(error, ValueError) and address == configured
                              and fields.get("controller_secret_env")
                              and not os.environ.get(fields["controller_secret_env"])))
            if needs_auth:
                return {"controller": address, "verified": False, "needs_auth": True}
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="nearroute-discover") as workers:
        candidates = [candidate for candidate in workers.map(inspect, addresses) if candidate is not None]
    verified = [candidate for candidate in candidates if candidate["verified"]]
    selected = next((candidate["controller"] for candidate in verified if candidate["controller"] == configured), None)
    if selected:
        message = "当前控制地址验证成功，保留现有连接。"
    elif len(verified) == 1 and len(candidates) == 1:
        selected = verified[0]["controller"]
        message = "已识别可用控制地址，请确认代理组后保存连接。"
    elif len(verified) > 1 or verified:
        message = "发现多个可能的客户端，请选择支持配置读取的控制地址；需要认证的接口须先设置密钥环境变量。"
    elif any(candidate.get("unsupported") for candidate in candidates):
        message = CONFIG_READ_UNSUPPORTED_MESSAGE
        if any(candidate["needs_auth"] for candidate in candidates):
            message += " 另有接口需要认证，请在客户端连接中选择并测试。"
    elif candidates:
        message = "发现需要认证的本机接口，请选择地址并设置密钥环境变量后测试连接。"
    else:
        message = "未发现可用控制接口；请启动代理客户端并开启 HTTP 外部控制，或手动填写地址。"
    return {"candidates": candidates, "selected_controller": selected, "message": message}
