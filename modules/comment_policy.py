"""Comment-aware upload limits and status helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


COMMENT_THREAD_READ_COST = 1
COMMENT_REPLY_COST = 50
DEFAULT_ACTIVE_UPLOAD_LIMIT = 5
DEFAULT_COMMENT_THRESHOLD = 1
DEFAULT_REPLY_QUOTA_RESERVE_UNITS = 1500


def _project_root(base_dir: Path | None = None) -> Path:
    return Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent


def _comments_cfg(config: dict[str, Any]) -> dict[str, Any]:
    cfg = config.get("comments") or {}
    return cfg if isinstance(cfg, dict) else {}


def language_ru(code: str | None) -> str:
    names = {
        "af": "африкаанс", "am": "амхарский", "ar": "арабский", "bn": "бенгальский",
        "bg": "болгарский", "cs": "чешский", "da": "датский", "de": "немецкий",
        "el": "греческий", "en": "английский", "es": "испанский", "fa": "персидский",
        "fi": "финский", "fil": "филиппинский", "fr": "французский", "gu": "гуджарати",
        "he": "иврит", "hi": "хинди", "hr": "хорватский", "hu": "венгерский",
        "id": "индонезийский", "it": "итальянский", "ja": "японский", "ko": "корейский",
        "ka": "грузинский", "kk": "казахский", "lt": "литовский", "ms": "малайский",
        "mr": "маратхи", "nl": "нидерландский",
        "no": "норвежский", "pa": "панджаби", "pl": "польский", "pt": "португальский",
        "ro": "румынский", "ru": "русский", "sk": "словацкий", "sl": "словенский",
        "sr": "сербский", "sv": "шведский", "sw": "суахили", "ta": "тамильский",
        "te": "телугу", "th": "тайский", "tr": "турецкий", "uk": "украинский",
        "uz": "узбекский", "vi": "вьетнамский", "yo": "йоруба",
        "yue": "кантонский", "zh": "китайский", "hy": "армянский",
    }
    code = (code or "").strip().lower()
    return names.get(code, code.upper() if code else "не указан")


def _base_upload_limit(config: dict[str, Any]) -> int | None:
    raw = (config.get("youtube") or {}).get("max_uploads_per_day")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def load_analytics_snapshot(base_dir: Path | None = None) -> dict[str, Any] | None:
    path = _project_root(base_dir) / "data" / "analytics_snapshot.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def channel_comments_total(snapshot: dict[str, Any] | None, channel_name: str) -> int:
    if not snapshot:
        return 0
    for ch in snapshot.get("channels") or []:
        if ch.get("name") != channel_name:
            continue
        if ch.get("comments_total") is not None:
            try:
                return max(0, int(ch.get("comments_total") or 0))
            except (TypeError, ValueError):
                return 0
        total = 0
        for video in ch.get("videos") or []:
            try:
                total += max(0, int(video.get("comments") or 0))
            except (TypeError, ValueError):
                continue
        return total
    return 0


def effective_upload_limit(
    config: dict[str, Any],
    channel_name: str,
    *,
    base_dir: Path | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return per-channel upload limit, lowered when comments need quota room."""
    base_limit = _base_upload_limit(config)
    cfg = _comments_cfg(config)
    snapshot = snapshot if snapshot is not None else load_analytics_snapshot(base_dir)
    comments_total = channel_comments_total(snapshot, channel_name)

    threshold = int(cfg.get("active_comment_threshold", DEFAULT_COMMENT_THRESHOLD) or 1)
    active_limit_raw = cfg.get("active_channel_max_uploads_per_day", DEFAULT_ACTIVE_UPLOAD_LIMIT)
    try:
        active_limit = max(1, int(active_limit_raw))
    except (TypeError, ValueError):
        active_limit = DEFAULT_ACTIVE_UPLOAD_LIMIT

    auto_lower = bool(cfg.get("auto_lower_uploads", True))
    enabled = bool(cfg.get("enabled", True))
    active = comments_total >= threshold

    effective = base_limit
    reason = "default"
    if enabled and auto_lower and active and base_limit is not None:
        effective = min(base_limit, active_limit)
        reason = "comments_active"

    return {
        "channel": channel_name,
        "base_max_uploads_per_day": base_limit,
        "effective_max_uploads_per_day": effective,
        "comments_total": comments_total,
        "comments_active": active,
        "reason": reason,
        "snapshot_collected_at": (snapshot or {}).get("collected_at"),
        "reply_quota_reserve_units": int(
            cfg.get("reply_quota_reserve_units", DEFAULT_REPLY_QUOTA_RESERVE_UNITS)
            or DEFAULT_REPLY_QUOTA_RESERVE_UNITS
        ),
    }


def build_comments_status(config: dict[str, Any], base_dir: Path | None = None) -> dict[str, Any]:
    snapshot = load_analytics_snapshot(base_dir)
    channels_cfg = config.get("channels") or {}
    rows = []
    active_count = 0
    for name, ch_cfg in channels_cfg.items():
        if not ch_cfg.get("channel_id"):
            continue
        row = effective_upload_limit(config, name, base_dir=base_dir, snapshot=snapshot)
        row["language"] = ch_cfg.get("language") or ""
        row["language_ru"] = language_ru(ch_cfg.get("language"))
        row["channel_id"] = ch_cfg.get("channel_id") or ""
        rows.append(row)
        if row["comments_active"]:
            active_count += 1

    rows.sort(
        key=lambda r: (
            0 if r["comments_active"] else 1,
            -(r["comments_total"] or 0),
            r["channel"],
        )
    )

    cfg = _comments_cfg(config)
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "auto_lower_uploads": bool(cfg.get("auto_lower_uploads", True)),
        "active_comment_threshold": int(
            cfg.get("active_comment_threshold", DEFAULT_COMMENT_THRESHOLD) or 1
        ),
        "active_channel_max_uploads_per_day": int(
            cfg.get("active_channel_max_uploads_per_day", DEFAULT_ACTIVE_UPLOAD_LIMIT) or 5
        ),
        "reply_quota_reserve_units": int(
            cfg.get("reply_quota_reserve_units", DEFAULT_REPLY_QUOTA_RESERVE_UNITS)
            or DEFAULT_REPLY_QUOTA_RESERVE_UNITS
        ),
        "comment_thread_read_cost": COMMENT_THREAD_READ_COST,
        "comment_reply_cost": COMMENT_REPLY_COST,
        "required_scope": "https://www.googleapis.com/auth/youtube.force-ssl",
        "snapshot_collected_at": (snapshot or {}).get("collected_at"),
        "channels_total": len(rows),
        "active_channels": active_count,
        "channels": rows,
    }
