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
    "可在客户端连接中启用代理组兼容模式，仅优选所选组；当前模式和实际出站路径无法验证。"
)
_BUILTINS = {"DIRECT", "REJECT", "REJECTDROP", "PASS", "COMPATIBLE"}
_MAX_CLIENT_CONFIG_BYTES = 16 * 1024 * 1024


def _generated_scalar(value):
    """Read the one-line scalars emitted by the supported clients, not arbitrary YAML."""
    value = value.strip()
    if not value or value.startswith("#"):
        return None
    if value.startswith('"'):
        scalar, end = json.JSONDecoder().raw_decode(value)
        rest = value[end:].strip()
        if not isinstance(scalar, str) or rest and not rest.startswith("#"):
            raise ValueError("unsupported generated scalar")
        return scalar
    if value.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", value)
        if not match:
            raise ValueError("unsupported generated scalar")
        return match[1].replace("''", "'")
    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
    if (not value or value[0] in "[]{}&*!|>@`%,?#-:" or ": " in value
            or any(ord(char) < 32 for char in value)):
        raise ValueError("unsupported generated scalar")
    if value in ("true", "false"):
        return value == "true"
    if value in ("null", "Null", "NULL", "~"):
        return None
    # Numeric and YAML 1.1 boolean spelling is ambiguous without a full YAML parser.
    if (value.lower() in ("yes", "no", "on", "off", ".inf", "+.inf", "-.inf", ".nan")
            or re.fullmatch(r"[+-]?(?:0[xob][0-9a-fA-F_]+|[0-9_]+(?:\.[0-9_]*)?(?:[eE][+-]?[0-9]+)?|\.[0-9_]+)", value)):
        raise ValueError("unsupported generated scalar")
    return value


