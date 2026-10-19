from __future__ import annotations
from codex_telegram_bridge.i18n import tr as _t
import os,json,subprocess,logging
from pathlib import Path
from ...paths import codex_home
log = logging.getLogger(__name__).warning

def _desktop_codex_path(path: Path, runtime_root: Path) -> bool:
    """Accept only Desktop's build directories, including after resolving links."""
    resolved = path.resolve()
    return (
        resolved.name.lower() == "codex.exe"
        and resolved.parent.parent == runtime_root.resolve()
    )


def _running_desktop_codex(runtime_root: Path) -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import psutil
        records = []
        for process in psutil.process_iter(['name', 'pid', 'ppid', 'exe']):
            info = process.info
            records.append({'Name': info['name'], 'ProcessId': info['pid'],
                            'ParentProcessId': info['ppid'], 'ExecutablePath': info['exe']})
        desktop_pids = set()
        for record in records:
            path = Path(record.get("ExecutablePath") or "")
            if (str(record.get("Name", "")).lower() == "chatgpt.exe"
                    and path.parent.name.lower() == "app"
                    and path.parent.parent.name.lower().startswith("openai.codex_")
                    and path.parent.parent.parent.name.lower() == "windowsapps"):
                desktop_pids.add(record["ProcessId"])
        return sorted({
            Path(record["ExecutablePath"]).resolve()
            for record in records
            if record.get("ParentProcessId") in desktop_pids
            and str(record.get("Name", "")).lower() == "codex.exe"
            and record.get("ExecutablePath")
            and _desktop_codex_path(Path(record["ExecutablePath"]), runtime_root)
            and Path(record["ExecutablePath"]).is_file()
        })
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

    if configured and not _desktop_codex_path(Path(configured), runtime_root):
        raise ValueError(_t('CODEX_CLI_PATH 必须指向 Codex Desktop 的 OpenAI/Codex/bin 配套程序；禁止 VS Code、CLI 和其他来源。'))
    running = (discover or _running_desktop_codex)(runtime_root)
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
