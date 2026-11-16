from __future__ import annotations
from codex_telegram_bridge.i18n import tr as _t
import ctypes,os,re,subprocess,logging
from pathlib import Path
from ...paths import codex_home
log = logging.getLogger(__name__).warning

def _registered_package_path(full_name: str) -> Path | None:
    """Ask Windows for the installed package, never trust a lookalike folder."""
    if os.name != 'nt':
        return None
    from ctypes import wintypes
    try:
        query = ctypes.WinDLL('kernel32', use_last_error=True).GetPackagePathByFullName
        query.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
        query.restype = wintypes.LONG
        size = wintypes.UINT()
        if query(full_name, ctypes.byref(size), None) != 122 or not 0 < size.value <= 32768:
            return None
        buffer = ctypes.create_unicode_buffer(size.value)
        if query(full_name, ctypes.byref(size), buffer) != 0 or not buffer.value:
            return None
        path = Path(buffer.value)
        return path.resolve() if path.is_absolute() else None
    except (AttributeError, OSError, ValueError):
        return None


def _packaged_runtime_root(path: Path) -> Path | None:
    """Recognize the official package identity and its exact runtime location."""
    resolved = path.resolve()
    package = resolved.parent.parent.parent
    if (resolved.name.lower() == 'codex.exe'
            and resolved.parent.name.lower() == 'resources'
            and resolved.parent.parent.name.lower() == 'app'
            and re.fullmatch(r'OpenAI\.Codex_\d+(?:\.\d+){3}_(?:x64|arm64|x86|neutral)__2p2nqsd0c76g0',
                             package.name, re.I)):
        return package
    return None


def _desktop_codex_path(path: Path, runtime_root: Path) -> bool:
    """Accept extracted builds or the OS-registered Desktop package runtime."""
    resolved = path.resolve()
    if resolved.name.lower() == 'codex.exe' and resolved.parent.parent == runtime_root.resolve():
        return True
    package = _packaged_runtime_root(resolved)
    return package is not None and _registered_package_path(package.name) == package


def _retired_package_hint(path: Path, running: list[Path], runtime_root: Path) -> bool:
    # An absent old package is only a hint: select a verified active runtime,
    # never execute the missing or an unregistered existing configured path.
    package = _packaged_runtime_root(path)
    if package is None or path.exists():
        return False
    return any((current := _packaged_runtime_root(active)) is not None
               and current != package and current.parent == package.parent
               and _desktop_codex_path(active, runtime_root) for active in running)


def _running_desktop_codex(runtime_root: Path) -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import psutil
        records = []
        for process in psutil.process_iter(['name', 'pid', 'ppid', 'exe', 'cmdline']):
            info = process.info
            records.append({'Name': info['name'], 'ProcessId': info['pid'],
                            'ParentProcessId': info['ppid'], 'ExecutablePath': info['exe'],
                            'CommandLine': info.get('cmdline') or []})
        desktop_apps = {}
        for record in records:
            path = Path(record.get("ExecutablePath") or "")
            if (str(record.get("Name", "")).lower() == "chatgpt.exe"
                    and not any(arg == '--type' or arg.startswith('--type=')
                                for arg in record['CommandLine'])
                    and path.parent.name.lower() == "app"
                    and path.parent.parent.name.lower().startswith("openai.codex_")
                    and path.parent.parent.parent.name.lower() == "windowsapps"):
                desktop_apps[record['ProcessId']] = path.resolve().parent
        running = set()
        for pid, app in desktop_apps.items():
            children = set()
            for record in records:
                if (record.get('ParentProcessId') != pid
                        or str(record.get('Name', '')).lower() != 'codex.exe'
                        or not record.get('ExecutablePath')):
                    continue
                path = Path(record['ExecutablePath']).resolve()
                package = _packaged_runtime_root(path)
                if (path.is_file() and _desktop_codex_path(path, runtime_root)
                        and (package is None or package == app.parent)):
                    children.add(path)
            # A running Desktop can load its backend lazily. Only its own
            # registered package supplies this fallback, not a newer cache.
            bundled = app / 'resources/codex.exe'
            if not children and bundled.is_file() and _desktop_codex_path(bundled, runtime_root):
                children.add(bundled.resolve())
            running.update(children)
        return sorted(running)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        log(f"Desktop runtime discovery unavailable: {type(exc).__name__}")
        return []


def locate_codex(discover=None) -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise FileNotFoundError(_t('缺少 LOCALAPPDATA，无法定位 Codex Desktop 配套程序。'))
    runtime_root = Path(local) / "OpenAI/Codex/bin"
    configured = os.environ.get("CODEX_CLI_PATH")
    config_path = codex_home() / "config.toml"
    if not configured and config_path.exists():
        try:
            import tomlkit

            config = tomlkit.loads(config_path.read_text(encoding="utf-8"))
            env = (
                config.get("mcp_servers", {})
                .get("node_repl", {})
                .get("env", {})
            )
            if isinstance(env, dict) and env.get("CODEX_CLI_PATH"):
                configured = str(env["CODEX_CLI_PATH"])
        except Exception as exc:
            log(f"could not read CODEX_CLI_PATH from config: {exc}")

    running = (discover or _running_desktop_codex)(runtime_root)
    if (configured and not _desktop_codex_path(Path(configured), runtime_root)
            and not _retired_package_hint(Path(configured), running, runtime_root)):
        raise ValueError(_t('CODEX_CLI_PATH 必须指向已验证的 Codex Desktop 配套程序（Desktop 安装包或 OpenAI/Codex/bin）；禁止 VS Code、CLI 和其他来源。'))
    if len(running) > 1:
        raise RuntimeError(_t('检测到多个不同版本的 Desktop 后端；请关闭旧版本实例后重试。'))
    if running:
        return running[0]
    if configured and Path(configured).is_file():
        return Path(configured).resolve()
    existing = [path.resolve() for path in runtime_root.glob("*/codex.exe")
                if path.is_file() and _desktop_codex_path(path, runtime_root)]
    if not existing:
        raise FileNotFoundError(_t('未找到 Codex Desktop 配套程序。请先启动或更新 Desktop；不会回退到 VS Code 或 PATH 中的 CLI。'))
    return max(existing, key=lambda path: path.stat().st_mtime_ns)
