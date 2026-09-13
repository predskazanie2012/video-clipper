"""Planning calculations for language/channel publishing stock."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


INVALID_LANGUAGE_VALUES = {"false", "true", "null", "none", "undefined"}
LOW_STOCK_CLIP_THRESHOLD = 10


def normalize_language(value, channel_name: str = "") -> str:
    """Normalize a language code, falling back to the channel suffix."""
    if isinstance(value, str):
        raw = value.strip()
        low = raw.lower()
        if raw and low not in INVALID_LANGUAGE_VALUES:
            return low
    elif value is not None:
        low = str(value).strip().lower()
        if low and low not in INVALID_LANGUAGE_VALUES:
            return low

    if isinstance(channel_name, str):
        parts = channel_name.strip().lower().split("-")
        if parts:
            cand = parts[-1]
            if cand.isalpha() and 2 <= len(cand) <= 5:
                return cand
    return ""


def parse_publish_datetime(entry: dict) -> Optional[datetime]:
    """Parse a schedule entry's publish date/time as UTC datetime."""
    publish_date = str(entry.get("publish_date") or "").strip()
    if not publish_date:
        return None

    publish_time = str(entry.get("publish_time") or "00:00").strip() or "00:00"
    if len(publish_time.split(":")) == 2:
        publish_time = f"{publish_time}:00"

    for candidate in (
        f"{publish_date}T{publish_time}+00:00",
        f"{publish_date}T00:00:00+00:00",
    ):
        try:
            return datetime.fromisoformat(candidate)
        except Exception:
            continue
    return None


def _clips_in_queue(queue_dir: Path, channel_name: str) -> int:
    channel_dir = queue_dir / channel_name
    if not channel_dir.exists():
        return 0
    return len(list(channel_dir.glob("*.mp4")))


def _daily_rate(config: dict, channel_config: dict, override: Optional[int]) -> int:
    if override:
        return max(1, int(override))

    schedule_utc = channel_config.get("schedule_utc", []) or []
    if schedule_utc:
        return max(1, len(schedule_utc))

    fallback = config.get("youtube", {}).get("max_uploads_per_day", 3) or 3
    return max(1, int(fallback))


def _is_planning_channel_enabled(config: dict, channel_name: str, channel_config: dict, base_dir: Path) -> bool:
    channel_id = str(channel_config.get("channel_id") or "").strip()
    if not channel_id:
        return False

    token_rel = str(channel_config.get("token_file", f"tokens/{channel_name}.json") or "").strip()
    if not token_rel:
        return False
    token_file = base_dir / token_rel
    account_groups = config.get("account_groups", {}) or {}
    ag_name = channel_config.get("google_account", "")
    ag = account_groups.get(ag_name, {}) or {}
    secret_rel = str(ag.get("client_secret", f"secrets/{channel_name}_secret.json") or "").strip()
    if not secret_rel:
        return False
    secret_file = base_dir / secret_rel
    return token_file.exists() and secret_file.exists()


def _empty_language_row(language: str) -> dict:
    return {
        "language": language,
        "channels": [],
        "pending": 0,
        "scheduled_future": 0,
        "scheduled_today_left": 0,
        "uploading": 0,
        "failed_future": 0,
        "per_day": 0,
        "next_publish": None,
    }


def _status_for_stock(stock_total: int, days_left: float, need_to_cut: int) -> tuple[str, int]:
    if stock_total <= 0:
        return "critical", 0
    if stock_total <= LOW_STOCK_CLIP_THRESHOLD:
        return "urgent", 1
    if need_to_cut > 0:
        return "watch", 2
    return "ok", 3


def build_planning_dashboard(
    config: dict,
    queue_dir: Path,
    schedule_entries: list,
    target_days: int = 14,
    daily_per_channel: Optional[int] = None,
    now_utc: Optional[datetime] = None,
    base_dir: Optional[Path] = None,
) -> dict:
    """Build predictive stock rows grouped by language."""
    now_utc = now_utc or datetime.now(timezone.utc)
    base_dir = base_dir or queue_dir.parent
    channels = config.get("channels", {}) or {}
    by_language: dict[str, dict] = {}

    for channel_name, channel_config in channels.items():
        if not _is_planning_channel_enabled(config, channel_name, channel_config, base_dir):
            continue

        language = normalize_language(channel_config.get("language", ""), channel_name) or channel_name
        per_day = _daily_rate(config, channel_config, daily_per_channel)
        channel_entries = [e for e in schedule_entries if e.get("channel") == channel_name]

        future_scheduled = 0
        scheduled_today_left = 0
        uploading = 0
        failed_future = 0
        next_publish_dt: Optional[datetime] = None

        for entry in channel_entries:
            status = entry.get("status")
            publish_dt = parse_publish_datetime(entry)
            is_future = publish_dt is None or publish_dt >= now_utc

            if status == "scheduled" and is_future:
                future_scheduled += 1
                if publish_dt and publish_dt.date() == now_utc.date():
                    scheduled_today_left += 1
                if publish_dt and (next_publish_dt is None or publish_dt < next_publish_dt):
                    next_publish_dt = publish_dt
            elif status == "uploading":
                uploading += 1
            elif status == "failed" and is_future:
                failed_future += 1

        row = by_language.setdefault(language, _empty_language_row(language))
        row["channels"].append(channel_name)
        row["pending"] += _clips_in_queue(queue_dir, channel_name)
        row["scheduled_future"] += future_scheduled
        row["scheduled_today_left"] += scheduled_today_left
        row["uploading"] += uploading
        row["failed_future"] += failed_future
        row["per_day"] += per_day

        if next_publish_dt:
            next_iso = next_publish_dt.isoformat()
            if row["next_publish"] is None or next_iso < row["next_publish"]:
                row["next_publish"] = next_iso

    rows = [_finalize_language_row(row, target_days) for row in by_language.values()]
    rows.sort(key=lambda r: (r["priority"], r["days_left"], -r["need_to_cut"], r["language"]))
    return {"summary": _build_summary(rows, target_days, daily_per_channel), "rows": rows}


def _finalize_language_row(row: dict, target_days: int) -> dict:
    per_day = max(1, int(row["per_day"] or 1))
    stock_total = int(row["pending"] + row["scheduled_future"] + row["uploading"])
    target_count = target_days * per_day
    days_left = stock_total / per_day
    need_to_cut = max(0, target_count - stock_total)
    status, priority = _status_for_stock(stock_total, days_left, need_to_cut)

    row.update({
        "channels_count": len(row["channels"]),
        "target_days": target_days,
        "target_count": target_count,
        "stock_total": stock_total,
        "days_left": round(days_left, 2),
        "scheduled_days_left": round(row["scheduled_future"] / per_day, 2),
        "queue_days_left": round(row["pending"] / per_day, 2),
        "need_to_cut": need_to_cut,
        "status": status,
        "priority": priority,
    })
    return row


def _build_summary(rows: list, target_days: int, daily_per_channel: Optional[int]) -> dict:
    return {
        "target_days": target_days,
        "daily_per_channel": daily_per_channel,
        "languages": len(rows),
        "channels": sum(r["channels_count"] for r in rows),
        "critical": sum(1 for r in rows if r["status"] == "critical"),
        "urgent": sum(1 for r in rows if r["status"] == "urgent"),
        "need_total": sum(r["need_to_cut"] for r in rows),
        "stock_total": sum(r["stock_total"] for r in rows),
        "min_days_left": min((r["days_left"] for r in rows), default=0),
    }
