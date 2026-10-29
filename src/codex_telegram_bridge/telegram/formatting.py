"""Pure task and quota formatting shared by execution backends."""
from __future__ import annotations
from datetime import datetime
import re
from typing import Any
from ..i18n import tr as _t


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return _t('未知')
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return _t('{p0}小时{p1}分{p2}秒', p0=format(hours, ''), p1=format(minutes, ''), p2=format(secs, ''))
    if minutes:
        return _t('{p0}分{p1}秒', p0=format(minutes, ''), p1=format(secs, ''))
    return _t('{p0}秒', p0=format(secs, ''))

def token_count(value: Any) -> int | None:
    """Keep an absent/invalid counter distinct from the measured value zero."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        count = int(value)
        if count < 0 or isinstance(value, float) and value != count:
            return None
        return count
    except (TypeError, ValueError, OverflowError):
        return None


def format_token_count(value: Any) -> str:
    count = token_count(value)
    return _t('未知') if count is None else f"{count:,}"

def format_quota_remaining(used_percent: Any) -> str:
    try:
        remaining = max(0.0, min(100.0, 100.0 - float(used_percent)))
    except (TypeError, ValueError):
        return _t('未知')
    if remaining.is_integer():
        return f"{int(remaining)}%"
    return f"{remaining:.1f}%"

def format_quota_reset(value: Any, now: datetime | None = None) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return _t('未知')
    try:
        reset_at = datetime.fromtimestamp(timestamp).astimezone()
    except (OSError, OverflowError, ValueError):
        return _t('未知')
    current = now.astimezone() if now is not None else datetime.now().astimezone()
    day_delta = (reset_at.date() - current.date()).days
    clock = reset_at.strftime("%H:%M")
    if day_delta == 0:
        return _t('今天 {p0}', p0=format(clock, ''))
    if day_delta == 1:
        return _t('明天 {p0}', p0=format(clock, ''))
    if reset_at.year == current.year:
        return _t('{p0}月{p1}日 {p2}', p0=format(reset_at.month, ''), p1=format(reset_at.day, ''), p2=format(clock, ''))
    return _t('{p0}年{p1}月{p2}日 {p3}', p0=format(reset_at.year, ''), p1=format(reset_at.month, ''), p2=format(reset_at.day, ''), p3=format(clock, ''))

def format_quota_reset_countdown(value: Any, now: datetime | None = None) -> str:
    current = now if now is not None else datetime.now().astimezone()
    reset_time = format_quota_reset(value, current)
    if reset_time in {'未知', 'Unknown'}:
        return reset_time
    seconds = float(value) - current.timestamp()
    if seconds <= 0:
        return _t('已到重置时间')
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        duration = _t('{p0} 天', p0=format(days, '')) + (_t(' {p0} 小时', p0=format(hours, '')) if hours else "")
    elif hours:
        duration = _t('{p0} 小时', p0=format(hours, '')) + (_t(' {p0} 分钟', p0=format(minutes, '')) if minutes else "")
    elif minutes:
        duration = _t('{p0} 分钟', p0=format(minutes, ''))
    else:
        duration = _t('不到 1 分钟')
    return _t('还有 {p0}', p0=format(duration, ''))

def format_account_credits(result: dict[str, Any]) -> list[str]:
    """Account credits are separate from weekly percentages and reset cards."""
    from decimal import Decimal, InvalidOperation
    buckets = result.get('rateLimitsByLimitId') or {}
    codex = buckets.get('codex') if isinstance(buckets, dict) else None
    legacy = result.get('rateLimits') or {}
    credits = codex.get('credits') if isinstance(codex, dict) else None
    if not isinstance(credits, dict) and isinstance(legacy, dict) and legacy.get('limitId') in (None, 'codex'):
        credits = legacy.get('credits')
    lines = [_t('**🪙 账户积分**')]
    if not isinstance(credits, dict):
        return lines + [_t('**余额：** 暂未返回')]
    if credits.get('unlimited') is True:
        return lines + [_t('**余额：** 不限量')]
    balance = _t('暂未返回')
    raw = credits.get('balance')
    if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
        try:
            amount = Decimal(str(raw))
            if amount.is_finite() and 0 <= amount < Decimal('1e18'):
                balance = format(amount.quantize(Decimal('0.01')), ',.2f') + _t(' 积分')
        except (InvalidOperation, ValueError):
            pass
    lines.append(_t('**余额：** ') + balance)
    available = credits.get('hasCredits')
    if isinstance(available, bool):
        lines.append(_t('**可用状态：** ') + (_t('有可用积分') if available else _t('无可用积分')))
    return lines

def extract_quota_windows(result: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Return 5-hour and 7-day windows from the main Codex account bucket."""
    targets = {300, 10080}
    selected: dict[int, dict[str, Any]] = {}
    buckets: list[dict[str, Any]] = []

    by_limit_id = result.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, dict) and "codex" in by_limit_id:
        main_bucket = by_limit_id.get("codex")
        if isinstance(main_bucket, dict):
            buckets.append(main_bucket)
    else:
        legacy = result.get("rateLimits")
        if isinstance(legacy, dict) and str(legacy.get("limitId") or "codex") == "codex":
            buckets.append(legacy)

    for bucket in buckets:
        for key in ("primary", "secondary"):
            window = bucket.get(key)
            if not isinstance(window, dict):
                continue
            try:
                duration = int(window.get("windowDurationMins"))
            except (TypeError, ValueError):
                continue
            if duration not in targets or duration in selected:
                continue
            selected[duration] = {"window": window}
    return selected

def diff_file_names(diff: str) -> list[str]:
    names: list[str] = []
    patterns = (
        r"^diff --git a/(.+?) b/(.+)$",
        r"^\+\+\+ b/(.+)$",
        r"^\*\*\* (?:Update|Add|Delete) File: (.+)$",
    )
    for line in str(diff or "").splitlines():
        for pattern in patterns:
            match = re.match(pattern, line)
            if not match:
                continue
            name = match.group(match.lastindex or 1).strip()
            if name != "/dev/null" and name not in names:
                names.append(name)
            break
    return names
