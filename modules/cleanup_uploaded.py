"""
Удаление локальных файлов клипов после успешной загрузки на YouTube.

Опирается на queue/schedule.json: записи с youtube_id и status=scheduled.
Имя файла в очереди: {project_id}_{clip_stem}.mp4 (см. queue_manager.add_to_queue).
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from modules import schedule_store

logger = logging.getLogger(__name__)


def _parse_created_at(s: str) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        t = s.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _resolve_clip_in_output(
    output_root: Path, queue_filename: str
) -> tuple[Path | None, Path | None]:
    """
    Найти output/{video_id}/clips_final/{stem}.mp4 по имени файла из очереди.
    Возвращает (mp4_path, project_dir) или (None, None).
    """
    if not queue_filename or not queue_filename.lower().endswith(".mp4"):
        return None, None
    fname = queue_filename

    for vid_dir in sorted(output_root.iterdir()):
        if not vid_dir.is_dir():
            continue
        prefix = f"{vid_dir.name}_"
        if fname.startswith(prefix):
            stem = fname[len(prefix) : -4]
            mp4 = vid_dir / "clips_final" / f"{stem}.mp4"
            if mp4.parent.is_dir():
                return mp4, vid_dir

    matches = list(output_root.glob(f"*/clips_final/{fname}"))
    if len(matches) == 1:
        m = matches[0]
        if m.parent.is_dir():
            return m, m.parent.parent
    if len(matches) > 1:
        logger.warning(
            "cleanup: неоднозначное имя %s — %s папок, пропуск",
            fname,
            len(matches),
        )
    return None, None


def _delete_clip_files(mp4_path: Path) -> list[str]:
    """Удалить mp4, мета и превью. Возвращает список удалённых имён."""
    removed: list[str] = []
    stem = mp4_path.stem
    final_dir = mp4_path.parent

    if mp4_path.is_file():
        mp4_path.unlink()
        removed.append(mp4_path.name)

    for ext in (f"{stem}_meta.json", f"{stem}.json", f"{stem}_youtube.txt", f"{stem}.txt"):
        p = final_dir / ext
        if p.is_file():
            p.unlink()
            removed.append(p.name)

    thumb = final_dir / ".thumbs" / f"{stem}.jpg"
    if thumb.is_file():
        thumb.unlink()
        removed.append(".thumbs/" + thumb.name)

    return removed


def _project_has_any_mp4(project_dir: Path) -> bool:
    for sub in ("clips_final", "clips_raw"):
        d = project_dir / sub
        if d.is_dir() and any(d.glob("*.mp4")):
            return True
    return False


def run_cleanup(config: dict, base_dir: Path) -> dict[str, Any]:
    """
    Выполнить очистку по config['cleanup'].

    Returns:
        Статистика: deleted_clips, removed_projects, errors, ...
    """
    cfg = (config or {}).get("cleanup") or {}
    if not cfg.get("enabled", False):
        return {"ok": True, "skipped": "disabled"}

    days = int(cfg.get("days_after_upload", 7))
    remove_empty = bool(cfg.get("remove_empty_projects", True))
    remove_from_schedule = bool(cfg.get("remove_schedule_entries", True))
    dry_run = bool(cfg.get("dry_run", False))

    output_root = base_dir / config.get("paths", {}).get("output_dir", "output")
    queue_root = base_dir / config.get("paths", {}).get("queue_dir", "queue")
    schedule_path = queue_root / "schedule.json"

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    stats: dict[str, Any] = {
        "deleted_clips": 0,
        "bytes_freed": 0,
        "removed_projects": 0,
        "skipped_entries": 0,
        "schedule_entries_removed": 0,
        "errors": [],
    }

    ids_to_drop: set[str] = set()

    entries = schedule_store.load_safe(schedule_path)

    for e in entries:
        if e.get("status") != "scheduled":
            stats["skipped_entries"] += 1
            continue
        if not e.get("youtube_id"):
            stats["skipped_entries"] += 1
            continue
        created = _parse_created_at(e.get("created_at", ""))
        if not created or created > cutoff:
            continue

        fname = e.get("filename") or ""
        mp4_path, project_dir = _resolve_clip_in_output(output_root, fname)
        if not mp4_path or not project_dir:
            stats["skipped_entries"] += 1
            if fname:
                logger.debug(
                    "cleanup: не найден локальный файл для schedule id=%s",
                    e.get("id"),
                )
            continue

        eid = e.get("id")
        if not eid:
            continue

        try:
            sz = mp4_path.stat().st_size if mp4_path.is_file() else 0
            if dry_run:
                logger.info("cleanup [dry-run]: удалил бы %s", mp4_path)
                stats["deleted_clips"] += 1
                stats["bytes_freed"] += sz
            else:
                removed = _delete_clip_files(mp4_path)
                if removed:
                    stats["deleted_clips"] += 1
                    stats["bytes_freed"] += sz
                    logger.info("cleanup: удалено %s (%s)", mp4_path, ", ".join(removed[:5]))
                if remove_from_schedule:
                    ids_to_drop.add(str(eid))
        except Exception as ex:
            err = f"{eid}: {ex}"
            stats["errors"].append(err)
            logger.warning("cleanup: %s", err)

    if ids_to_drop and not dry_run and remove_from_schedule:
        stats["schedule_entries_removed"] = schedule_store.remove_entries_by_ids(
            schedule_path, ids_to_drop
        )

    if remove_empty and not dry_run:
        min_age_sec = 86400  # не трогать проекты младше 24 часов
        for vid_dir in sorted(output_root.iterdir()):
            if not vid_dir.is_dir():
                continue
            if _project_has_any_mp4(vid_dir):
                continue
            # Не удалять свежие проекты — могут быть в процессе обработки
            try:
                age_sec = (datetime.now(timezone.utc) - datetime.fromtimestamp(
                    vid_dir.stat().st_mtime, tz=timezone.utc
                )).total_seconds()
                if age_sec < min_age_sec:
                    logger.debug("cleanup: пропуск молодого проекта %s (%.1fч)", vid_dir.name, age_sec/3600)
                    continue
            except Exception:
                continue
            try:
                shutil.rmtree(vid_dir)
                stats["removed_projects"] += 1
                logger.info("cleanup: удалена пустая папка проекта %s", vid_dir.name)
            except Exception as ex:
                stats["errors"].append(f"rmtree {vid_dir.name}: {ex}")

    stats["ok"] = True
    return stats


def run_cleanup_safe(config: dict, base_dir: Path) -> dict[str, Any]:
    try:
        return run_cleanup(config, base_dir)
    except Exception as exc:
        logger.exception("cleanup: фатальная ошибка: %s", exc)
        return {"ok": False, "error": str(exc)}
