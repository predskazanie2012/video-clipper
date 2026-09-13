"""
Thread-safe хранилище расписания публикаций (schedule.json).

Единый _lock предотвращает race conditions при записи из uploader.py и app.py,
которые работают в разных потоках одного процесса.
"""

import threading
import json
from pathlib import Path
from datetime import datetime, timezone

_lock = threading.Lock()


def load(path: Path) -> list:
    """Прочитать schedule.json без захвата лока (вызывать только внутри with _lock)."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save(path: Path, entries: list) -> None:
    """Записать schedule.json без захвата лока (вызывать только внутри with _lock)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_safe(path: Path) -> list:
    """Thread-safe чтение schedule.json."""
    with _lock:
        return load(path)


def save_safe(path: Path, entries: list) -> None:
    """Thread-safe запись schedule.json."""
    with _lock:
        save(path, entries)


def append_entry(path: Path, entry: dict) -> None:
    """Thread-safe добавление новой записи в schedule.json."""
    with _lock:
        entries = load(path)
        entries.append(entry)
        save(path, entries)


def update_entry(path: Path, entry_id: str, **kwargs) -> None:
    """Thread-safe обновление одной записи по id."""
    with _lock:
        entries = load(path)
        for e in entries:
            if e.get("id") == entry_id:
                e.update(kwargs)
                break
        save(path, entries)


def remove_entry(path: Path, entry_id: str) -> None:
    """Thread-safe удаление записи по id."""
    with _lock:
        entries = load(path)
        entries = [e for e in entries if e.get("id") != entry_id]
        save(path, entries)


def remove_entries_by_ids(path: Path, ids: set) -> int:
    """Удалить записи, чей id входит в ids. Возвращает число удалённых."""
    if not ids:
        return 0
    ids_str = {str(i) for i in ids if i is not None}
    with _lock:
        entries = load(path)
        n_before = len(entries)
        entries = [e for e in entries if str(e.get("id")) not in ids_str]
        save(path, entries)
        return n_before - len(entries)


def clean_stale_failed(path: Path, channel: str, filename: str) -> int:
    """
    Удалить failed-записи для channel+filename, если существует успешная (scheduled) запись.
    Вызывается после каждой успешной загрузки для уборки мусора.
    Возвращает число удалённых записей.
    """
    with _lock:
        entries = load(path)
        # Есть ли хоть одна успешная запись для этого файла?
        has_success = any(
            e.get("channel") == channel
            and e.get("filename") == filename
            and e.get("status") == "scheduled"
            for e in entries
        )
        if not has_success:
            return 0
        n_before = len(entries)
        entries = [
            e for e in entries
            if not (
                e.get("channel") == channel
                and e.get("filename") == filename
                and e.get("status") == "failed"
            )
        ]
        save(path, entries)
        return n_before - len(entries)


def clean_all_stale_failed(path: Path) -> int:
    """
    Удалить ВСЕ failed-записи, для которых есть успешная scheduled-запись (тот же channel+filename).
    Используется для разовой чистки накопившегося мусора.
    Возвращает число удалённых записей.
    """
    with _lock:
        entries = load(path)
        successful = {
            (e.get("channel"), e.get("filename"))
            for e in entries
            if e.get("status") == "scheduled"
        }
        n_before = len(entries)
        entries = [
            e for e in entries
            if not (
                e.get("status") == "failed"
                and (e.get("channel"), e.get("filename")) in successful
            )
        ]
        save(path, entries)
        return n_before - len(entries)


def clean_interrupted_uploading(path: Path, queue_base_dir: Path, cutoff: datetime) -> int:
    """
    Убрать/пометить записи uploading, оставшиеся от прошлого процесса.
    Если файл всё ещё лежит в queue — удаляем календарную запись, чтобы он повторился.
    Если файла уже нет — помечаем failed, чтобы запись не висела как активная загрузка.
    """
    cutoff_utc = cutoff.astimezone(timezone.utc)
    changed = 0
    with _lock:
        entries = load(path)
        kept = []
        for e in entries:
            if e.get("status") != "uploading" or e.get("youtube_id"):
                kept.append(e)
                continue

            try:
                created_at = datetime.fromisoformat(e.get("created_at", "")).astimezone(timezone.utc)
            except Exception:
                created_at = cutoff_utc

            if created_at > cutoff_utc:
                kept.append(e)
                continue

            video_path = Path(queue_base_dir) / str(e.get("channel", "")) / str(e.get("filename", ""))
            if video_path.exists():
                changed += 1
                continue

            e["status"] = "interrupted"
            e["error"] = "Загрузка была прервана при перезапуске сервера"
            e["error"] = "Upload was interrupted during server restart and the queue file is missing"
            e["resolved_at"] = datetime.now(timezone.utc).isoformat()
            kept.append(e)
            changed += 1

        if changed:
            save(path, kept)
        return changed


def count_uploads_today(channel: str, entries: list) -> int:
    """Сколько видео загружено сегодня (UTC) для канала (статус uploading или scheduled)."""
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(
        1 for e in entries
        if e.get("channel") == channel
        and e.get("created_at", "").startswith(today)
        and e.get("status") in ("uploading", "scheduled")
    )