def _generated_fields(path, keys):
    """Extract named top-level fields from JSON or simple client-generated YAML.

    Duplicate keys, merges, tags, aliases, multiline selected values, and multiple
    documents are rejected rather than guessed. Nested proxy credentials are ignored.
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as stream:
            content = stream.read(_MAX_CLIENT_CONFIG_BYTES + 1)
        if len(content) > _MAX_CLIENT_CONFIG_BYTES:
            return None
        if content.lstrip().startswith("{"):
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate generated key")
                    result[key] = value
                return result
            parsed = json.loads(content, object_pairs_hook=unique_object)
            return {key: parsed[key] for key in keys if key in parsed}
        selected, seen, started, ended = {}, set(), False, False
        previous_key, sequence_value = None, False
        for line in content.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line == "---" and not started:
                started = True
                continue
            if line == "..." and not ended:
                ended = True
                continue
            if ended:
                raise ValueError("multiple generated documents")
            started = True
            if line[0].isspace() or line.startswith("- "):
                if previous_key in keys or line.startswith("- ") and not sequence_value:
                    raise ValueError("unsupported generated continuation")
                continue
            match = re.fullmatch(r"([A-Za-z0-9_-]+):(?:[ \t]+(.*))?", line)
            if not match or match[1] in seen:
                raise ValueError("unsupported generated mapping")
            key, value = match[1], match[2] or ""
            seen.add(key)
            previous_key, sequence_value = key, not value or value.startswith("#")
            if key in keys:
                selected[key] = _generated_scalar(value)
        return selected
    except (OSError, UnicodeError, ValueError):
        return None


def _local_client_configurations():
    """Read fixed runtime paths, never subscription profiles or directory trees.

    Source paths: clash-verge-rev/src-tauri/src/config/config.rs and
    mihomo-party-org/clash-party/src/main/{utils/dirs,core/factory}.ts.
    Both current Clash Party and earlier Mihomo Party retain the mihomo-party app ID.
    """
    appdata = os.environ.get("APPDATA")
    if os.name != "nt" or not appdata or not os.path.isabs(appdata):
        return []
    paths = [os.path.join(appdata, "io.github.clash-verge-rev.clash-verge-rev", "clash-verge.yaml")]
    party = os.path.join(appdata, "mihomo-party")
    options = _generated_fields(os.path.join(party, "config.yaml"), {"diffWorkDir"})
    if options is not None:
        different = options.get("diffWorkDir", False)
        if different is False:
            paths.append(os.path.join(party, "work", "config.yaml"))
        elif different is True:
            profile = _generated_fields(os.path.join(party, "profile.yaml"), {"current"})
            if profile is not None:
                current = profile.get("current") or "default"
                if isinstance(current, str) and re.fullmatch(r"[A-Za-z0-9_-]+", current):
                    parts = ("work", "config.yaml") if current == "work" else ("work", current, "config.yaml")
                    paths.append(os.path.join(party, *parts))
    configurations = []
    for path in paths:
        fields = _generated_fields(path, {"external-controller", "secret"})
        if not fields:
            continue
        endpoint = fields.get("external-controller")
        secret = fields.get("secret", "")
        if isinstance(endpoint, str) and isinstance(secret, str) and not any(ord(char) < 32 for char in secret):
            address = _runtime_controller_address(endpoint)
            if address:
                configurations.append((address, secret))
    return configurations


def _runtime_controller_address(endpoint):
    # external-controller is a host:port bind, not a URL or a proxy endpoint.
    if (not endpoint or any(char in endpoint for char in "/@?#\\")
            or any(ord(char) <= 32 or ord(char) >= 127 for char in endpoint)):
        return None
    address = "http://" + endpoint
    try:
        url = urllib.parse.urlsplit(address)
        if url.hostname in ("0.0.0.0", "::"):
            host = "127.0.0.1" if url.hostname == "0.0.0.0" else "[::1]"
            address = f"http://{host}:{url.port}"
        return address if _local_controller(address) and url.port is not None else None
    except ValueError:
        return None


def _controller_endpoint(address):
    if not _local_controller(address):
        return None
    url = urllib.parse.urlsplit(address)
    if url.path not in ("", "/"):
        return None
    host = "127.0.0.1" if url.hostname == "localhost" else url.hostname
    return host, url.port or 80


def local_client_credentials(controller):
    """Return an unambiguous supported-client secret for this exact local endpoint.

    The value stays in memory. Remote addresses, other ports/families, stale baseline
    settings, and conflicting runtime credentials never receive a secret.
    """
    endpoint = _controller_endpoint(controller)
    if endpoint is None:
        return ""
    secrets = {secret for address, secret in _local_client_configurations()
               if _controller_endpoint(address) == endpoint}
    return secrets.pop() if len(secrets) == 1 else ""


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
        if mode != "unknown" and (mode == "global") != (name == "GLOBAL"):
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
    for address, _secret in _local_client_configurations():
        if not any(_controller_endpoint(address) == _controller_endpoint(existing) for existing in addresses):
            addresses.append(address)
    kernels = {}
    for listener in controller_listeners():
        identity = listener["pid"], listener["port"]
        kernels.setdefault(identity, set()).add(listener["host"])
    known_ports = {port for _pid, port in kernels}
    for port in COMMON_CONTROLLER_PORTS:
        if port not in known_ports:
            address = f"http://127.0.0.1:{port}"
            if not any(_controller_endpoint(address) == _controller_endpoint(existing) for existing in addresses):
                addresses.append(address)
    for (_pid, port), hosts in kernels.items():
        host = "127.0.0.1" if "127.0.0.1" in hosts else "::1"
        if (current and (current.port or 80) == port and current.path in ("", "/")
                and (current.hostname in hosts or current.hostname == "localhost" and len(hosts) == 2)):
            address = configured
        else:
            address = f"http://{'[' + host + ']' if ':' in host else host}:{port}"
        if not any(_controller_endpoint(address) == _controller_endpoint(existing) for existing in addresses):
            addresses.append(address)
    return addresses[:MAX_CONTROLLER_ADDRESSES]


def discover_clients(cfg, inspect_func, *, addresses=None):
    """Inspect bounded candidates without writing configuration or changing routes."""
    configured = cfg.get("controller")
    addresses = (addresses or controller_addresses)(configured)

    def inspect(address):
        fields = dict(cfg, controller=address)
        if address != configured:
            fields["controller_secret_env"] = ""
            fields["controller_api_mode"] = "standard"
        try:
            try:
                information = inspect_func(fields, timeout=0.6, adaptive=True)
            except (OSError, ValueError, RuntimeError) as error:
                if getattr(error, "unsupported_config_read", False) is not True:
                    raise
                fields["controller_api_mode"] = "selector"
                try:
                    information = inspect_func(fields, timeout=0.6, adaptive=True)
                except (OSError, ValueError, RuntimeError) as retry_error:
                    if (getattr(retry_error, "code", None) in (401, 403)
                            or re.search(r"\bHTTP(?:\s+Error)?\s+(?:401|403)\b", str(retry_error))):
                        raise
                    return {"controller": address, "verified": False, "needs_auth": False, "unsupported": True}
            if fields.get("controller_api_mode") == "selector":
                information = dict(information, controller_api_mode="selector", verification_scope="selector")
            return dict(information, controller=address, controller_secret_env=fields.get("controller_secret_env", ""),
                        verified=True, needs_auth=False)
        except (OSError, ValueError, RuntimeError) as error:
            if getattr(error, "unsupported_config_read", False) is True:
                return {"controller": address, "verified": False, "needs_auth": False, "unsupported": True}
            needs_auth = (getattr(error, "authentication_required", False) is True
                          or getattr(error, "code", None) in (401, 403)
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
        message = "发现多个可能的客户端，请选择要连接的控制地址。"
    elif any(candidate.get("unsupported") for candidate in candidates):
        message = CONFIG_READ_UNSUPPORTED_MESSAGE
        if any(candidate["needs_auth"] for candidate in candidates):
            message += " 另有接口需要认证，请在客户端连接中选择并测试。"
    elif candidates:
        message = "发现需要认证的本机接口，正在尝试读取客户端连接信息；也可选择地址后重新连接。"
    else:
        message = "未发现可用控制接口；请启动代理客户端并开启 HTTP 外部控制，或手动填写地址。"
    return {"candidates": candidates, "selected_controller": selected, "message": message}
