"""Read explicit local Markdown images and retain paths for Telegram fallbacks."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from codex_telegram_bridge.paths import data_dir
import re
import secrets
import stat
import time
from urllib.parse import unquote, urlsplit

MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_REPLY_PHOTO_BYTES = 20 * 1024 * 1024
MAX_REPLY_PHOTOS = 10
MAX_REPLY_IMAGE_REFERENCES = 40
PATH_CACHE_DIR = data_dir() / "telegram_image_paths"
PATH_RETENTION = 30 * 86400


@dataclass(frozen=True)
class LocalImage:
    start: int
    end: int
    label: str
    path: str


def local_path(destination: str, base_dir: str = "") -> str | None:
    value = destination.strip()
    if value.startswith("<"):
        end = value.find(">")
        if end < 0:
            return None
        value = value[1:end]
    else:
        value = re.sub(r'''\s+(?:"[^"]*"|'[^']*')\s*$''', "", value)
    value = re.sub(r"\\([\\()\[\]<> ])", r"\1", value).strip()
    if not value or any(c in value for c in "\r\n\x00") or len(value) > 4096:
        return None
    if value.lower().startswith("file:"):
        uri = urlsplit(value)
        value = unquote(uri.path)
        if uri.netloc and uri.netloc.lower() != "localhost":
            value = "//" + uri.netloc + value
    elif re.match(r"^[a-z][a-z0-9+.-]*:", value, re.I) and not re.match(r"^[a-z]:[\\/]", value, re.I):
        return None
    value = value.replace("\\", "/")
    if re.match(r"^/[a-z]:/", value, re.I):
        value = value[1:]
    if base_dir and not re.match(r"^[a-z]:/|^/", value, re.I):
        value = (Path(base_dir) / value).as_posix()
    return value


def find_local_images(text: str, base_dir: str = "") -> list[LocalImage]:
    """Ignore fenced/indented code, inline code, escaped embeds, and remote URLs."""
    images: list[LocalImage] = []
    offset = 0
    fence = ""
    opener = re.compile(r"(?<!\\)!\[((?:\\.|[^\]\\\n])*)\]\(")
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            run = marker.group(1)
            if not fence:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = ""
            offset += len(line)
            continue
        if fence or line.startswith(("    ", "\t")):
            offset += len(line)
            continue
        protected: list[tuple[int, int]] = []
        i = 0
        while i < len(line):
            if line[i] == "`":
                end = i + 1
                while end < len(line) and line[end] == "`":
                    end += 1
                closing = re.search(r"(?<!`)" + re.escape(line[i:end]) + r"(?!`)", line[end:])
                if closing:
                    stop = end + closing.end()
                    protected.append((i, stop))
                    i = stop
                    continue
                i = end
            else:
                i += 1
        for match in opener.finditer(line):
            if any(start <= match.start() < end for start, end in protected):
                continue
            end = match.end()
            depth = 1
            angled = False
            while end < len(line):
                char = line[end]
                if char == "\\" and end + 1 < len(line) and line[end + 1] in "()<>\\":
                    end += 2
                    continue
                if char == "<":
                    angled = True
                elif char == ">":
                    angled = False
                elif not angled and char == "(":
                    depth += 1
                elif not angled and char == ")":
                    depth -= 1
                    if depth == 0:
                        break
                elif char in "\r\n":
                    break
                end += 1
            if depth:
                continue
            path = local_path(line[match.end():end], base_dir)
            if path is not None:
                label = re.sub(r"\\(.)", r"\1", match.group(1))
                images.append(LocalImage(offset + match.start(), offset + end + 1, label, path))
                if len(images) >= MAX_REPLY_IMAGE_REFERENCES:
                    return images
        offset += len(line)
    return images


def read_photo(path: str, remaining: int = MAX_PHOTO_BYTES) -> tuple[bytes, str]:
    """Bound reads, reject network/device paths and files without image signatures."""
    if path.startswith("//") or ":" in path[2:] or not Path(path).is_absolute():
        raise ValueError("图片路径不是本地绝对路径")
    resolved = Path(path).resolve()
    if resolved.as_posix().startswith("//"):
        raise ValueError("图片路径指向网络或设备")
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > min(MAX_PHOTO_BYTES, remaining):
        raise ValueError("图片超过上传限制")
    with resolved.open("rb") as source:
        info = os.fstat(source.fileno())
        limit = min(MAX_PHOTO_BYTES, remaining)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("图片超过上传限制")
        prefix = source.read(32)
        if prefix.startswith(b"\x89PNG\r\n\x1a\n") and prefix[12:16] == b"IHDR":
            content_type = "image/png"
        elif prefix.startswith(b"\xff\xd8\xff"):
            content_type = "image/jpeg"
        elif prefix.startswith((b"GIF87a", b"GIF89a")):
            content_type = "image/gif"
        elif prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
            content_type = "image/webp"
        else:
            raise ValueError("文件不是可上传的图片")
        content = prefix + source.read(max(0, limit + 1 - len(prefix)))
        if len(content) > limit:
            raise ValueError("图片超过上传限制")
        return content, content_type


def path_button(token: str, chat_id: str, path: str) -> dict:
    if len(path.encode("utf-16-le")) // 2 <= 256:
        return {"text": "📋 复制图片路径", "copy_text": {"text": path}}
    PATH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - PATH_RETENTION
    for cached in PATH_CACHE_DIR.glob("*.json"):
        if re.fullmatch(r"[0-9a-f]{16}\.json", cached.name) and cached.stat().st_mtime < cutoff:
            cached.unlink(missing_ok=True)
    key = secrets.token_hex(8)
    record = {"bot": token.split(":")[0], "chat": str(chat_id), "path": path, "created": time.time()}
    target = PATH_CACHE_DIR / (key + ".json")
    temporary = target.with_suffix(f".tmp.{os.getpid()}.{time.time_ns()}")
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)
    return {"text": "📂 查看图片路径", "callback_data": "localpath|" + key}


def load_path(token: str, chat_id: str, key: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{16}", key):
        raise ValueError("图片路径按钮无效")
    target = PATH_CACHE_DIR / (key + ".json")
    try:
        if target.stat().st_size > 20 * 1024:
            raise ValueError("图片路径记录无效")
        record = json.loads(target.read_text(encoding="utf-8"))
        path = record.get("path")
        if (record.get("bot") != token.split(":")[0] or record.get("chat") != str(chat_id)
                or float(record.get("created", 0)) < time.time() - PATH_RETENTION
                or not isinstance(path, str) or not path or len(path) > 4096):
            raise ValueError("图片路径按钮无效或已过期")
        return path
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError("图片路径按钮无效或已过期") from exc
