"""
Video Clipper Web UI — FastAPI сервер.

Запуск:
    python app.py
    Затем откройте http://127.0.0.1:8088 в браузере.
"""

import io
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone, timedelta, date as date_type
from pathlib import Path
from types import SimpleNamespace

# Принудительно UTF-8 для stdout/stderr — избегает UnicodeEncodeError на Windows
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"

import secrets as _secrets_mod

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, Query
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel
from typing import Optional, List, Any

load_dotenv(__import__("pathlib").Path(__file__).resolve().parent / ".env")

from modules import schedule_store
from modules.comment_policy import build_comments_status, effective_upload_limit

BASE_DIR = Path(__file__).parent

# Создать служебные папки до инициализации логгера
for _d in ("logs", "static", "tokens", "secrets", "queue", "output", "temp", "data"):
    (BASE_DIR / _d).mkdir(exist_ok=True)

class _SuppressWinError10054(logging.Filter):
    """Подавляет шумовые WinError 10054 от asyncio на Windows."""
    def filter(self, record):
        msg = record.getMessage()
        return "10054" not in msg and "_call_connection_lost" not in msg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(BASE_DIR / "logs" / "processing.log"), encoding="utf-8"),
    ],
)
logging.getLogger("asyncio").addFilter(_SuppressWinError10054())
logger = logging.getLogger("video-clipper-ui")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    """Автозапуск планировщика при старте приложения."""
    global _scheduler_thread, _cleanup_thread, _scheduler_stop
    config = _load_config()
    _scheduler_stop.clear()
    yt_cfg = config.get("youtube") or {}
    uploads_enabled = bool(yt_cfg.get("uploads_enabled", False))
    scheduler_enabled = bool(yt_cfg.get("scheduler_enabled", False))

    def _run():
        try:
            from modules.uploader import start_scheduler
            start_scheduler(config, stop_event=_scheduler_stop, pause_callback=_processing_busy)
        except Exception as exc:
            logger.error(f"Scheduler error: {exc}")

    def _cleanup_loop():
        """Фоновая автоочистка локальных клипов после загрузки на YouTube (см. config cleanup)."""
        import time
        from modules.cleanup_uploaded import run_cleanup_safe

        try:
            cfg0 = _load_config()
            cu0 = cfg0.get("cleanup") or {}
            delay_min = int(cu0.get("initial_delay_minutes", 3))
            time.sleep(60 * max(1, delay_min))
        except Exception:
            pass
        while not _scheduler_stop.is_set():
            try:
                cfg = _load_config()
                cu = cfg.get("cleanup") or {}
                if cu.get("enabled"):
                    # Никогда не чистим диск во время активной обработки:
                    # иначе можно удалить папку проекта до завершения job.
                    with _jobs_lock:
                        has_running_jobs = any(j.get("status") == "running" for j in _jobs.values())
                    if has_running_jobs:
                        logger.info("Автоочистка пропущена: есть активная обработка видео")
                    else:
                        r = run_cleanup_safe(cfg, BASE_DIR)
                        if r.get("ok") and r.get("skipped") != "disabled":
                            if (r.get("deleted_clips") or 0) > 0 or (r.get("removed_projects") or 0) > 0:
                                logger.info("Автоочистка диска: %s", r)
                interval = int(cu.get("interval_hours", 24)) * 3600
                if _scheduler_stop.wait(timeout=max(3600, interval)):
                    break
            except Exception as exc:
                logger.warning("cleanup loop: %s", exc)
                if _scheduler_stop.wait(timeout=3600):
                    break

    if uploads_enabled and scheduler_enabled:
        _scheduler_thread = threading.Thread(target=_run, daemon=True)
        _scheduler_thread.start()
        logger.info("YouTube scheduler started automatically")
    if uploads_enabled:
        _cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
        _cleanup_thread.start()
    yield
    # Остановка при завершении
    _scheduler_stop.set()

_scheduler_thread: Optional[threading.Thread] = None
_cleanup_thread: Optional[threading.Thread] = None
_scheduler_stop = threading.Event()

app = FastAPI(title="Video Clipper UI", docs_url=None, redoc_url=None, lifespan=lifespan)

from local_access import LocalOnly
app.add_middleware(LocalOnly)



# ─────────────────────────────────────────────────────────────────
# Хранилище задач (in-memory)
# ─────────────────────────────────────────────────────────────────
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_cancel_events: dict[str, threading.Event] = {}


def _processing_busy() -> bool:
    with _jobs_lock:
        return any(j.get("status") in ("pending", "running") for j in _jobs.values())


def _active_processing_ids() -> list[str]:
    with _jobs_lock:
        return [
            str(j.get("id", ""))
            for j in _jobs.values()
            if j.get("status") in ("pending", "running")
        ]


def _guard_no_processing(operation: str = "операция YouTube") -> None:
    active = [jid for jid in _active_processing_ids() if jid]
    if active:
        raise HTTPException(
            409,
            f"{operation} временно заблокирована: сейчас идёт нарезка ({', '.join(active)}). "
            "Дождитесь завершения обработки, чтобы не перегружать диск, сеть и Python-процессы.",
        )


def _uploads_enabled(config: dict) -> bool:
    return bool((config.get("youtube") or {}).get("uploads_enabled", False))


def _scheduler_enabled(config: dict) -> bool:
    yt_cfg = config.get("youtube") or {}
    return _uploads_enabled(config) and bool(yt_cfg.get("scheduler_enabled", False))


def _manual_upload_mode(config: dict) -> bool:
    return not _uploads_enabled(config)


def _guard_uploads_enabled(config: dict) -> None:
    if not _uploads_enabled(config):
        raise HTTPException(
            423,
            "Загрузки YouTube временно отключены в config.yaml: youtube.uploads_enabled=false.",
        )


def _friendly_job_alert(job: dict) -> Optional[str]:
    status = job.get("status")
    logs = job.get("logs") or []
    tail = "\n".join(str(x) for x in logs[-60:]).lower()
    last_log_at = float(job.get("last_log_at") or job.get("created_ts") or time.time())

    checks = [
        (("winerror 483", "fatal device hardware error"), "Проблема с диском: Windows сообщает о сбое устройства. Лучше остановить нарезку и проверить внешний диск/кабель."),
        (("no space left", "not enough space", "there is not enough space"), "На диске не хватает места для временных или готовых файлов."),
        (("unable to create process", "not a valid application"), "Проблема с Python-окружением: виртуальная среда ссылается на сломанный Python."),
        (("cuda out of memory", "outofmemory", "out of memory"), "Не хватает памяти GPU/CPU для Whisper или кодирования."),
        (("error opening output", "failed to open", "permission denied"), "FFmpeg не смог записать файл. Часто причина: диск, права доступа или файл занят."),
        (("traceback", "[error]", "exception"), "В задаче появилась ошибка. Откройте последнюю строку лога или перезапустите нарезку после проверки файлов."),
    ]
    for needles, message in checks:
        if any(n in tail for n in needles):
            return message

    if status == "running" and time.time() - last_log_at > 20 * 60:
        return "Нарезка давно не пишет новые строки в лог. Возможно, процесс завис или ждёт внешний ресурс."
    if status == "error":
        return "Нарезка завершилась с ошибкой. Новые выгрузки на YouTube не запускались."
    return None


_CONFIG_PATH = BASE_DIR / "config.yaml"
_config_cache: dict = {}
_config_mtime: float = 0.0


def _load_config() -> dict:
    """Загрузить config.yaml с кэшированием по mtime файла."""
    global _config_cache, _config_mtime
    try:
        mtime = _CONFIG_PATH.stat().st_mtime
    except FileNotFoundError:
        return {}
    if mtime != _config_mtime:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            _config_cache = yaml.safe_load(f) or {}
        _config_mtime = mtime
    return _config_cache


def _save_config(config: dict):
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False,
                  sort_keys=False)
    # Сбросить кэш после сохранения
    global _config_mtime
    _config_mtime = 0.0


def _output_dir(config: Optional[dict] = None) -> Path:
    """Путь к папке с готовыми клипами."""
    cfg = config or _load_config()
    return BASE_DIR / cfg.get("paths", {}).get("output_dir", "output")


def _queue_dir(config: Optional[dict] = None) -> Path:
    """Путь к папке очереди загрузки."""
    cfg = config or _load_config()
    return BASE_DIR / cfg.get("paths", {}).get("queue_dir", "queue")


def _snapshot_output_projects(output_dir: Path) -> list[str]:
    if not output_dir.exists():
        return []
    try:
        return sorted(
            p.name
            for p in output_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".") and not (p / ".delete_pending").exists()
        )
    except Exception:
        return []


def _snapshot_queue_counts(queue_dir: Path) -> dict[str, int]:
    if not queue_dir.exists():
        return {}
    counts: dict[str, int] = {}
    try:
        for ch_dir in queue_dir.iterdir():
            if ch_dir.is_dir():
                counts[ch_dir.name] = len(list(ch_dir.glob("*.mp4")))
    except Exception:
        return counts
    return counts


def _collect_job_artifacts(job: dict, config: Optional[dict] = None) -> dict:
    cfg = config or _load_config()
    output_dir = _output_dir(cfg)
    queue_dir = _queue_dir(cfg)

    before_projects_raw = job.get("output_projects_before")
    project_rows: list[dict] = []
    clips_created = 0
    raw_clips = 0
    if isinstance(before_projects_raw, list):
        before_projects = set(str(x) for x in before_projects_raw)
        if output_dir.exists():
            try:
                for project_dir in output_dir.iterdir():
                    if (
                        not project_dir.is_dir()
                        or project_dir.name.startswith(".")
                        or (project_dir / ".delete_pending").exists()
                        or project_dir.name in before_projects
                    ):
                        continue
                    final_dir = project_dir / "clips_final"
                    raw_dir = project_dir / "clips_raw"
                    final_count = len(list(final_dir.glob("*.mp4"))) if final_dir.exists() else 0
                    raw_count = len(list(raw_dir.glob("*"))) if raw_dir.exists() else 0
                    clips_created += final_count
                    raw_clips += raw_count
                    project_rows.append({
                        "id": project_dir.name,
                        "clips": final_count,
                        "raw_clips": raw_count,
                    })
            except Exception:
                pass

    queued_added = 0
    queued_channels: list[dict] = []
    before_queue_raw = job.get("queue_counts_before")
    if isinstance(before_queue_raw, dict):
        before_queue = {str(k): int(v or 0) for k, v in before_queue_raw.items()}
        now_queue = _snapshot_queue_counts(queue_dir)
        for channel in sorted(set(before_queue) | set(now_queue)):
            delta = max(0, int(now_queue.get(channel, 0)) - int(before_queue.get(channel, 0)))
            if delta:
                queued_added += delta
                queued_channels.append({"channel": channel, "added": delta})

    return {
        "clips_created": clips_created,
        "raw_clips": raw_clips,
        "queued_added": queued_added,
        "queued_channels": queued_channels,
        "projects": project_rows,
    }


def _read_meta(meta_path: Path) -> dict:
    """Прочитать JSON метаданных клипа.
    Пробует {stem}_meta.json и fallback {stem}.json. Возвращает {} при ошибке.
    """
    # Поддержка обоих форматов имён файла
    candidates = [meta_path]
    stem = meta_path.stem
    if stem.endswith("_meta"):
        candidates.append(meta_path.with_name(stem[:-5] + ".json"))
    else:
        candidates.append(meta_path.with_name(stem + "_meta.json"))
    for p in candidates:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


def _viral_score_payload(meta: dict | None, clip_meta: dict | None = None) -> dict | None:
    """Return existing viral_score or compute it without mutating source metadata."""
    meta = meta or {}
    existing = meta.get("viral_score")
    if isinstance(existing, dict):
        try:
            score = int(round(float(existing.get("score"))))
        except (TypeError, ValueError):
            score = None
        if score is not None:
            existing = dict(existing)
            existing["score"] = max(0, min(100, score))
            return existing
    if isinstance(existing, (int, float)):
        score = max(0, min(100, int(round(float(existing)))))
        label = "high" if score >= 75 else "medium" if score >= 55 else "low"
        label_ru = "сильный потенциал" if label == "high" else "средний потенциал" if label == "medium" else "слабый потенциал"
        return {"score": score, "label": label, "label_ru": label_ru, "reasons": [], "signals": {}}

    try:
        from modules.viral_score import score_metadata

        return score_metadata(meta, clip_meta)
    except Exception as exc:
        logger.debug("viral score compute failed: %s", exc)
        return None


def _normalize_language(value, channel_name: str = "") -> str:
    """Нормализовать код языка; fallback: суффикс имени канала (nanoplastic-no -> no)."""
    from modules.planning import normalize_language
    return normalize_language(value, channel_name)


def _clip_language_mismatch_message(
    meta: dict,
    channel: str,
    channel_config: dict,
    filename: str,
) -> str:
    from modules.language_guard import LanguageChannelMismatch, assert_clip_language_matches_channel

    try:
        assert_clip_language_matches_channel(meta, channel, channel_config, filename)
    except LanguageChannelMismatch as exc:
        return str(exc)
    return ""


def _queue_manual_upload_payload(mp4: Path, meta: dict) -> dict:
    from modules.manual_metadata import hashtags_line, manual_upload_text, sidecar_path_for, youtube_description

    sidecar = sidecar_path_for(mp4)
    return {
        "description": meta.get("description", ""),
        "tags": meta.get("tags", []),
        "hashtags": hashtags_line(meta),
        "youtube_description": youtube_description(meta),
        "upload_text": manual_upload_text(meta, filename=mp4.name),
        "upload_text_path": str(sidecar),
        "upload_text_exists": sidecar.exists(),
        "viral_score": _viral_score_payload(meta),
    }


YOUTUBE_REQUIRED_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def _token_status(token_file: Path, required_scopes: Optional[list[str]] = None) -> dict:
    required = set(required_scopes or YOUTUBE_REQUIRED_SCOPES)
    if not token_file.exists():
        return {
            "ok": False,
            "status": "missing_token",
            "message": "Токен YouTube не найден",
            "missing_scopes": [],
        }

    try:
        data = json.loads(token_file.read_text(encoding="utf-8"))
    except Exception:
        return {
            "ok": False,
            "status": "invalid_token",
            "message": "Файл токена YouTube повреждён или не читается",
            "missing_scopes": [],
        }

    scopes_raw = data.get("scopes") or []
    scopes = set(str(s) for s in scopes_raw if str(s).strip())
    missing_scopes = sorted(required - scopes) if scopes else []
    has_refresh = bool(data.get("refresh_token"))
    expiry_raw = str(data.get("expiry") or "").strip()
    expired = False
    if expiry_raw:
        try:
            expiry_dt = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
            expired = expiry_dt <= datetime.now(timezone.utc)
        except Exception:
            pass

    if missing_scopes:
        return {
            "ok": False,
            "status": "reauth_required",
            "message": "Токен YouTube старый: не хватает новых прав API",
            "missing_scopes": missing_scopes,
            "expired": expired,
        }
    if not has_refresh:
        return {
            "ok": False,
            "status": "reauth_required",
            "message": "Токен YouTube без refresh-token: нужна повторная авторизация",
            "missing_scopes": [],
            "expired": expired,
        }
    return {
        "ok": True,
        "status": "ok",
        "message": "Токен YouTube готов",
        "missing_scopes": [],
        "expired": expired,
    }


def _channel_auth_status(channel: str, cfg: dict, account_groups: dict) -> dict:
    token_file = BASE_DIR / cfg.get("token_file", f"tokens/{channel}.json")
    ag_name = cfg.get("google_account", "")
    ag = account_groups.get(ag_name, {})
    secret_path = BASE_DIR / ag.get("client_secret", f"secrets/{channel}_secret.json")
    token = _token_status(token_file)
    has_secret = secret_path.exists()
    if not has_secret:
        return {
            **token,
            "ok": False,
            "status": "missing_secret",
            "message": "Не найден client_secret.json для Google Cloud",
            "has_secret": False,
            "token_exists": token_file.exists(),
        }
    return {
        **token,
        "has_secret": True,
        "token_exists": token_file.exists(),
    }


# ─────────────────────────────────────────────────────────────────
# Logging handler — перехват логов конкретной задачи
# ─────────────────────────────────────────────────────────────────
class _JobLogHandler(logging.Handler):
    def __init__(self, job_id: str):
        super().__init__()
        self.job_id = job_id
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        with _jobs_lock:
            job = _jobs.get(self.job_id)
            if job:
                job["logs"].append(msg)
                job["last_log_at"] = time.time()
                job["last_log"] = msg
                job["log_queue"].put(msg)


# ─────────────────────────────────────────────────────────────────
# Обработка видео в фоне
# ─────────────────────────────────────────────────────────────────
# Максимум источников за один запуск (URLs или локальных путей — отдельно, не вперемешку).
MAX_BATCH_VIDEOS = 50


class ProcessRequest(BaseModel):
    url: Optional[str] = None
    input_path: Optional[str] = None
    batch_urls: Optional[List[str]] = None
    batch_input_paths: Optional[List[str]] = None
    max_clips: int = 10
    crop: str = "center"
    lang: str = "auto"
    subtitles: bool = True
    subtitle_preset: str = "mozi"
    subtitle_position: Optional[str] = None    # "top"/"top-mid"/"center"/"bot-mid"/"bottom"
    subtitle_size: Optional[int] = None        # px override (48/64/80/96)
    # None → брать subtitles.per_clip_whisper из config.yaml
    subtitle_per_clip_whisper: Optional[bool] = None
    hook_title: bool = False                # заголовок-хук сверху (по умолчанию выкл.)
    hook_title_style: str = "card"          # стиль: card/neon/fire/clean/pink
    hook_title_duration: float = 3.5        # длительность показа (сек)
    description_suffix: str = ""           # суффикс описания для всех клипов
    add_to_queue: bool = False
    encode_quality: str = "turbo"          # "high" | "medium" | "fast" | "turbo"


def _normalize_process_sources(params: ProcessRequest) -> tuple[list[str], list[str]]:
    """Вернуть (urls, paths) — ровно один список непустой; комментарии # в URL отбрасываются."""
    urls: list[str] = []
    if params.batch_urls:
        urls = [
            u.strip()
            for u in params.batch_urls
            if u and str(u).strip() and not str(u).strip().startswith("#")
        ]
    elif params.url and params.url.strip():
        urls = [params.url.strip()]

    paths: list[str] = []
    if params.batch_input_paths:
        paths = [p.strip() for p in params.batch_input_paths if p and str(p).strip()]
    elif params.input_path and params.input_path.strip():
        paths = [params.input_path.strip()]

    return urls, paths


def _run_process(job_id: str, params: ProcessRequest):
    handler = _JobLogHandler(job_id)
    root = logging.getLogger()
    root.addHandler(handler)

    cancel_event = threading.Event()
    _cancel_events[job_id] = cancel_event

    with _jobs_lock:
        _jobs[job_id]["status"] = "running"

    try:
        config = _load_config()

        # Язык в UI — это язык исходного аудио для Whisper.
        # "auto" должен действительно включать автоопределение, а не брать
        # устаревший source_lang из config.yaml.
        requested_lang = (params.lang or "auto").strip().lower()
        config["source_lang"] = requested_lang if requested_lang else "auto"

        urls, paths = _normalize_process_sources(params)
        if urls:
            args = SimpleNamespace(
                url=None,
                urls_list=urls,
                urls_file=None,
                input=None,
                input_paths_list=None,
                max_clips=params.max_clips,
                crop=params.crop,
                langs=params.lang,
                add_to_queue=params.add_to_queue,
                no_subtitles=not params.subtitles,
                subtitle_preset=params.subtitle_preset,
                subtitle_position=params.subtitle_position,
                subtitle_size=params.subtitle_size,
                subtitle_per_clip_whisper=params.subtitle_per_clip_whisper,
                hook_title=params.hook_title,
                hook_title_style=params.hook_title_style,
                hook_title_duration=params.hook_title_duration,
                description_suffix=params.description_suffix,
                encode_quality=params.encode_quality,
                cancel_event=cancel_event,
            )
        else:
            args = SimpleNamespace(
                url=None,
                urls_list=None,
                urls_file=None,
                input=None,
                input_paths_list=paths,
                max_clips=params.max_clips,
                crop=params.crop,
                langs=params.lang,
                add_to_queue=params.add_to_queue,
                no_subtitles=not params.subtitles,
                subtitle_preset=params.subtitle_preset,
                subtitle_position=params.subtitle_position,
                subtitle_size=params.subtitle_size,
                subtitle_per_clip_whisper=params.subtitle_per_clip_whisper,
                hook_title=params.hook_title,
                hook_title_style=params.hook_title_style,
                hook_title_duration=params.hook_title_duration,
                description_suffix=params.description_suffix,
                encode_quality=params.encode_quality,
                cancel_event=cancel_event,
            )

        from main import cmd_process
        cmd_process(args, config)

        with _jobs_lock:
            if _jobs[job_id]["status"] == "running":
                _jobs[job_id]["status"] = "done"

    except Exception as exc:
        if cancel_event.is_set():
            logging.getLogger("video-clipper").info(f"Задача {job_id} отменена пользователем.")
            with _jobs_lock:
                _jobs[job_id]["status"] = "cancelled"
        else:
            logging.getLogger("video-clipper").error(f"Ошибка задачи {job_id}: {exc}", exc_info=True)
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(exc)
    finally:
        _cancel_events.pop(job_id, None)
        root.removeHandler(handler)
        with _jobs_lock:
            job_for_summary = dict(_jobs.get(job_id) or {})
        if job_for_summary:
            artifacts = _collect_job_artifacts(job_for_summary)
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id]["artifact_counts"] = artifacts
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job:
                job["log_queue"].put(None)  # sentinel → SSE завершается


# ─────────────────────────────────────────────────────────────────
# Video Upload API
# ─────────────────────────────────────────────────────────────────
@app.post("/api/upload-video")
async def upload_video(request: Request):
    """Загрузить видеофайл на сервер, сохранить в temp/, вернуть путь.
    Использует request напрямую чтобы снять ограничение multipart по размеру файла."""
    video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv"}
    MAX_PART = 1024 * 1024 * 1024 * 100  # 100 ГБ — фактически без ограничений
    form = await request.form(max_files=MAX_BATCH_VIDEOS, max_fields=100, max_part_size=MAX_PART)
    files = [
        item
        for key in ("file", "files")
        for item in form.getlist(key)
        if hasattr(item, "filename") and hasattr(item, "file")
    ]
    if not files:
        raise HTTPException(400, "Поле 'file' не найдено в запросе")
    if len(files) > MAX_BATCH_VIDEOS:
        raise HTTPException(400, f"За один раз можно загрузить не больше {MAX_BATCH_VIDEOS} файлов")

    paths: list[str] = []
    for file in files:
        raw_name = Path(str(file.filename or "video")).name
        suffix = Path(raw_name).suffix.lower()
        if suffix not in video_exts:
            raise HTTPException(400, f"Неподдерживаемый формат: {suffix}")

        stem = re.sub(r"[^\w .()-]+", "_", Path(raw_name).stem, flags=re.UNICODE).strip(" .") or "video"
        dest = BASE_DIR / "temp" / f"{stem}{suffix}"
        if dest.exists():
            for idx in range(2, 1000):
                candidate = BASE_DIR / "temp" / f"{stem}_{idx}{suffix}"
                if not candidate.exists():
                    dest = candidate
                    break
            else:
                dest = BASE_DIR / "temp" / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"

        with dest.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        paths.append(str(dest))

    return {"path": paths[0], "paths": paths, "count": len(paths)}


# ─────────────────────────────────────────────────────────────────
# Jobs API
# ─────────────────────────────────────────────────────────────────
@app.get("/api/meta")
def api_meta():
    """Проверка версии API из UI (пакетные источники, лимит). Старый процесс без этого маршрута — 404."""
    return {
        "batch_sources": True,
        "max_batch_sources": MAX_BATCH_VIDEOS,
    }


@app.post("/api/jobs/start")
def start_job(params: ProcessRequest):
    urls, paths = _normalize_process_sources(params)
    if not urls and not paths:
        raise HTTPException(400, "Укажите YouTube URL(ы) или локальный файл(ы)")
    if urls and paths:
        raise HTTPException(
            400,
            "Укажите либо набор ссылок, либо набор локальных путей — не оба варианта сразу",
        )
    n = len(urls) if urls else len(paths)
    if n > MAX_BATCH_VIDEOS:
        raise HTTPException(
            400,
            f"За один запуск не более {MAX_BATCH_VIDEOS} видео (сейчас {n}). Разбейте на несколько задач.",
        )
    active_uploads = _active_upload_entries()
    if active_uploads:
        sample_channels = sorted({str(e.get("channel") or "?") for e in active_uploads})[:4]
        suffix = ", ".join(sample_channels)
        if len(active_uploads) > len(sample_channels):
            suffix += f" +{len(active_uploads) - len(sample_channels)}"
        raise HTTPException(
            409,
            f"Нарезка временно заблокирована: сейчас идёт загрузка на каналы ({suffix}). "
            "Дождитесь завершения, чтобы не перегружать диск, сеть и Python-процессы.",
        )

    job_id = str(uuid.uuid4())[:8]
    config = _load_config()
    output_projects_before = _snapshot_output_projects(_output_dir(config))
    queue_counts_before = _snapshot_queue_counts(_queue_dir(config))
    logger.info(
        "job %s created: %s source(s), lang=%s, subtitles=%s, queue=%s",
        job_id,
        n,
        params.lang,
        params.subtitles,
        params.add_to_queue,
    )
    eff_params = params.model_copy(
        update={
            "url": None,
            "input_path": None,
            "batch_urls": urls if urls else None,
            "batch_input_paths": paths if paths else None,
        }
    )
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "pending",
            "logs": [],
            "log_queue": queue.Queue(),
            "params": eff_params.model_dump(),
            "created_at": time.strftime("%H:%M:%S"),
            "created_ts": time.time(),
            "last_log_at": time.time(),
            "output_projects_before": output_projects_before,
            "queue_counts_before": queue_counts_before,
        }

    thread = threading.Thread(target=_run_process, args=(job_id, eff_params), daemon=True)
    thread.start()
    return {"job_id": job_id}


@app.get("/api/jobs")
def list_jobs():
    with _jobs_lock:
        return [
            {
                "id": j["id"],
                "status": j["status"],
                "params": j["params"],
                "log_lines": len(j["logs"]),
                "created_at": j.get("created_at", ""),
                "last_log_age_sec": round(time.time() - float(j.get("last_log_at") or time.time()), 1),
                "alert": _friendly_job_alert(j),
            }
            for j in _jobs.values()
        ]


@app.get("/api/jobs/{job_id}/logs")
def get_job_logs(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return {
        "logs": job["logs"],
        "status": job["status"],
        "last_log_age_sec": round(time.time() - float(job.get("last_log_at") or time.time()), 1),
        "alert": _friendly_job_alert(job),
    }


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Отменить выполняющуюся задачу."""
    event = _cancel_events.get(job_id)
    if event:
        event.set()
        with _jobs_lock:
            if _jobs.get(job_id, {}).get("status") == "running":
                _jobs[job_id]["status"] = "cancelled"
        return {"ok": True, "message": "Задача отменяется..."}
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return {"ok": False, "message": f"Задача уже завершена ({job['status']})"}


@app.get("/api/log/tail")
def log_tail(lines: int = Query(200, ge=1, le=2000)):
    """Последние N строк из logs/processing.log."""
    log_path = BASE_DIR / "logs" / "processing.log"
    if not log_path.exists():
        return {"lines": [], "exists": False}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = [l.rstrip("\n\r") for l in all_lines[-lines:]]
        return {"lines": tail, "exists": True, "total": len(all_lines)}
    except Exception as exc:
        return {"lines": [f"[ошибка чтения лога: {exc}]"], "exists": True, "total": 0}


@app.get("/api/log/stream")
def log_stream(lines: int = Query(800, ge=1, le=2000)):
    """SSE-поток общего logs/processing.log: сначала хвост, затем новые строки."""
    log_path = BASE_DIR / "logs" / "processing.log"

    def generate():
        pos = 0
        if log_path.exists():
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                    for line in all_lines[-lines:]:
                        yield f"data: {json.dumps({'log': line.rstrip(chr(10) + chr(13))})}\n\n"
                    pos = f.tell()
            except Exception as exc:
                yield f"data: {json.dumps({'log': f'[ошибка чтения лога: {exc}]'})}\n\n"

        while True:
            try:
                if not log_path.exists():
                    yield f"data: {json.dumps({'heartbeat': True})}\n\n"
                    time.sleep(1)
                    continue

                size = log_path.stat().st_size
                if size < pos:
                    pos = 0

                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    new_lines = f.readlines()
                    pos = f.tell()

                if new_lines:
                    for line in new_lines:
                        yield f"data: {json.dumps({'log': line.rstrip(chr(10) + chr(13))})}\n\n"
                else:
                    yield f"data: {json.dumps({'heartbeat': True})}\n\n"
                    time.sleep(1)
            except GeneratorExit:
                break
            except Exception as exc:
                yield f"data: {json.dumps({'log': f'[ошибка live-лога: {exc}]'})}\n\n"
                time.sleep(2)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/jobs/{job_id}/stream")
def stream_job(job_id: str):
    """SSE-поток логов задачи в реальном времени."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")

    existing = list(job["logs"])
    log_q = job["log_queue"]
    status_ref = [job["status"]]

    def generate():
        # Сначала отдаём уже накопленные строки
        for line in existing:
            yield f"data: {json.dumps({'log': line})}\n\n"

        if status_ref[0] in ("done", "error"):
            yield f"data: {json.dumps({'done': True, 'status': status_ref[0]})}\n\n"
            return

        # Затем слушаем очередь
        while True:
            try:
                msg = log_q.get(timeout=30)
            except queue.Empty:
                yield f"data: {json.dumps({'heartbeat': True})}\n\n"
                continue

            if msg is None:
                with _jobs_lock:
                    final = _jobs.get(job_id, {}).get("status", "done")
                yield f"data: {json.dumps({'done': True, 'status': final})}\n\n"
                break

            yield f"data: {json.dumps({'log': msg})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ─────────────────────────────────────────────────────────────────
# Subtitle Presets API
# ─────────────────────────────────────────────────────────────────
@app.get("/api/subtitle-presets")
def get_subtitle_presets():
    from modules.subtitles import PRESETS
    return [
        {
            "id":             k,
            "label":          v["label"],
            "preview_bg":     v.get("preview_bg", "#111"),
            "preview_fg":     v.get("preview_fg", "#fff"),
            "preview_stroke": v.get("preview_stroke", "transparent"),
            "preview_accent": v.get("preview_accent", None),
            "font_size":      v.get("font_size", 52),
            "bold":           v.get("bold", True),
            "alignment":      v.get("alignment", 2),
            "margin_v":       v.get("margin_v", 120),
            "karaoke":        v.get("karaoke", False),
        }
        for k, v in PRESETS.items()
    ]


# ─────────────────────────────────────────────────────────────────
# Clip Metadata Edit API
# ─────────────────────────────────────────────────────────────────
class MetaPatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None


@app.patch("/api/clips/{video_id}/{clip_id}/meta")
def patch_clip_meta(video_id: str, clip_id: str, body: MetaPatch):
    config = _load_config()
    output_dir = _output_dir(config)
    meta_path = output_dir / video_id / "clips_final" / f"{clip_id}_meta.json"
    if not meta_path.exists():
        raise HTTPException(404, "Метаданные не найдены")
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    if body.title is not None:
        meta["title"] = body.title[:100]  # YouTube лимит
    if body.description is not None:
        meta["description"] = body.description
    if body.tags is not None:
        meta["tags"] = [t.lstrip("#").strip() for t in body.tags if t.strip()]
    try:
        from modules.viral_score import attach_viral_score

        attach_viral_score(meta)
    except Exception as exc:
        logger.debug("viral score refresh failed: %s", exc)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    try:
        from modules.manual_metadata import write_manual_upload_text, write_plain_upload_text

        video_path = output_dir / video_id / "clips_final" / f"{clip_id}.mp4"
        write_manual_upload_text(video_path, meta, meta_path.with_name(f"{clip_id}_youtube.txt"))
        write_plain_upload_text(video_path, meta, meta_path.with_name(f"{clip_id}.txt"))
    except Exception as exc:
        logger.warning("Manual upload text refresh failed for %s: %s", meta_path.name, exc)
    return {"ok": True, "meta": meta}


def _safe_path_segment(seg: str, max_len: int = 160) -> bool:
    if not seg or len(seg) > max_len:
        return False
    if ".." in seg or "/" in seg or "\\" in seg:
        return False
    return bool(re.match(r"^[a-zA-Z0-9_.-]+$", seg))


def _safe_project_segment(seg: str, max_len: int = 240) -> bool:
    """Allow real Windows project folder names while blocking traversal."""
    if not seg or len(seg) > max_len:
        return False
    if seg in {".", ".."}:
        return False
    if "\x00" in seg or "/" in seg or "\\" in seg:
        return False
    return True


def _resolve_output_project_dir(config: dict, video_id: str) -> Path:
    if not _safe_project_segment(video_id):
        raise HTTPException(400, "Некорректное имя проекта")
    out_root = _output_dir(config).resolve()
    project_dir = (out_root / video_id).resolve()
    try:
        project_dir.relative_to(out_root)
    except ValueError:
        raise HTTPException(400, "Некорректный путь") from None
    if not project_dir.is_dir():
        raise HTTPException(404, "Проект не найден")
    return project_dir


def _safe_trash_segment(name: str) -> str:
    text = str(name or "project").strip()
    out = "".join(ch if ch.isalnum() or ch in " ._-" else "_" for ch in text)
    out = out.strip(" ._") or "project"
    return out[:80]


def _rmtree_with_retries(path: Path, *, attempts: int = 10, delay_sec: float = 0.45) -> None:
    path = Path(path)
    last_exc: Exception | None = None

    def _onerror(func, path_str, exc_info):
        try:
            os.chmod(path_str, 0o700)
            func(path_str)
        except Exception:
            raise

    for attempt in range(attempts):
        if not path.exists():
            return
        try:
            shutil.rmtree(path, onerror=_onerror)
            return
        except FileNotFoundError:
            return
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(delay_sec)

    if last_exc:
        raise last_exc


def _delete_path_in_background(path: Path, label: str) -> None:
    def _worker():
        try:
            _rmtree_with_retries(path)
            logger.info("Deleted project trash: %s", label)
        except Exception as exc:
            logger.exception("Project trash delete failed for %s: %s", label, exc)

    threading.Thread(target=_worker, daemon=True, name=f"delete-{label[:24]}").start()


def _mark_delete_pending(project_dir: Path) -> None:
    try:
        (project_dir / ".delete_pending").write_text(
            datetime.now().isoformat(timespec="seconds"),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("delete pending marker failed for %s: %s", project_dir, exc)


def _resolve_clips_json_path(video_dir: Path) -> Path:
    """
    JSON со списком выбранных LLM клипов: `{имя_папки}_clips.json`,
    иначе самый свежий `*_clips.json` (старые/переименованные проекты).
    """
    primary = video_dir / f"{video_dir.name}_clips.json"
    if primary.exists():
        return primary
    cands = sorted(
        video_dir.glob("*_clips.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return cands[0] if cands else primary


def _zip_skip_relative(rel: Path) -> bool:
    """Не класть в архив служебные каталоги (.thumbs, .reburn_*, __pycache__)."""
    for part in rel.parts:
        if part.startswith("."):
            return True
        if part == "__pycache__":
            return True
    return False


def _unlink_quiet(path: str | Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _build_project_zip_file(
    project_dir: Path,
    video_id: str,
    *,
    final_only: bool,
) -> Path:
    """Собрать zip во временный файл; вызывающий удаляет через BackgroundTask."""
    temp_dir = BASE_DIR / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="projzip_", suffix=".zip", dir=str(temp_dir))
    os.close(fd)
    root_arc = Path(video_id)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            if final_only:
                base = project_dir / "clips_final"
                if not base.is_dir():
                    raise FileNotFoundError("Нет папки clips_final")
                arc_prefix = root_arc / "clips_final"
                for path in sorted(base.rglob("*")):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(base)
                    if _zip_skip_relative(rel):
                        continue
                    zf.write(path, (arc_prefix / rel).as_posix())
            else:
                for path in sorted(project_dir.rglob("*")):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(project_dir)
                    if _zip_skip_relative(rel):
                        continue
                    zf.write(path, (root_arc / rel).as_posix())
        return Path(tmp_path)
    except Exception:
        _unlink_quiet(tmp_path)
        raise


@app.get("/api/projects/{video_id}/metadata-doc")
def download_project_metadata_doc(
    video_id: str,
    subdir: str = "clips_final",
    include_tags: bool = False,
    export_format: str = Query("xlsx", alias="format"),
):
    """
    Экспорт titre + description по всем клипам: Excel (.xlsx) по умолчанию или Markdown (?format=md).
    subdir: clips_final | clips_subtitles_ok | clips_raw
    """
    if not _safe_path_segment(video_id) or not _safe_path_segment(subdir):
        raise HTTPException(400, "Некорректный video_id или subdir")
    allowed = frozenset({"clips_final", "clips_subtitles_ok", "clips_raw"})
    if subdir not in allowed:
        raise HTTPException(400, f"subdir: одно из {', '.join(sorted(allowed))}")
    fmt = (export_format or "xlsx").strip().lower()
    if fmt not in ("xlsx", "md", "markdown"):
        raise HTTPException(400, "format: xlsx или md")
    if fmt == "markdown":
        fmt = "md"

    config = _load_config()
    project_dir = _output_dir(config) / video_id
    if not project_dir.is_dir():
        raise HTTPException(404, "Проект не найден")

    from modules import export_metadata_doc as export_metadata_doc_mod

    if fmt == "md":
        try:
            text = export_metadata_doc_mod.build_metadata_markdown(
                project_dir,
                clips_subdir=subdir,
                include_tags=include_tags,
            )
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        fname = f"{video_id}_metadata_titles_descriptions.md"
        return Response(
            content=text.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    try:
        data = export_metadata_doc_mod.build_metadata_xlsx_bytes(
            project_dir,
            clips_subdir=subdir,
            include_tags=include_tags,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ImportError as e:
        raise HTTPException(503, str(e)) from e

    fname = f"{video_id}_metadata_titles_descriptions.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/projects/{video_id}/download-zip")
def download_project_zip(
    video_id: str,
    final_only: bool = Query(
        False,
        description="Только clips_final (меньше размер; без raw и json в корне)",
    ),
):
    """Скачать весь проект одним .zip (внутри папка с именем video_id)."""
    if not _safe_path_segment(video_id):
        raise HTTPException(400, "Некорректный video_id")
    config = _load_config()
    out_root = _output_dir(config).resolve()
    project_dir = (out_root / video_id).resolve()
    try:
        project_dir.relative_to(out_root)
    except ValueError:
        raise HTTPException(400, "Некорректный путь") from None
    if not project_dir.is_dir():
        raise HTTPException(404, "Проект не найден")
    try:
        tmp_zip = _build_project_zip_file(project_dir, video_id, final_only=final_only)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        logger.exception("Сборка ZIP проекта %s: %s", video_id, e)
        raise HTTPException(500, f"Не удалось собрать архив: {e}") from e

    fname = f"{video_id}_clips_final.zip" if final_only else f"{video_id}_project.zip"
    return FileResponse(
        path=str(tmp_zip),
        filename=fname,
        media_type="application/zip",
        background=BackgroundTask(_unlink_quiet, str(tmp_zip)),
    )


@app.post("/api/projects/{video_id}/open-folder")
def open_project_folder(video_id: str, subdir: str = Query("project")):
    """Открыть локальную папку проекта в проводнике/файловом менеджере."""
    config = _load_config()
    project_dir = _resolve_output_project_dir(config, video_id)
    allowed = {
        "project": project_dir,
        "clips_final": project_dir / "clips_final",
        "clips_raw": project_dir / "clips_raw",
    }
    target = allowed.get(subdir)
    if target is None:
        raise HTTPException(400, "subdir: project, clips_final или clips_raw")
    if not target.is_dir():
        raise HTTPException(404, f"Папка не найдена: {subdir}")

    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as exc:
        logger.exception("Open project folder failed: %s", exc)
        raise HTTPException(500, f"Не удалось открыть папку: {exc}") from exc

    return {"ok": True, "path": str(target)}


# ─────────────────────────────────────────────────────────────────
# Clips API
# ─────────────────────────────────────────────────────────────────
@app.get("/api/clips")
def list_clips():
    config = _load_config()
    output_dir = _output_dir(config)

    clips = []
    if not output_dir.exists():
        return clips

    # Опубликованные клипы (есть youtube_id) для подсчёта по проекту.
    # Считаем по filename, а не по title: заголовки могут повторяться между проектами.
    try:
        schedule = _load_schedule()
        published_filenames: set[str] = {
            str(e.get("filename", "")).strip()
            for e in schedule
            if e.get("youtube_id") and e.get("filename")
        }
    except Exception:
        published_filenames = set()

    def _project_ctime(path: Path) -> float:
        try:
            return path.stat().st_ctime
        except OSError:
            return 0.0

    for video_dir in sorted(output_dir.iterdir(), key=_project_ctime, reverse=True):
        if not video_dir.is_dir():
            continue
        if video_dir.name.startswith(".") or (video_dir / ".delete_pending").exists():
            continue
        final_dir = video_dir / "clips_final"
        if not final_dir.exists():
            continue
        proj_created_at = datetime.fromtimestamp(video_dir.stat().st_ctime).isoformat()

        # Исходное имя файла из project.json (сохраняется при обработке)
        # Для новых проектов — имя загруженного файла; для старых — video_id как fallback
        source_name: str | None = None
        project_language = ""
        proj_json = video_dir / "project.json"
        if proj_json.exists():
            try:
                pdata = json.loads(proj_json.read_text(encoding="utf-8"))
                source_name = pdata.get("source_title") or None
                project_language = (pdata.get("language") or "").strip()
                if not source_name:
                    raw = pdata.get("source_filename", "")
                    source_name = Path(raw).stem if raw else None
            except Exception:
                pass
        if not source_name:
            source_name = video_dir.name

        # Fallback-данные по клипам проекта (если *_meta.json отсутствует)
        fallback_clips_meta: list[dict] = []
        clips_meta_path = _resolve_clips_json_path(video_dir)
        if clips_meta_path.exists():
            try:
                data = json.loads(clips_meta_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    fallback_clips_meta = data
            except Exception:
                pass

        def _fallback_title_for(mp4_stem: str) -> str:
            # clip_001 -> 0
            m = re.search(r"(\d+)$", mp4_stem)
            idx = int(m.group(1)) - 1 if m else -1
            if 0 <= idx < len(fallback_clips_meta):
                row = fallback_clips_meta[idx] or {}
                hook = str(row.get("hook") or "").strip()
                txt = str(row.get("text") or "").strip()
                if hook:
                    return hook[:100]
                if txt:
                    clean = re.sub(r"\s+", " ", txt).strip(" .")
                    if clean:
                        return clean[:100]
            base = source_name or video_dir.name
            return f"{base} — {mp4_stem}"

        def _fallback_clip_meta_for(mp4_stem: str) -> dict:
            m = re.search(r"(\d+)$", mp4_stem)
            idx = int(m.group(1)) - 1 if m else -1
            if 0 <= idx < len(fallback_clips_meta):
                row = fallback_clips_meta[idx]
                return row if isinstance(row, dict) else {}
            return {}

        # Один проход: читаем мету, считаем опубликованные, собираем клипы
        proj_clips = []
        published_count = 0
        mp4_files = sorted(final_dir.glob("*.mp4"))
        project_sort_ts = video_dir.stat().st_ctime
        if mp4_files:
            try:
                project_sort_ts = max(project_sort_ts, max(p.stat().st_mtime for p in mp4_files))
            except Exception:
                pass
        project_sort_at = datetime.fromtimestamp(project_sort_ts).isoformat()

        for mp4 in mp4_files:
            meta = _read_meta(final_dir / f"{mp4.stem}_meta.json")
            fallback_clip_meta = _fallback_clip_meta_for(mp4.stem)
            # Надёжный матч только по имени с префиксом проекта:
            # "{project_id}_{clip}.mp4". Сырые "clip_001.mp4" у разных проектов
            # совпадают и дают ложные "Размещено".
            queue_filename = f"{video_dir.name}_{mp4.name}"
            if queue_filename in published_filenames:
                published_count += 1
            lang = project_language or (meta.get("language") or "").strip()
            title = str(meta.get("title") or "").strip()
            if not title or title == mp4.stem:
                title = _fallback_title_for(mp4.stem)
            proj_clips.append({
                "video_id": video_dir.name,
                "clip_name": mp4.stem,
                "url": f"/api/clips/video/{video_dir.name}/{mp4.name}",
                "poster_url": f"/api/clips/poster/{video_dir.name}/{mp4.name}",
                "language": lang,
                "title": title,
                "description": meta.get("description", ""),
                "tags": meta.get("tags", []),
                "viral_score": _viral_score_payload(meta, fallback_clip_meta),
                "size_mb": round(mp4.stat().st_size / 1024 / 1024, 1),
                "project_created_at": proj_created_at,
                "project_sort_at": project_sort_at,
                "project_folder": video_dir.name,
                "project_path": str(video_dir),
                "clips_final_path": str(final_dir),
                "source_name": source_name,
                "published_count": 0,  # заполним ниже
            })
        for c in proj_clips:
            c["published_count"] = published_count
        clips.extend(proj_clips)

    return clips


@app.delete("/api/clips/{video_id}/{clip_id}")
def delete_clip(video_id: str, clip_id: str):
    """Удалить один клип (mp4 + meta.json)."""
    config = _load_config()
    output_dir = _output_dir(config)
    clip_dir = output_dir / video_id / "clips_final"
    deleted = []
    thumb = clip_dir / ".thumbs" / f"{clip_id}.jpg"
    if thumb.exists():
        try:
            thumb.unlink()
        except Exception:
            pass
    for ext in (".mp4", "_meta.json", ".txt", "_youtube.txt"):
        p = clip_dir / f"{clip_id}{ext}"
        if p.exists():
            p.unlink()
            deleted.append(p.name)
    if not deleted:
        raise HTTPException(404, "Клип не найден")
    return {"ok": True, "deleted": deleted}


class ProjectDeleteManyRequest(BaseModel):
    video_ids: list[str]


def _delete_output_project_dir(config: dict, video_id: str) -> dict:
    """Удалить целую папку проекта из output/ с защитой от выхода за root."""
    project_dir = _resolve_output_project_dir(config, video_id)
    project_id = project_dir.name
    if project_id.startswith("."):
        raise HTTPException(400, "Некорректное имя проекта")

    output_dir = _output_dir(config).resolve()
    trash_root = output_dir / ".deleted_projects"
    trash_root.mkdir(parents=True, exist_ok=True)
    trash_dir = trash_root / (
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
        f"{uuid.uuid4().hex[:8]}_{_safe_trash_segment(project_id)}"
    )

    _mark_delete_pending(project_dir)
    try:
        project_dir.rename(trash_dir)
        _delete_path_in_background(trash_dir, project_id)
        return {"video_id": project_id, "delete_mode": "background"}
    except Exception as exc:
        logger.warning("Move project to trash failed for %s: %s", project_id, exc)
        _delete_path_in_background(project_dir, project_id)
        return {"video_id": project_id, "delete_mode": "pending"}


@app.delete("/api/projects/{video_id}")
def delete_project(video_id: str):
    """Удалить весь проект из output/<video_id>/."""
    config = _load_config()
    _guard_no_processing("Удаление проекта")
    try:
        deleted = _delete_output_project_dir(config, video_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("delete project failed: %s", video_id)
        raise HTTPException(500, f"Не удалось удалить проект: {exc}") from exc
    return {"ok": True, "deleted": [deleted]}


@app.post("/api/projects/actions/delete")
def delete_many_projects(body: ProjectDeleteManyRequest):
    """Удалить несколько проектов из сетки «Проекты»."""
    config = _load_config()
    _guard_no_processing("Удаление проектов")
    seen: set[str] = set()
    video_ids: list[str] = []
    for raw_id in body.video_ids:
        video_id = str(raw_id or "").strip()
        if video_id and video_id not in seen:
            seen.add(video_id)
            video_ids.append(video_id)
    if not video_ids:
        raise HTTPException(400, "Не выбраны проекты")

    deleted: list[dict] = []
    errors: list[dict] = []
    for video_id in video_ids:
        try:
            deleted.append(_delete_output_project_dir(config, video_id))
        except HTTPException as exc:
            errors.append({"video_id": video_id, "error": str(exc.detail)})
        except Exception as exc:
            logger.exception("delete project failed: %s", video_id)
            errors.append({"video_id": video_id, "error": str(exc)})

    return {
        "ok": not errors,
        "deleted": deleted,
        "deleted_count": len(deleted),
        "errors": errors,
    }


@app.delete("/api/clips/{video_id}")
def delete_video_clips(video_id: str):
    """Удалить весь проект одного видео (legacy endpoint для старого UI)."""
    config = _load_config()
    _guard_no_processing("Удаление проекта")
    try:
        deleted = _delete_output_project_dir(config, video_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("delete project failed: %s", video_id)
        raise HTTPException(500, f"Не удалось удалить проект: {exc}") from exc
    return {"ok": True, "deleted": [deleted]}


def _clips_final_mp4_path(config: dict, video_id: str, filename: str) -> Path | None:
    """Безопасный путь к mp4 в clips_final или None."""
    if not filename or not filename.lower().endswith(".mp4"):
        return None
    if ".." in filename or "/" in filename or "\\" in filename:
        return None
    output_dir = _output_dir(config)
    path = output_dir / video_id / "clips_final" / filename
    if not path.is_file():
        return None
    return path


def _clip_poster_cache_path(mp4_path: Path) -> Path:
    return mp4_path.parent / ".thumbs" / f"{mp4_path.stem}.jpg"


def _ensure_clip_poster_jpeg(mp4_path: Path, cache_path: Path) -> bool:
    """Один кадр из mp4 → JPEG в .thumbs/ (кэш)."""
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return True
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", "0.5",
        "-i", str(mp4_path),
        "-vframes", "1",
        "-q:v", "4",
        "-y",
        str(cache_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode != 0 or not cache_path.exists():
            logger.warning("poster: ffmpeg failed for %s: %s", mp4_path, (r.stderr or b"")[:300])
            return False
        return cache_path.stat().st_size > 0
    except Exception as exc:
        logger.warning("poster: %s", exc)
        return False


@app.get("/api/clips/poster/{video_id}/{filename}")
def serve_clip_poster(video_id: str, filename: str):
    """JPEG-превью кадра из клипа; кэш в clips_final/.thumbs/{stem}.jpg"""
    config = _load_config()
    mp4_path = _clips_final_mp4_path(config, video_id, filename)
    if not mp4_path:
        raise HTTPException(404, "Клип не найден")
    cache_path = _clip_poster_cache_path(mp4_path)
    if not _ensure_clip_poster_jpeg(mp4_path, cache_path):
        raise HTTPException(503, "Не удалось создать превью")
    return FileResponse(str(cache_path), media_type="image/jpeg")


@app.get("/api/clips/video/{video_id}/{filename}")
def serve_clip(video_id: str, filename: str):
    config = _load_config()
    path = _clips_final_mp4_path(config, video_id, filename)
    if not path:
        raise HTTPException(404, "Клип не найден")
    return FileResponse(str(path), media_type="video/mp4",
                        headers={"Accept-Ranges": "bytes"})


# ─────────────────────────────────────────────────────────────────
# Queue API
# ─────────────────────────────────────────────────────────────────
@app.get("/api/queue")
def queue_status():
    config = _load_config()
    queue_dir = _queue_dir(config)
    channels = config.get("channels", {})
    try:
        from modules.queue_manager import get_queue_status
        return get_queue_status(queue_dir, channels)
    except Exception as e:
        logger.warning(f"queue_status error: {e}")
        return {}


@app.get("/api/queue/{channel}/clips")
def get_channel_queue_clips(channel: str):
    """Список клипов в очереди канала с заголовками."""
    config = _load_config()
    queue_dir = _queue_dir(config)
    ch_dir = queue_dir / channel
    if not ch_dir.exists():
        return []
    clips = []
    for mp4 in sorted(ch_dir.glob("*.mp4")):
        meta_path = ch_dir / f"{mp4.stem}_meta.json"
        if not meta_path.exists():
            meta_path = mp4.with_suffix(".json")
        meta = _read_meta(meta_path)
        row = {
            "filename": mp4.name,
            "stem": mp4.stem,
            "title": meta.get("title", mp4.stem),
            "path": str(mp4),
            "folder": str(ch_dir),
        }
        row.update(_queue_manual_upload_payload(mp4, meta))
        clips.append(row)
    return clips


@app.delete("/api/queue/{channel}")
def clear_channel_queue(channel: str):
    config = _load_config()
    queue_dir = _queue_dir(config)
    try:
        from modules.queue_manager import clear_queue
        clear_queue(channel, queue_dir)
    except Exception as e:
        raise HTTPException(500, str(e))
    entries = _load_schedule()
    entries = [e for e in entries if e.get("channel") != channel]
    _save_schedule(entries)
    return {"ok": True}


@app.get("/api/queue/dashboard")
def queue_dashboard():
    """Детальный статус очереди по каждому каналу: pending + scheduled + uploaded_today."""
    config    = _load_config()
    queue_dir = _queue_dir(config)
    channels  = config.get("channels", {})
    schedule_entries = _load_schedule()
    today = datetime.now(timezone.utc).date().isoformat()
    manual_upload_mode = _manual_upload_mode(config)

    result = []
    for ch_name, ch_cfg in channels.items():
        ch_dir = queue_dir / ch_name
        pending = len(list(ch_dir.glob("*.mp4"))) if ch_dir.exists() else 0

        ch_entries = [e for e in schedule_entries if e.get("channel") == ch_name]
        scheduled  = sum(1 for e in ch_entries if e.get("status") == "scheduled")
        uploading  = sum(1 for e in ch_entries if e.get("status") == "uploading")
        # next_day = не загружено, дата публикации ещё не прошла (ждёт следующего окна)
        # failed   = настоящая ошибка: дата уже прошла или дата не указана
        next_day   = sum(
            1 for e in ch_entries
            if _is_actionable_failed(e, queue_dir) and e.get("publish_date", "") >= today
        )
        failed     = sum(
            1 for e in ch_entries
            if _is_actionable_failed(e, queue_dir) and e.get("publish_date", "") < today
        )
        uploaded_today = _count_uploads_today(ch_name, ch_entries)
        limit_info = _channel_upload_limit(config, ch_name)
        max_per_day = limit_info.get("effective_max_uploads_per_day")
        # Ближайшая дата публикации
        future = sorted(
            [e.get("publish_date","") for e in ch_entries
             if e.get("status") == "scheduled" and e.get("publish_date","") >= today]
        )
        next_publish = future[0] if future else None

        result.append({
            "channel":        ch_name,
            "language":       _normalize_language(ch_cfg.get("language", ""), ch_name),
            "manual_upload_mode": manual_upload_mode,
            "folder":         str(ch_dir),
            "pending":        pending,
            "scheduled":      scheduled,
            "uploading":      uploading,
            "next_day":       next_day,
            "failed":         failed,
            "uploaded_today": uploaded_today,
            "max_per_day":    max_per_day,
            "base_max_per_day": limit_info.get("base_max_uploads_per_day"),
            "comment_quota_protected": limit_info.get("reason") == "comments_active",
            "comments_total": limit_info.get("comments_total", 0),
            "next_publish":   next_publish,
            "total":          pending + scheduled,
            "schedule_utc":   ch_cfg.get("schedule_utc", []),
        })

    return result


@app.get("/api/planning")
def planning_dashboard(
    target_days: int = Query(14, ge=1, le=120),
    daily_per_channel: Optional[int] = Query(None, ge=1, le=50),
):
    """Predictive stock dashboard: what language should be cut next."""
    config = _load_config()
    from modules.planning import build_planning_dashboard
    return build_planning_dashboard(
        config=config,
        queue_dir=_queue_dir(config),
        schedule_entries=_load_schedule(),
        target_days=target_days,
        daily_per_channel=daily_per_channel,
        base_dir=BASE_DIR,
    )


@app.post("/api/cleanup/run")
def api_cleanup_run(dry_run: bool = Query(False, description="Только показать, что будет удалено")):
    """Ручной запуск очистки диска (см. config cleanup). Принудительно включает cleanup на один запуск."""
    cfg = dict(_load_config())
    cleanup = dict(cfg.get("cleanup") or {})
    cleanup["enabled"] = True
    if dry_run:
        cleanup["dry_run"] = True
    cfg["cleanup"] = cleanup
    from modules.cleanup_uploaded import run_cleanup_safe
    return run_cleanup_safe(cfg, BASE_DIR)


@app.post("/api/clips/cleanup-published")
def api_cleanup_published(dry_run: bool = Query(False)):
    """Удалить папку clips_final у проектов, все клипы которых полностью опубликованы на YouTube."""
    config = _load_config()
    output_dir = _output_dir(config)

    try:
        schedule = _load_schedule()
        published_filenames: set[str] = {
            str(e.get("filename", "")).strip()
            for e in schedule
            if e.get("youtube_id") and e.get("filename")
        }
    except Exception:
        published_filenames = set()

    stats: dict = {"projects_cleaned": 0, "bytes_freed": 0, "mb_freed": 0.0, "projects": [], "dry_run": dry_run}

    if not output_dir.exists():
        return stats

    for video_dir in sorted(output_dir.iterdir(), key=lambda d: d.stat().st_ctime, reverse=True):
        if not video_dir.is_dir():
            continue
        final_dir = video_dir / "clips_final"
        if not final_dir.exists():
            continue
        mp4_files = list(final_dir.glob("*.mp4"))
        if not mp4_files:
            continue
        total = len(mp4_files)
        published_count = sum(
            1 for mp4 in mp4_files
            if f"{video_dir.name}_{mp4.name}" in published_filenames
        )
        if published_count < total:
            continue  # не все опубликованы — пропуск

        sz = sum(f.stat().st_size for f in final_dir.rglob("*") if f.is_file())
        entry = {"video_id": video_dir.name, "clips": total, "mb": round(sz / 1024 / 1024, 1)}
        if not dry_run:
            try:
                shutil.rmtree(str(final_dir))
            except Exception as ex:
                logger.warning("cleanup-published: rmtree %s: %s", final_dir, ex)
                continue
        stats["projects"].append(entry)
        stats["projects_cleaned"] += 1
        stats["bytes_freed"] += sz

    stats["mb_freed"] = round(stats["bytes_freed"] / 1024 / 1024, 1)
    return stats


class QueueAddRequest(BaseModel):
    clips: list[dict]   # [{video_id: str, clip_id: str}, ...]
    channel: str


class QueueFromProjectRequest(BaseModel):
    video_id: str
    channel: str


def _meta_path_clips_final(final_dir: Path, clip_stem: str) -> Path:
    """{stem}_meta.json или {stem}.json — как в _read_meta / uploader."""
    p = final_dir / f"{clip_stem}_meta.json"
    if p.exists():
        return p
    p2 = final_dir / f"{clip_stem}.json"
    if p2.exists():
        return p2
    return final_dir / f"{clip_stem}_meta.json"


@app.post("/api/queue/add")
def add_clips_to_queue(body: QueueAddRequest):
    """Добавить выбранные клипы в очередь YouTube-канала."""
    config = _load_config()
    output_dir = _output_dir(config)
    queue_dir = _queue_dir(config)
    channels = config.get("channels", {})

    if body.channel not in channels:
        raise HTTPException(400, f"Канал '{body.channel}' не найден в config.yaml")

    from modules.queue_manager import add_to_queue

    added = []
    errors = []
    for clip in body.clips:
        video_id = clip.get("video_id", "")
        clip_id = clip.get("clip_id", "")
        if not video_id or not clip_id:
            continue
        final_dir = output_dir / video_id / "clips_final"
        video_path = final_dir / f"{clip_id}.mp4"
        meta_path = _meta_path_clips_final(final_dir, clip_id)
        if not video_path.exists():
            errors.append(f"{clip_id}: файл не найден")
            continue
        if not meta_path.exists():
            errors.append(f"{clip_id}: нет метаданных (json)")
            continue
        mismatch = _clip_language_mismatch_message(
            _read_meta(meta_path),
            body.channel,
            channels[body.channel],
            video_path.name,
        )
        if mismatch:
            errors.append(f"{clip_id}: {mismatch}")
            continue
        try:
            add_to_queue(video_path, meta_path, body.channel, queue_dir, channels[body.channel])
            added.append(clip_id)
        except Exception as exc:
            errors.append(f"{clip_id}: {exc}")

    return {"ok": True, "added": len(added), "errors": errors}


@app.post("/api/queue/from-project")
def add_project_clips_to_queue(body: QueueFromProjectRequest):
    """Добавить все клипы из output/<video_id>/clips_final/ в очередь (надёжнее, чем огромный JSON со списком)."""
    config = _load_config()
    output_dir = _output_dir(config)
    queue_dir = _queue_dir(config)
    channels = config.get("channels", {})

    if body.channel not in channels:
        raise HTTPException(400, f"Канал '{body.channel}' не найден в config.yaml")

    final_dir = output_dir / body.video_id / "clips_final"
    if not final_dir.is_dir():
        raise HTTPException(404, f"Папка clips_final не найдена: {body.video_id}")

    from modules.queue_manager import add_to_queue

    added: list[str] = []
    errors: list[str] = []
    for mp4 in sorted(final_dir.glob("*.mp4")):
        stem = mp4.stem
        meta_path = _meta_path_clips_final(final_dir, stem)
        if not meta_path.exists():
            errors.append(f"{mp4.name}: нет метаданных (json)")
            continue
        mismatch = _clip_language_mismatch_message(
            _read_meta(meta_path),
            body.channel,
            channels[body.channel],
            mp4.name,
        )
        if mismatch:
            errors.append(f"{mp4.name}: {mismatch}")
            continue
        try:
            add_to_queue(mp4, meta_path, body.channel, queue_dir, channels[body.channel])
            added.append(stem)
        except Exception as exc:
            errors.append(f"{mp4.name}: {exc}")

    return {"ok": True, "added": len(added), "errors": errors}


class QueueFromProjectsRequest(BaseModel):
    video_ids: List[str]
    channel: str


@app.post("/api/queue/from-projects")
def add_many_projects_to_queue(body: QueueFromProjectsRequest):
    """Добавить в очередь все клипы из нескольких проектов (сетка «Проекты»)."""
    ids = list(dict.fromkeys([x.strip() for x in body.video_ids if x and str(x).strip()]))
    if not ids:
        raise HTTPException(400, "Список проектов (video_ids) пуст")
    if len(ids) > 300:
        raise HTTPException(400, "Не более 300 проектов за один запрос — разбейте на части")

    config = _load_config()
    output_dir = _output_dir(config)
    queue_dir = _queue_dir(config)
    channels = config.get("channels", {})

    if body.channel not in channels:
        raise HTTPException(400, f"Канал '{body.channel}' не найден в config.yaml")

    from modules.queue_manager import add_to_queue

    total_added = 0
    all_errors: list[str] = []

    for video_id in ids:
        final_dir = output_dir / video_id / "clips_final"
        if not final_dir.is_dir():
            all_errors.append(f"{video_id}: нет папки clips_final")
            continue
        for mp4 in sorted(final_dir.glob("*.mp4")):
            stem = mp4.stem
            meta_path = _meta_path_clips_final(final_dir, stem)
            if not meta_path.exists():
                all_errors.append(f"{video_id}/{mp4.name}: нет метаданных (json)")
                continue
            mismatch = _clip_language_mismatch_message(
                _read_meta(meta_path),
                body.channel,
                channels[body.channel],
                mp4.name,
            )
            if mismatch:
                all_errors.append(f"{video_id}/{mp4.name}: {mismatch}")
                continue
            try:
                add_to_queue(mp4, meta_path, body.channel, queue_dir, channels[body.channel])
                total_added += 1
            except Exception as exc:
                all_errors.append(f"{video_id}/{mp4.name}: {exc}")

    return {"ok": True, "added": total_added, "errors": all_errors, "projects": len(ids)}


# ─────────────────────────────────────────────────────────────────
# Calendar / Schedule tracking (schedule.json)
# ─────────────────────────────────────────────────────────────────
_SCHEDULE_FILE = BASE_DIR / "queue" / "schedule.json"
# Общий lock с uploader.py через schedule_store — предотвращает race conditions
_schedule_lock = schedule_store._lock


def _load_schedule_raw() -> list:
    """Прочитать расписание без захвата лока (вызывать только внутри _schedule_lock)."""
    return schedule_store.load(_SCHEDULE_FILE)


def _save_schedule_raw(entries: list):
    """Записать расписание без захвата лока (вызывать только внутри _schedule_lock)."""
    schedule_store.save(_SCHEDULE_FILE, entries)


def _load_schedule() -> list:
    with _schedule_lock:
        return _load_schedule_raw()


def _save_schedule(entries: list):
    with _schedule_lock:
        _save_schedule_raw(entries)


def _update_schedule_entry(entry_id: str, **kwargs):
    """Thread-safe обновление одной записи расписания."""
    with _schedule_lock:
        entries = _load_schedule_raw()
        for entry in entries:
            if entry["id"] == entry_id:
                entry.update(kwargs)
                break
        _save_schedule_raw(entries)


def _remove_schedule_entry(entry_id: str):
    """Thread-safe удаление записи расписания (recoverable-ошибки: quota/limit)."""
    with _schedule_lock:
        entries = _load_schedule_raw()
        _save_schedule_raw([e for e in entries if e.get("id") != entry_id])


def _count_uploads_today(channel: str, entries: Optional[list] = None) -> int:
    """Сколько видео уже загружено на YouTube сегодня (UTC) для канала."""
    today = datetime.now(timezone.utc).date().isoformat()
    if entries is None:
        entries = _load_schedule()
    return sum(
        1 for e in entries
        if e.get("channel") == channel
        and e.get("created_at", "").startswith(today)
        and e.get("status") in ("uploading", "scheduled")
    )


def _remove_queue_files(video_path: Path, meta_path: Path) -> None:
    """Удалить видео и мета-файл из папки очереди."""
    video_path.unlink(missing_ok=True)
    if meta_path.exists():
        meta_path.unlink(missing_ok=True)
    (video_path.with_name(f"{video_path.stem}_youtube.txt")).unlink(missing_ok=True)
    video_path.with_suffix(".txt").unlink(missing_ok=True)


def _queue_video_exists(entry: dict, queue_dir: Path) -> bool:
    channel = str(entry.get("channel", ""))
    filename = str(entry.get("filename", ""))
    return bool(channel and filename and (queue_dir / channel / filename).exists())


def _is_actionable_failed(entry: dict, queue_dir: Path) -> bool:
    """Only count failed entries that still have a queue file to retry/fix."""
    return entry.get("status") == "failed" and _queue_video_exists(entry, queue_dir)


def _channel_upload_limit(config: dict, channel: str) -> dict:
    return effective_upload_limit(config, channel, base_dir=BASE_DIR)


def _active_upload_entries(entries: Optional[list] = None) -> list[dict]:
    try:
        schedule_entries = entries if entries is not None else _load_schedule()
    except Exception:
        schedule_entries = []
    return [e for e in schedule_entries if e.get("status") == "uploading"]


def _job_source_count(params: dict) -> int:
    for key in ("batch_urls", "batch_input_paths"):
        items = params.get(key)
        if isinstance(items, list) and items:
            return len(items)
    return 1 if (params.get("url") or params.get("input_path")) else 0


def _schedule_entry_created_ts(entry: dict) -> float:
    raw = str(entry.get("created_at") or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _issue_channel_row(channel: str, cfg: dict, extra: Optional[dict] = None) -> dict:
    row = {
        "channel": channel,
        "language": _normalize_language(cfg.get("language", ""), channel),
    }
    if extra:
        row.update(extra)
    return row


def _collect_attention_issues(config: dict, queue_counts: dict[str, int], entries: list) -> list[dict]:
    channels = config.get("channels", {}) or {}
    account_groups = config.get("account_groups", {}) or {}
    issues: list[dict] = []

    if _manual_upload_mode(config):
        return issues

    relevant_channels = {
        channel
        for channel, count in queue_counts.items()
        if count > 0
    }
    for entry in entries:
        if entry.get("status") in ("uploading", "failed"):
            ch = str(entry.get("channel") or "")
            if ch:
                relevant_channels.add(ch)

    by_auth_status: dict[str, list[dict]] = {
        "reauth_required": [],
        "missing_token": [],
        "missing_secret": [],
        "invalid_token": [],
    }
    for channel in sorted(relevant_channels):
        cfg = channels.get(channel)
        if not cfg:
            continue
        auth = _channel_auth_status(channel, cfg, account_groups)
        status = str(auth.get("status") or "")
        if status in by_auth_status:
            by_auth_status[status].append(_issue_channel_row(channel, cfg, {
                "pending": int(queue_counts.get(channel, 0)),
                "missing_scopes": auth.get("missing_scopes") or [],
            }))

    if by_auth_status["reauth_required"]:
        issues.append({
            "severity": "error",
            "code": "reauth_required",
            "title": "Обновите авторизацию YouTube API",
            "detail": "У этих каналов старый OAuth-токен или не хватает новых прав. Нажмите «Каналы → Обновить токен».",
            "channels": by_auth_status["reauth_required"],
        })
    if by_auth_status["missing_token"]:
        issues.append({
            "severity": "error",
            "code": "missing_token",
            "title": "Авторизуйте каналы YouTube",
            "detail": "Для этих каналов нет token-файла. Нажмите «Каналы → Авторизовать».",
            "channels": by_auth_status["missing_token"],
        })
    if by_auth_status["missing_secret"]:
        issues.append({
            "severity": "error",
            "code": "missing_secret",
            "title": "Добавьте client_secret.json",
            "detail": "Для этих каналов не найден файл Google Cloud client_secret.json.",
            "channels": by_auth_status["missing_secret"],
        })
    if by_auth_status["invalid_token"]:
        issues.append({
            "severity": "error",
            "code": "invalid_token",
            "title": "Проверьте token-файл YouTube",
            "detail": "Token-файл повреждён или не читается. Проще всего заново авторизовать канал.",
            "channels": by_auth_status["invalid_token"],
        })

    recent_failed = [
        e for e in entries
        if e.get("status") == "failed" and _schedule_entry_created_ts(e) >= time.time() - 36 * 3600
    ]
    if recent_failed:
        auth_failed = []
        quota_failed = []
        upload_limit_failed = []
        network_failed = []
        other_failed = []
        for entry in recent_failed:
            channel = str(entry.get("channel") or "")
            cfg = channels.get(channel, {}) or {}
            err = str(entry.get("error") or "")
            low = err.lower()
            row = _issue_channel_row(channel, cfg, {"filename": entry.get("filename", ""), "error": err[:220]})
            if "invalid_scope" in low or "invalid_grant" in low or "unauthorized" in low or "insufficient" in low:
                auth_failed.append(row)
            elif "quotaexceeded" in low or "quota exceeded" in low:
                quota_failed.append(row)
            elif "uploadlimitexceeded" in low or "exceeded the number of videos" in low:
                upload_limit_failed.append(row)
            elif "getaddrinfo failed" in low or "failed to resolve" in low or "timeout" in low:
                network_failed.append(row)
            else:
                other_failed.append(row)
        if auth_failed:
            issues.append({
                "severity": "error",
                "code": "recent_auth_failed",
                "title": "Проверьте авторизацию YouTube API",
                "detail": "Свежие загрузки падали из-за прав/токена. Обновите токен для каналов.",
                "channels": auth_failed[:8],
            })
        if quota_failed:
            issues.append({
                "severity": "warning",
                "code": "quota_failed",
                "title": "Проверьте квоту Google Cloud API",
                "detail": "YouTube Data API вернул quotaExceeded. Нужен другой API-проект или ожидание сброса квоты.",
                "channels": quota_failed[:8],
            })
        if upload_limit_failed:
            issues.append({
                "severity": "warning",
                "code": "upload_limit_failed",
                "title": "Дневной лимит загрузок YouTube",
                "detail": "YouTube ограничил количество загрузок для канала. Файлы остаются в очереди до следующего окна.",
                "channels": upload_limit_failed[:8],
            })
        if network_failed:
            issues.append({
                "severity": "warning",
                "code": "network_failed",
                "title": "Проверьте доступ к Google API",
                "detail": "Были сетевые ошибки при обращении к Google OAuth/API.",
                "channels": network_failed[:8],
            })
        if other_failed:
            issues.append({
                "severity": "warning",
                "code": "upload_failed",
                "title": "Проверьте ошибки загрузки",
                "detail": "Есть свежие failed-записи в календаре загрузок.",
                "channels": other_failed[:8],
            })

    return issues


def _job_activity_payload(job: dict, config: dict) -> dict:
    params = job.get("params") or {}
    status = str(job.get("status") or "")
    artifacts = job.get("artifact_counts")
    if status in ("pending", "running") or not isinstance(artifacts, dict):
        artifacts = _collect_job_artifacts(job, config)

    return {
        "id": job.get("id", ""),
        "status": status,
        "created_at": job.get("created_at", ""),
        "source_count": _job_source_count(params),
        "lang": params.get("lang") or "auto",
        "add_to_queue": bool(params.get("add_to_queue")),
        "max_clips": params.get("max_clips"),
        "clips_created": int(artifacts.get("clips_created") or 0),
        "raw_clips": int(artifacts.get("raw_clips") or 0),
        "queued_added": int(artifacts.get("queued_added") or 0),
        "queued_channels": artifacts.get("queued_channels") or [],
        "projects": artifacts.get("projects") or [],
        "last_log_age_sec": round(time.time() - float(job.get("last_log_at") or time.time()), 1),
        "alert": _friendly_job_alert(job),
    }


@app.get("/api/activity/summary")
def activity_summary():
    """Compact high-level status for the process page under the live log."""
    config = _load_config()
    channels = config.get("channels", {}) or {}
    queue_dir = _queue_dir(config)
    queue_counts = _snapshot_queue_counts(queue_dir)
    queue_total = sum(queue_counts.values())
    uploads_enabled = _uploads_enabled(config)
    manual_upload_mode = not uploads_enabled

    with _jobs_lock:
        jobs = [dict(j) for j in _jobs.values()]

    active_jobs = [j for j in jobs if j.get("status") in ("pending", "running")]
    latest_job = max(jobs, key=lambda j: float(j.get("created_ts") or 0), default=None)
    latest_payload = _job_activity_payload(latest_job, config) if latest_job else None

    entries = _load_schedule()
    issues = _collect_attention_issues(config, queue_counts, entries)
    if active_jobs and latest_payload and latest_payload.get("alert"):
        issues.insert(0, {
            "severity": "warning",
            "code": "processing_stale",
            "title": "Нарезка не пишет новые строки",
            "detail": latest_payload.get("alert"),
            "channels": [],
        })
    today = datetime.now(timezone.utc).date().isoformat()
    uploading_entries = [] if manual_upload_mode else _active_upload_entries(entries)
    scheduled_today = [] if manual_upload_mode else [
        e for e in entries
        if e.get("status") == "scheduled" and str(e.get("created_at") or "").startswith(today)
    ]
    failed_today = [] if manual_upload_mode else [
        e for e in entries
        if e.get("status") == "failed" and str(e.get("created_at") or "").startswith(today)
    ]
    latest_job_ts = float(latest_job.get("created_ts") or 0) if latest_job else 0.0
    latest_upload_ts = max(
        (_schedule_entry_created_ts(e) for e in uploading_entries + scheduled_today + failed_today),
        default=0.0,
    )

    channel_rows: dict[str, dict] = {}

    def row_for(channel: str) -> dict:
        ch_cfg = channels.get(channel, {}) or {}
        return channel_rows.setdefault(channel, {
            "channel": channel,
            "language": _normalize_language(ch_cfg.get("language", ""), channel),
            "uploading": 0,
            "scheduled_today": 0,
            "failed_today": 0,
            "pending": int(queue_counts.get(channel, 0)),
        })

    for entry in uploading_entries:
        row_for(str(entry.get("channel") or "?"))["uploading"] += 1
    for entry in scheduled_today:
        row_for(str(entry.get("channel") or "?"))["scheduled_today"] += 1
    for entry in failed_today:
        row_for(str(entry.get("channel") or "?"))["failed_today"] += 1

    queue_rows = [
        {
            "channel": channel,
            "language": _normalize_language((channels.get(channel, {}) or {}).get("language", ""), channel),
            "pending": count,
        }
        for channel, count in sorted(queue_counts.items(), key=lambda item: item[1], reverse=True)
        if count > 0
    ]

    if active_jobs and uploading_entries:
        state = "mixed"
    elif active_jobs:
        state = "processing"
    elif uploading_entries:
        state = "uploading"
    elif scheduled_today and latest_upload_ts >= latest_job_ts:
        state = "upload_done"
    elif latest_job and latest_job.get("status") == "error":
        state = "error"
    elif latest_job and latest_job.get("status") == "cancelled":
        state = "cancelled"
    elif latest_job and latest_job.get("status") == "done":
        state = "done"
    elif scheduled_today:
        state = "upload_done"
    else:
        state = "idle"

    return {
        "state": state,
        "manual_upload_mode": manual_upload_mode,
        "uploads_enabled": uploads_enabled,
        "scheduler_enabled": _scheduler_enabled(config),
        "queue_dir": str(queue_dir),
        "busy": bool(active_jobs or uploading_entries),
        "can_start_processing": not active_jobs and not uploading_entries,
        "processing": {
            "active": bool(active_jobs),
            "active_count": len(active_jobs),
            "latest": latest_payload,
        },
        "uploads": {
            "active": bool(uploading_entries),
            "uploading": len(uploading_entries),
            "scheduled_today": len(scheduled_today),
            "failed_today": len(failed_today),
            "channels": sorted(
                channel_rows.values(),
                key=lambda row: (row["uploading"], row["scheduled_today"], row["pending"]),
                reverse=True,
            ),
        },
        "queue": {
            "pending": queue_total,
            "channels": queue_rows[:8],
        },
        "issues": issues,
    }


@app.get("/api/calendar")
def get_calendar():
    """Вернуть все записи календаря расписания."""
    return [e for e in _load_schedule() if e.get("status") != "interrupted"]


class CalendarAddRequest(BaseModel):
    channel: str
    filename: str       # имя mp4-файла в очереди канала
    publish_date: str   # YYYY-MM-DD
    publish_time: str   # HH:MM (UTC)


class CommentDraftRequest(BaseModel):
    channel: str
    comment_text: str
    video_title: str = ""
    russian_instruction: str = ""
    previous_reply: str = ""


class CommentTranslateRequest(BaseModel):
    comments: list[dict] = []


class CommentReplyRequest(BaseModel):
    channel: str
    parent_id: str
    text: str


class CommentStateItem(BaseModel):
    channel: str
    comment_id: str


class CommentStateRequest(BaseModel):
    action: str
    comments: list[CommentStateItem] = []


@app.post("/api/calendar/add")
def calendar_add(body: CalendarAddRequest):
    """Запланировать клип из очереди на конкретную дату и время."""
    _guard_no_processing("Загрузка в календарь")
    config = _load_config()
    _guard_uploads_enabled(config)
    channels = config.get("channels", {})
    if body.channel not in channels:
        raise HTTPException(400, f"Канал '{body.channel}' не найден")

    ch_cfg = channels[body.channel]
    account_groups = config.get("account_groups", {})
    google_account = ch_cfg.get("google_account", "")
    ag = account_groups.get(google_account, {})
    client_secret_p = BASE_DIR / ag.get("client_secret", "secrets/client_1.json")
    token_file = BASE_DIR / ch_cfg.get("token_file", f"tokens/{body.channel}.json")
    queue_dir = _queue_dir(config)
    ch_dir = queue_dir / body.channel
    video_path = ch_dir / body.filename
    if not video_path.exists():
        raise HTTPException(404, f"Файл '{body.filename}' не найден в очереди {body.channel}")

    meta_path = ch_dir / f"{video_path.stem}_meta.json"
    if not meta_path.exists():
        meta_path = video_path.with_suffix(".json")
    meta_dict = _read_meta(meta_path) or {"title": video_path.stem, "description": "", "tags": []}
    mismatch = _clip_language_mismatch_message(meta_dict, body.channel, ch_cfg, video_path.name)
    if mismatch:
        raise HTTPException(400, mismatch)
    title = meta_dict.get("title", video_path.stem)

    try:
        h_t, m_t = map(int, body.publish_time.split(":"))
        pub_dt = datetime.fromisoformat(body.publish_date).replace(
            hour=h_t, minute=m_t, second=0, microsecond=0, tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise HTTPException(400, f"Неверный формат даты/времени: {exc}")

    entry_id = str(uuid.uuid4())[:8]
    language = _normalize_language(ch_cfg.get("language", ""), body.channel)

    # Проверяем лимит загрузок в день (по дате загрузки, а не публикации)
    limit_info = _channel_upload_limit(config, body.channel)
    max_per_day = limit_info.get("effective_max_uploads_per_day")
    entries = _load_schedule()
    if max_per_day:
        already_today = _count_uploads_today(body.channel, entries)
        if already_today >= max_per_day:
            today_str = datetime.now(timezone.utc).date().isoformat()
            raise HTTPException(
                400,
                f"Лимит YouTube: не более {max_per_day} загрузок в сутки на канал «{body.channel}». "
                f"Сегодня ({today_str}) уже загружено {already_today}."
            )

    # Добавляем запись со статусом "uploading"
    entry: dict = {
        "id": entry_id,
        "channel": body.channel,
        "language": language,
        "filename": body.filename,
        "title": title,
        "publish_date": body.publish_date,
        "publish_time": body.publish_time,
        "status": "uploading",
        "youtube_id": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    entries.append(entry)
    _save_schedule(entries)

    def _upload():
        from modules.uploader import authenticate_channel, upload_video
        try:
            service = authenticate_channel(client_secret_p, token_file)
            vid_id = upload_video(service, video_path, meta_dict, publish_at=pub_dt)
            _update_schedule_entry(entry_id, status="scheduled", youtube_id=vid_id)
            schedule_store.clean_stale_failed(_SCHEDULE_FILE, body.channel, body.filename)
            _remove_queue_files(video_path, meta_path)
            logger.info(f"✓ Запланировано: {body.filename} → {body.publish_date} {body.publish_time} UTC · yt:{vid_id}")
        except Exception as exc:
            from modules.uploader import is_recoverable_queue_error

            if is_recoverable_queue_error(exc):
                _remove_schedule_entry(entry_id)
                logger.warning(f"Recoverable upload issue for {body.filename}; file stays in queue: {exc}")
                return
            _update_schedule_entry(entry_id, status="failed", error=str(exc))
            logger.error(f"✗ Ошибка планирования {body.filename}: {exc}")

    threading.Thread(target=_upload, daemon=True).start()
    return {
        "ok": True,
        "id": entry_id,
        "message": f"Загрузка запущена — публикация {body.publish_date} {body.publish_time} UTC",
    }


@app.delete("/api/calendar/{entry_id}")
def calendar_delete(entry_id: str):
    """Удалить запись из календаря."""
    entries = _load_schedule()
    entries = [e for e in entries if e["id"] != entry_id]
    _save_schedule(entries)
    return {"ok": True}


@app.post("/api/calendar/{entry_id}/retry")
def calendar_retry(entry_id: str):
    """Вернуть failed-клип в очередь: копирует оригинал из output в queue и удаляет запись."""
    entries = _load_schedule()
    entry = next((e for e in entries if e["id"] == entry_id), None)
    if not entry:
        raise HTTPException(404, "Запись не найдена")
    if entry.get("status") != "failed":
        raise HTTPException(400, "Повтор доступен только для записей со статусом failed")

    config = _load_config()
    channel = entry["channel"]
    filename = entry["filename"]
    stem = Path(filename).stem

    queue_dir = _queue_dir(config) / channel
    queue_dir.mkdir(parents=True, exist_ok=True)

    output_dir = _output_dir(config)
    found_mp4 = None
    found_meta = None
    for video_folder in output_dir.iterdir():
        candidate_mp4  = video_folder / "clips_final" / filename
        candidate_meta = video_folder / "clips_final" / f"{stem}_meta.json"
        if candidate_mp4.exists():
            found_mp4  = candidate_mp4
            found_meta = candidate_meta if candidate_meta.exists() else None
            break

    if not found_mp4:
        raise HTTPException(
            404,
            f"Оригинальный файл '{filename}' не найден в output/. "
            "Возможно, он был удалён — нарежьте видео заново."
        )

    shutil.copy2(found_mp4, queue_dir / filename)
    if found_meta:
        shutil.copy2(found_meta, queue_dir / f"{stem}_meta.json")
        try:
            from modules.manual_metadata import write_manual_upload_text, write_plain_upload_text

            meta = _read_meta(found_meta)
            queue_video = queue_dir / filename
            write_manual_upload_text(queue_video, meta)
            write_plain_upload_text(queue_video, meta)
        except Exception as exc:
            logger.warning("Manual upload text retry refresh failed for %s: %s", filename, exc)

    entries = [e for e in entries if e["id"] != entry_id]
    _save_schedule(entries)

    return {"ok": True, "message": f"'{filename}' возвращён в очередь {channel}"}


class ScheduleUploadRequest(BaseModel):
    channel: str
    start_date: str         # "2024-03-15" (ISO)
    per_day: int = 1        # 1-5
    slot_times: list[str]   # ["09:00", "15:00"] UTC, длина = per_day


@app.post("/api/schedule/upload")
def schedule_upload(body: ScheduleUploadRequest):
    """
    Загрузить все клипы из очереди канала на YouTube с отложенным расписанием.
    Видео загружаются сразу как private + publishAt, YouTube сам публикует в нужное время.
    """
    _guard_no_processing("Расписание загрузок")
    config = _load_config()
    _guard_uploads_enabled(config)
    channels = config.get("channels", {})
    if body.channel not in channels:
        raise HTTPException(400, f"Канал '{body.channel}' не найден в config.yaml")
    limit_info = _channel_upload_limit(config, body.channel)
    max_per_day = limit_info.get("effective_max_uploads_per_day") or 6
    if body.per_day < 1 or body.per_day > max_per_day:
        raise HTTPException(400, f"per_day должен быть 1–{max_per_day}")
    if len(body.slot_times) < body.per_day:
        raise HTTPException(400, f"Нужно {body.per_day} временных слотов")
    try:
        start = date_type.fromisoformat(body.start_date)
    except ValueError:
        raise HTTPException(400, "Неверный формат даты (нужен YYYY-MM-DD)")

    channel_config = channels[body.channel]
    account_groups = config.get("account_groups", {})
    google_account = channel_config.get("google_account", "")
    ag = account_groups.get(google_account, {})
    client_secret = BASE_DIR / ag.get("client_secret", "secrets/client_1.json")
    token_file = BASE_DIR / channel_config.get("token_file", f"tokens/{body.channel}.json")
    queue_dir = _queue_dir(config)
    ch_dir = queue_dir / body.channel

    mismatch_errors: list[str] = []
    if ch_dir.exists():
        for video_path in sorted(ch_dir.glob("*.mp4")):
            meta_path = ch_dir / f"{video_path.stem}_meta.json"
            if not meta_path.exists():
                meta_path = video_path.with_suffix(".json")
            meta = _read_meta(meta_path) or {}
            mismatch = _clip_language_mismatch_message(meta, body.channel, channel_config, video_path.name)
            if mismatch:
                mismatch_errors.append(mismatch)
    if mismatch_errors:
        raise HTTPException(400, "Найден клип не для этого канала: " + mismatch_errors[0])

    def _run():
        from modules.uploader import authenticate_channel, upload_video

        video_files = sorted(ch_dir.glob("*.mp4")) if ch_dir.exists() else []
        if not video_files:
            logger.warning(f"schedule_upload: очередь {body.channel} пуста")
            return

        logger.info(f"=== Расписание публикаций: {body.channel} ===")
        logger.info(f"  Клипов: {len(video_files)}, с {body.start_date}, "
                    f"{body.per_day}/день, слоты: {body.slot_times[:body.per_day]}")

        try:
            service = authenticate_channel(client_secret, token_file)
        except Exception as exc:
            logger.error(f"Ошибка авторизации {body.channel}: {exc}")
            return

        slot_times = body.slot_times[:body.per_day]
        current_date = body.start_date
        slot_idx = 0

        language = _normalize_language(channel_config.get("language", ""), body.channel)

        uploads_today = _count_uploads_today(body.channel)

        for video_path in video_files:
            if uploads_today >= max_per_day:
                logger.warning(
                    f"  ⚠ Лимит {max_per_day} загрузок/день достигнут для {body.channel}. "
                    f"Остаток очереди — завтра."
                )
                break

            meta_path = ch_dir / f"{video_path.stem}_meta.json"
            if not meta_path.exists():
                meta_path = video_path.with_suffix(".json")
            meta = _read_meta(meta_path) or {"title": video_path.stem, "description": "", "tags": []}
            mismatch = _clip_language_mismatch_message(meta, body.channel, channel_config, video_path.name)
            if mismatch:
                logger.error(mismatch)
                continue

            h_t, m_t = map(int, slot_times[slot_idx % body.per_day].split(":"))
            pub_dt = datetime.fromisoformat(current_date).replace(
                hour=h_t, minute=m_t, second=0, microsecond=0, tzinfo=timezone.utc
            )
            date_label = pub_dt.strftime("%d %b %H:%M UTC")
            pub_date_str = pub_dt.strftime("%Y-%m-%d")
            pub_time_str = pub_dt.strftime("%H:%M")

            # Создаём запись в calendar до загрузки
            entry_id = str(uuid.uuid4())[:8]
            cal_entry: dict = {
                "id": entry_id,
                "channel": body.channel,
                "language": language,
                "filename": video_path.name,
                "title": meta.get("title", video_path.stem),
                "publish_date": pub_date_str,
                "publish_time": pub_time_str,
                "status": "uploading",
                "youtube_id": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            cal_entries = _load_schedule()
            cal_entries.append(cal_entry)
            _save_schedule(cal_entries)

            logger.info(f"  Загрузка: {video_path.name} → {date_label}")
            try:
                vid_id = upload_video(service, video_path, meta, publish_at=pub_dt)
                _update_schedule_entry(entry_id, status="scheduled", youtube_id=vid_id)
                schedule_store.clean_stale_failed(_SCHEDULE_FILE, body.channel, video_path.name)
                _remove_queue_files(video_path, meta_path)
                uploads_today += 1
                logger.info(f"  ✓ youtube.com/watch?v={vid_id} · публикация: {date_label}")
            except Exception as exc:
                from modules.uploader import is_quota_error, is_recoverable_queue_error, is_upload_limit_error

                err_str = str(exc)
                is_quota = is_quota_error(exc)
                is_limit = is_upload_limit_error(exc)
                if is_recoverable_queue_error(exc):
                    _remove_schedule_entry(entry_id)
                    if is_quota:
                        reason = "Квота GCP исчерпана"
                    elif is_limit:
                        reason = "Лимит загрузок YouTube"
                    else:
                        reason = "Temporary upload issue"
                    logger.warning(f"  ⚠ {reason} — прерываем, файл остаётся в очереди")
                    break
                _update_schedule_entry(entry_id, status="failed", error=err_str)
                logger.error(f"  ✗ Ошибка {video_path.name}: {exc}")

            slot_idx += 1
            if slot_idx % body.per_day == 0:
                current_date = (date_type.fromisoformat(current_date) + timedelta(days=1)).isoformat()

        logger.info(f"=== Расписание для {body.channel} создано ===")

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "Загрузка по расписанию запущена в фоне"}


@app.post("/api/upload/now")
def upload_now():
    _guard_no_processing("Ручная загрузка YouTube")
    config = _load_config()
    _guard_uploads_enabled(config)

    def _run():
        try:
            from modules import uploader, quota_manager as qm_mod
            channels = config.get("channels", {})
            account_groups = config.get("account_groups", {})
            queue_dir = _queue_dir(config)
            schedule_file = queue_dir / "schedule.json"
            qm = qm_mod.QuotaManager(account_groups)
            for ch_name, ch_cfg in channels.items():
                if _processing_busy():
                    logger.warning("Ручная загрузка YouTube остановлена: началась нарезка видео")
                    break
                if not ch_cfg.get("channel_id"):
                    continue
                try:
                    limit_info = _channel_upload_limit(config, ch_name)
                    uploader.process_queue(
                        ch_name, ch_cfg, queue_dir,
                        quota_manager=qm, account_groups=account_groups,
                        max_per_day=limit_info.get("effective_max_uploads_per_day"),
                        schedule_file=schedule_file,
                    )
                except Exception as exc:
                    logger.error(f"Ошибка загрузки {ch_name}: {exc}")
        except Exception as exc:
            logger.error(f"upload_now error: {exc}")

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "Загрузка запущена в фоне"}


# ─────────────────────────────────────────────────────────────────
# Quota API
# ─────────────────────────────────────────────────────────────────
@app.get("/api/quota")
def get_quota():
    config = _load_config()
    try:
        from modules.quota_manager import QuotaManager, UPLOAD_COST
        account_groups = config.get("account_groups", {})
        channels_cfg   = config.get("channels", {})

        qm = QuotaManager(account_groups)
        raw = qm.get_status()   # {ag_id: {used, quota, remaining, uploads_left}}

        # Канал → account_group
        ag_to_channels: dict[str, list] = {}
        for ch_name, ch_cfg in channels_cfg.items():
            ag = ch_cfg.get("google_account", "")
            ag_to_channels.setdefault(ag, []).append(ch_name)

        # Группировать account_groups по client_id внутри файла (один client_id = один GCP-проект)
        def _get_client_id(ag_cfg: dict) -> str:
            """Читает client_id из client_secret.json. Если файл недоступен — возвращает путь как fallback."""
            secret_path = BASE_DIR / ag_cfg.get("client_secret", "")
            try:
                data = json.loads(secret_path.read_text(encoding="utf-8"))
                info = data.get("installed") or data.get("web") or {}
                return info.get("client_id") or str(secret_path)
            except Exception:
                return str(secret_path)  # разные пути = разные проекты

        secret_to_groups: dict[str, list] = {}
        for ag_id, ag_cfg in account_groups.items():
            key = _get_client_id(ag_cfg)
            secret_to_groups.setdefault(key, []).append(ag_id)

        result = {}
        seen: set = set()

        for ag_id in raw:
            if ag_id in seen:
                continue
            ag_cfg  = account_groups.get(ag_id, {})
            secret  = ag_cfg.get("client_secret", ag_id)
            group   = secret_to_groups.get(secret, [ag_id])

            # Суммируем used_units по всем группам, разделяющим один секрет
            total_used  = sum(raw[g]["used"]  for g in group if g in raw)
            total_quota = max(raw[g]["quota"] for g in group if g in raw)

            # Каналы, использующие этот GCP-проект
            ch_list = []
            for g in group:
                ch_list.extend(ag_to_channels.get(g, []))

            result[secret] = {
                "display_name": secret.split("/")[-1].replace("_secret.json", ""),
                "groups": group,
                "channels": sorted(ch_list),
                "shared": len(group) > 1,
                "used":   total_used,
                "quota":  total_quota,
                "remaining":   total_quota - total_used,
                "uploads_left": max(0, (total_quota - total_used) // UPLOAD_COST),
            }
            seen.update(group)

        return result
    except Exception as e:
        logger.warning(f"quota error: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────
# Channels API
# ─────────────────────────────────────────────────────────────────
@app.get("/api/channels")
def get_channels():
    config = _load_config()
    account_groups = config.get("account_groups", {})
    result = []
    for name, cfg in config.get("channels", {}).items():
        auth = _channel_auth_status(name, cfg, account_groups)
        result.append({
            "name": name,
            "channel_id": cfg.get("channel_id", ""),
            "language": _normalize_language(cfg.get("language", ""), name),
            "schedule": cfg.get("schedule_utc", []),
            "authenticated": bool(auth.get("ok")),
            "has_secret": bool(auth.get("has_secret")),
            "token_exists": bool(auth.get("token_exists")),
            "auth_status": auth.get("status", "unknown"),
            "auth_message": auth.get("message", ""),
            "missing_scopes": auth.get("missing_scopes", []),
        })
    return result


@app.post("/api/channels/setup")
async def setup_channel(
    channel_name: str = Form(...),
    language: str = Form("ru"),
    schedule_times: str = Form("09:00,15:00,21:00"),
    client_secret: UploadFile = File(...),
):
    """
    Добавить/обновить YouTube-канал через UI.
    Загружает client_secret.json, прописывает канал в config.yaml.
    """
    # Валидация имени канала
    safe_name = "".join(c for c in channel_name if c.isalnum() or c in "-_")
    if not safe_name:
        raise HTTPException(400, "Недопустимое имя канала")

    # Читаем и валидируем client_secret.json
    content = await client_secret.read()
    try:
        secret_data = json.loads(content)
    except Exception:
        raise HTTPException(400, "Файл не является корректным JSON")

    # Проверяем, что это OAuth client secret (Desktop или Web)
    if not any(k in secret_data for k in ("installed", "web")):
        raise HTTPException(
            400,
            "Файл не похож на client_secret.json. "
            "Скачайте OAuth 2.0 Client ID (тип: Desktop app) из Google Cloud Console."
        )

    # Сохраняем client_secret.json
    secrets_dir = BASE_DIR / "secrets"
    secrets_dir.mkdir(exist_ok=True)
    secret_path = secrets_dir / f"{safe_name}_secret.json"
    secret_path.write_bytes(content)

    # Парсим расписание
    times = [t.strip() for t in schedule_times.split(",") if t.strip()]
    if not times:
        times = ["09:00"]

    # Обновляем config.yaml
    config = _load_config()
    if "channels" not in config:
        config["channels"] = {}
    if "account_groups" not in config:
        config["account_groups"] = {}

    ag_name = f"ag_{safe_name}"
    config["account_groups"][ag_name] = {
        "client_secret": f"secrets/{safe_name}_secret.json",
        "daily_quota": 10000,
    }
    config["channels"][safe_name] = {
        "google_account": ag_name,
        "channel_id": "",
        "language": language,
        "token_file": f"tokens/{safe_name}.json",
        "schedule_utc": times,
    }
    _save_config(config)

    logger.info(f"Канал '{safe_name}' добавлен в config.yaml")
    return {"ok": True, "channel": safe_name}


@app.post("/api/channels/{channel}/connect")
async def connect_channel(
    channel: str,
    channel_id: str = Form(""),
    client_secret: Optional[UploadFile] = File(None),
):
    """
    Обновить channel_id и/или client_secret для существующего канала.
    Используется для подключения уже созданных языковых каналов.
    """
    config = _load_config()
    channels = config.get("channels", {})
    if channel not in channels:
        raise HTTPException(404, f"Канал «{channel}» не найден в config.yaml")

    changed = []

    if channel_id.strip():
        config["channels"][channel]["channel_id"] = channel_id.strip()
        changed.append("channel_id")

    if client_secret and client_secret.filename:
        content = await client_secret.read()
        try:
            secret_data = json.loads(content)
        except Exception:
            raise HTTPException(400, "Файл не является корректным JSON")
        if not any(k in secret_data for k in ("installed", "web")):
            raise HTTPException(
                400,
                "Файл не похож на client_secret.json. "
                "Скачайте OAuth 2.0 Client ID (тип: Desktop app) из Google Cloud Console."
            )
        ag_name = channels[channel].get("google_account", "")
        ag = config.get("account_groups", {}).get(ag_name, {})
        secret_rel = ag.get("client_secret", f"secrets/{channel}_secret.json")
        secret_path = BASE_DIR / secret_rel
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_bytes(content)
        changed.append("client_secret")

    if not changed:
        return {"ok": True, "message": "Ничего не изменено"}

    _save_config(config)
    logger.info(f"Канал «{channel}» обновлён: {', '.join(changed)}")
    return {"ok": True, "channel": channel, "changed": changed}


@app.delete("/api/channels/{channel}")
def delete_channel(channel: str):
    """Удалить канал из config.yaml (и его client_secret)."""
    config = _load_config()
    channels = config.get("channels", {})
    if channel not in channels:
        raise HTTPException(404, f"Канал '{channel}' не найден")

    ch_cfg = channels.pop(channel)
    # Удаляем account_group если он только для этого канала
    ag_name = ch_cfg.get("google_account", "")
    ag = config.get("account_groups", {})
    if ag_name in ag:
        still_used = any(
            c.get("google_account") == ag_name
            for c in config.get("channels", {}).values()
        )
        if not still_used:
            ag.pop(ag_name, None)
            # Удаляем client_secret файл
            secret_path = BASE_DIR / ag.get("client_secret", "")
            if secret_path.exists():
                try:
                    secret_path.unlink()
                except Exception:
                    pass

    _save_config(config)
    logger.info(f"Канал '{channel}' удалён из config.yaml")
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────
# YouTube analytics snapshot (Data API v3)
# ─────────────────────────────────────────────────────────────────
_ANALYTICS_SNAPSHOT_PATH = BASE_DIR / "data" / "analytics_snapshot.json"
_ANALYTICS_HISTORY_PATH  = BASE_DIR / "data" / "analytics_history.json"
_ANALYTICS_HISTORY_MAX   = 90  # максимум точек в истории


def _snap_to_history_entry(snap: dict) -> dict:
    """Лёгкая запись истории из полного снимка (агрегаты + основные метрики по каналам)."""
    agg = snap.get("aggregates", {})
    ch_subs: dict = {}
    ch_views: dict = {}
    ch_comments: dict = {}
    for ch in snap.get("channels", []):
        if ch.get("error"):
            continue
        name = ch.get("name", "")
        subs = ch.get("subscribers") or 0
        views = ch.get("channel_views") or 0
        comments = ch.get("comments_total")
        if comments is None:
            comments = sum(int(v.get("comments") or 0) for v in ch.get("videos") or [])
        if subs or views:
            ch_subs[name] = subs
            ch_views[name] = views
        if comments:
            ch_comments[name] = int(comments or 0)
    return {
        "collected_at": snap.get("collected_at", ""),
        "total_subscribers": agg.get("total_subscribers", 0),
        "total_channel_views": agg.get("total_channel_views", 0),
        "total_comments_on_shorts": agg.get("total_comments_on_shorts", 0),
        "channels_subscribers": ch_subs,
        "channels_views": ch_views,
        "channels_comments": ch_comments,
    }


def _load_analytics_history() -> list:
    p = _ANALYTICS_HISTORY_PATH
    if not p.exists():
        # Предзаполнить из существующего снимка, если есть
        snap = _load_analytics_snapshot_file()
        if snap:
            return [_snap_to_history_entry(snap)]
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("analytics history read failed: %s", exc)
        return []


def _append_analytics_history(snap: dict) -> None:
    """Добавить запись в историю, сохраняя не более _ANALYTICS_HISTORY_MAX точек."""
    history = _load_analytics_history()
    entry = _snap_to_history_entry(snap)
    # Не дублировать одинаковый timestamp
    if history and history[-1].get("collected_at") == entry.get("collected_at"):
        history[-1].update(entry)
        _ANALYTICS_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ANALYTICS_HISTORY_PATH.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return
    history.append(entry)
    if len(history) > _ANALYTICS_HISTORY_MAX:
        history = history[-_ANALYTICS_HISTORY_MAX:]
    _ANALYTICS_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ANALYTICS_HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_analytics_snapshot_file() -> Optional[dict]:
    p = _ANALYTICS_SNAPSHOT_PATH
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("analytics snapshot read failed: %s", exc)
        return None


def _save_analytics_snapshot(data: dict) -> None:
    _ANALYTICS_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ANALYTICS_SNAPSHOT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


_ANALYTICS_TRANSIENT_ERROR_MARKERS = (
    "HttpError 500",
    "HttpError 502",
    "HttpError 503",
    "INTERNAL_ERROR",
    "Internal error",
    "backendError",
    "service is currently unavailable",
    "temporarily unavailable",
    "quotaExceeded",
    "Квота YouTube Data API",
)


def _is_transient_analytics_error(error: Any) -> bool:
    text = str(error or "").lower()
    return any(marker.lower() in text for marker in _ANALYTICS_TRANSIENT_ERROR_MARKERS)


def _analytics_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _recompute_analytics_aggregates(snap: dict) -> None:
    channel_rows = snap.get("channels") or []
    all_video_rows: list[dict[str, Any]] = []
    tot_sub = 0
    tot_views = 0
    tot_videos = 0
    tot_comments = 0
    ok_count = 0

    for ch in channel_rows:
        if ch.get("error"):
            continue
        if ch.get("subscribers") is not None:
            tot_sub += _analytics_int(ch.get("subscribers"))
        if ch.get("channel_views") is not None:
            tot_views += _analytics_int(ch.get("channel_views"))
        if ch.get("video_count") is not None:
            tot_videos += _analytics_int(ch.get("video_count"))

        comments = ch.get("comments_total")
        if comments is None:
            comments = sum(_analytics_int(v.get("comments")) for v in ch.get("videos") or [])
            ch["comments_total"] = comments
        tot_comments += _analytics_int(comments)

        for video in ch.get("videos") or []:
            all_video_rows.append({
                **video,
                "channel": ch.get("name", ""),
                "language": ch.get("language", ""),
            })

        ok_count += 1

    all_video_rows.sort(key=lambda row: row.get("views", 0), reverse=True)
    agg = dict(snap.get("aggregates") or {})
    agg.update({
        "total_subscribers": tot_sub,
        "total_channel_views": tot_views,
        "total_videos_on_channels": tot_videos,
        "total_comments_on_shorts": tot_comments,
        "channels_ok_count": ok_count,
        "channels_total": len(channel_rows),
    })
    snap["aggregates"] = agg
    snap["top_videos_global"] = all_video_rows[:50]


def _apply_analytics_stale_fallback(snap: dict, previous: Optional[dict]) -> dict:
    """Keep dashboard totals stable when YouTube returns transient 5xx errors."""
    if not previous:
        return snap

    prev_by_name = {
        ch.get("name"): ch
        for ch in previous.get("channels") or []
        if ch.get("name") and not ch.get("error")
    }
    if not prev_by_name:
        return snap

    stale_channels: list[str] = []
    merged_channels: list[dict[str, Any]] = []
    for ch in snap.get("channels") or []:
        name = ch.get("name")
        prev_ch = prev_by_name.get(name)
        if ch.get("error") and prev_ch and _is_transient_analytics_error(ch.get("error")):
            fallback = json.loads(json.dumps(prev_ch, ensure_ascii=False))
            fallback["error"] = None
            fallback["stale"] = True
            fallback["stale_from_collected_at"] = (
                prev_ch.get("stale_from_collected_at")
                or previous.get("collected_at", "")
            )
            fallback["refresh_error"] = ch.get("error")
            if ch.get("channel_id"):
                fallback["channel_id"] = ch.get("channel_id")
            if ch.get("language"):
                fallback["language"] = ch.get("language")
            stale_channels.append(str(name))
            merged_channels.append(fallback)
        else:
            merged_channels.append(ch)

    if not stale_channels:
        return snap

    snap = {**snap, "channels": merged_channels}
    snap["stale_channels"] = stale_channels
    snap["stale_channels_count"] = len(stale_channels)
    _recompute_analytics_aggregates(snap)
    logger.warning(
        "Analytics refresh reused stale data after transient YouTube errors: %s",
        ", ".join(stale_channels),
    )
    return snap


def _uncovered_transient_analytics_channels(snap: dict) -> list[str]:
    return [
        str(ch.get("name") or "?")
        for ch in snap.get("channels") or []
        if ch.get("error") and _is_transient_analytics_error(ch.get("error"))
    ]


_COMMENTS_STATE_PATH = BASE_DIR / "data" / "comments_state.json"
_comments_state_lock = threading.Lock()
_COMMENTS_HIDDEN_STATUSES = {"skipped", "cleared_answered"}


def _comment_state_key(channel: str, comment_id: str) -> str:
    return f"{channel.strip()}::{comment_id.strip()}"


def _load_comments_state_raw() -> dict[str, Any]:
    if not _COMMENTS_STATE_PATH.exists():
        return {"items": {}}
    try:
        data = json.loads(_COMMENTS_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return data
    except Exception as exc:
        logger.warning("comments state read failed: %s", exc)
    return {"items": {}}


def _save_comments_state_raw(data: dict[str, Any]) -> None:
    _COMMENTS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _COMMENTS_STATE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_comments_state() -> dict[str, Any]:
    with _comments_state_lock:
        return _load_comments_state_raw()


def _set_comment_state(
    channel: str,
    comment_id: str,
    status: str,
    *,
    extra: Optional[dict[str, Any]] = None,
) -> bool:
    channel = (channel or "").strip()
    comment_id = (comment_id or "").strip()
    if not channel or not comment_id:
        return False
    now = datetime.now(timezone.utc).isoformat()
    with _comments_state_lock:
        state = _load_comments_state_raw()
        items = state.setdefault("items", {})
        key = _comment_state_key(channel, comment_id)
        row = dict(items.get(key) or {})
        row.update({
            "channel": channel,
            "comment_id": comment_id,
            "status": status,
            "updated_at": now,
        })
        if extra:
            row.update(extra)
        items[key] = row
        _save_comments_state_raw(state)
    return True


def _set_comments_state(items: list[CommentStateItem], status: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    with _comments_state_lock:
        state = _load_comments_state_raw()
        stored = state.setdefault("items", {})
        for item in items:
            channel = (item.channel or "").strip()
            comment_id = (item.comment_id or "").strip()
            if not channel or not comment_id:
                continue
            key = _comment_state_key(channel, comment_id)
            row = dict(stored.get(key) or {})
            row.update({
                "channel": channel,
                "comment_id": comment_id,
                "status": status,
                "updated_at": now,
            })
            stored[key] = row
            updated += 1
        _save_comments_state_raw(state)
    return updated


def _apply_comment_state(comment: dict[str, Any], channel: str, state: dict[str, Any]) -> str:
    key = _comment_state_key(channel, comment.get("comment_id") or "")
    row = (state.get("items") or {}).get(key) or {}
    status = str(row.get("status") or "").strip()
    if status:
        comment["workflow_status"] = status
        comment["workflow_updated_at"] = row.get("updated_at") or ""
    comment["answered"] = bool(
        status == "answered"
        or comment.get("has_channel_reply")
        or comment.get("answered")
    )
    return status


@app.get("/api/comments/status")
def comments_status():
    """Current comment-aware upload limits and preparation status."""
    config = _load_config()
    return build_comments_status(config, BASE_DIR)


@app.get("/api/comments")
def comments_list(
    channel: Optional[str] = Query(None),
    max_results: int = Query(25, ge=1, le=100),
):
    """Fetch recent YouTube comments. Requires reauth after comment scope was added."""
    from modules.youtube_comments import fetch_recent_comments, language_ru

    config = _load_config()
    channels = config.get("channels", {})
    targets = []
    if channel:
        if channel not in channels:
            raise HTTPException(404, f"Канал «{channel}» не найден")
        targets = [channel]
    else:
        status = build_comments_status(config, BASE_DIR)
        targets = [r["channel"] for r in status.get("channels", []) if r.get("comments_active")]
        if not targets:
            targets = [name for name, cfg in channels.items() if cfg.get("channel_id")][:5]

    state = _load_comments_state()
    out = []
    errors = []
    hidden = {"skipped": 0, "cleared_answered": 0}
    for ch_name in targets[:10]:
        ch_cfg = channels.get(ch_name) or {}
        try:
            data = fetch_recent_comments(BASE_DIR, ch_name, ch_cfg, max_results=max_results)
        except Exception as exc:
            logger.warning("comments fetch failed for %s: %s", ch_name, exc)
            data = {"ok": False, "error": str(exc)}
        if not data.get("ok"):
            errors.append({
                "channel": ch_name,
                "language": ch_cfg.get("language") or "",
                "language_ru": language_ru(ch_cfg.get("language")),
                "error": data.get("error"),
            })
            continue
        for c in data.get("comments") or []:
            c["channel"] = ch_name
            c["language"] = ch_cfg.get("language") or ""
            c["language_ru"] = language_ru(ch_cfg.get("language"))
            workflow_status = _apply_comment_state(c, ch_name, state)
            if workflow_status in _COMMENTS_HIDDEN_STATUSES:
                hidden[workflow_status] = hidden.get(workflow_status, 0) + 1
                continue
            out.append(c)
    out.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    return {"ok": True, "comments": out, "errors": errors, "hidden": hidden}


@app.post("/api/comments/draft")
def comments_draft(body: CommentDraftRequest):
    """Prepare Russian translation and a moderated draft reply."""
    from modules.youtube_comments import draft_comment_reply

    config = _load_config()
    channels = config.get("channels", {})
    if body.channel not in channels:
        raise HTTPException(404, f"Канал «{body.channel}» не найден")
    ch_cfg = channels[body.channel]
    try:
        return {
            "ok": True,
            **draft_comment_reply(
                comment_text=body.comment_text,
                target_language=ch_cfg.get("language") or "",
                channel_name=body.channel,
                video_title=body.video_title,
                russian_instruction=body.russian_instruction,
                previous_reply=body.previous_reply,
            ),
        }
    except Exception as exc:
        logger.exception("comment draft failed")
        raise HTTPException(500, str(exc))


@app.post("/api/comments/translate")
def comments_translate(body: CommentTranslateRequest):
    """Translate loaded comments to Russian for moderation."""
    from modules.youtube_comments import translate_comments_ru

    try:
        return {"ok": True, **translate_comments_ru(body.comments)}
    except Exception as exc:
        logger.exception("comment translation failed")
        raise HTTPException(500, str(exc))


@app.post("/api/comments/state")
def comments_state(body: CommentStateRequest):
    """Persist comment workflow state for the local moderation queue."""
    action = (body.action or "").strip().lower()
    status_map = {
        "skip": "skipped",
        "clear_answered": "cleared_answered",
        "answered": "answered",
    }
    if action not in status_map:
        raise HTTPException(400, "Неизвестное действие для комментариев")
    if not body.comments:
        return {"ok": True, "updated": 0}
    updated = _set_comments_state(body.comments, status_map[action])
    return {"ok": True, "updated": updated}


@app.post("/api/comments/reply")
def comments_reply(body: CommentReplyRequest):
    """Send a reply only after user moderation/confirmation."""
    from modules.youtube_comments import send_comment_reply

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Текст ответа пустой")
    if len(text) > 9000:
        raise HTTPException(400, "Текст ответа слишком длинный")

    config = _load_config()
    channels = config.get("channels", {})
    if body.channel not in channels:
        raise HTTPException(404, f"Канал «{body.channel}» не найден")
    result = send_comment_reply(
        BASE_DIR,
        body.channel,
        channels[body.channel],
        parent_id=body.parent_id,
        text=text,
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "Не удалось отправить ответ")
    _set_comment_state(
        body.channel,
        body.parent_id,
        "answered",
        extra={"reply_id": result.get("id") or ""},
    )
    return result


@app.get("/api/analytics/ping")
def analytics_ping():
    """Проверка, что сервер с поддержкой аналитики (после обновления кода)."""
    return {"ok": True, "analytics": True}


@app.get("/api/analytics/snapshot")
def get_analytics_snapshot(fmt: str = "json"):
    """
    Последний сохранённый снимок без запросов к YouTube.
    fmt=json | csv | markdown  (параметр не «format», чтобы не пересекаться с внутренностями Starlette)
    """
    snap = _load_analytics_snapshot_file()
    if snap is None:
        if fmt == "csv":
            return PlainTextResponse("snapshot_collected_at,\n", media_type="text/csv; charset=utf-8")
        if fmt == "markdown":
            return PlainTextResponse(
                "Снимок ещё не собирался. Нажмите «Обновить статистику» в разделе Аналитика.",
                media_type="text/plain; charset=utf-8",
            )
        return {
            "ok": False,
            "message": "Снимок ещё не собирался — нажмите «Обновить статистику».",
            "collected_at": None,
            "aggregates": {},
            "channels": [],
            "top_videos_global": [],
        }

    fmt = (fmt or "json").lower().strip()
    if fmt == "csv":
        from modules.youtube_stats import snapshot_to_csv

        body = snapshot_to_csv(snap)
        return PlainTextResponse(body, media_type="text/csv; charset=utf-8")
    if fmt in ("markdown", "md"):
        from modules.youtube_stats import snapshot_to_markdown_for_ai

        return PlainTextResponse(
            snapshot_to_markdown_for_ai(snap),
            media_type="text/plain; charset=utf-8",
        )
    return {**snap, "ok": True}


@app.post("/api/analytics/refresh")
def post_analytics_refresh():
    """Собрать статистику с YouTube и сохранить снимок (может занять до минуты)."""
    from modules.youtube_stats import collect_analytics_snapshot

    config = _load_config()
    schedule = _load_schedule()
    previous_snap = _load_analytics_snapshot_file()
    try:
        snap = collect_analytics_snapshot(BASE_DIR, config, schedule)
    except Exception as exc:
        logger.exception("analytics refresh failed")
        raise HTTPException(500, f"Сбор аналитики не удался: {exc}") from exc
    snap = _apply_analytics_stale_fallback(snap, previous_snap)
    uncovered_transient = _uncovered_transient_analytics_channels(snap)
    if uncovered_transient:
        raise HTTPException(
            503,
            "YouTube API temporarily failed for channels: "
            + ", ".join(uncovered_transient)
            + ". Snapshot was not saved; retry later.",
        )
    _append_analytics_history(snap)
    _save_analytics_snapshot(snap)
    logger.info("Analytics snapshot saved: %s", _ANALYTICS_SNAPSHOT_PATH)
    return {**snap, "ok": True}


@app.get("/api/analytics/history")
def get_analytics_history():
    """История снимков для графиков роста подписчиков и просмотров."""
    return _load_analytics_history()


# Хранилище незавершённых OAuth-потоков: state -> (flow, token_file, channel_name, created_at)
_pending_auth_flows: dict = {}
_AUTH_FLOW_TTL = 600  # секунд — поток удаляется если не завершён за 10 минут


def _cleanup_auth_flows():
    """Удалить просроченные OAuth-потоки (вызывать перед добавлением нового)."""
    now = datetime.now(timezone.utc).timestamp()
    expired = [k for k, v in _pending_auth_flows.items()
               if now - v[3] > _AUTH_FLOW_TTL]
    for k in expired:
        del _pending_auth_flows[k]
        logger.debug(f"Удалён просроченный OAuth-поток: {k}")

# SCOPES для YouTube API
_YT_SCOPES = YOUTUBE_REQUIRED_SCOPES

# Разрешаем OAuth по HTTP на localhost (необходимо для google-auth-oauthlib)
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


@app.post("/api/auth/{channel}")
async def auth_channel(channel: str, request: Request):
    """
    Шаг 1 OAuth: генерирует URL авторизации Google.
    Если токен уже есть и валиден — обновляет его и возвращает успех без браузера.
    Иначе возвращает auth_url для открытия в popup.
    """
    config = _load_config()
    channels = config.get("channels", {})
    if channel not in channels:
        raise HTTPException(404, f"Канал «{channel}» не найден в config.yaml")

    ch_cfg = channels[channel]
    account_groups = config.get("account_groups", {})
    google_account = ch_cfg.get("google_account", "")
    ag = account_groups.get(google_account, {})
    client_secret_path = BASE_DIR / ag.get("client_secret", "secrets/client_1.json")
    token_file = BASE_DIR / ch_cfg.get("token_file", f"tokens/{channel}.json")

    if not client_secret_path.exists():
        raise HTTPException(
            400,
            f"Файл client_secret.json не найден ({client_secret_path.name}). "
            "Нажмите кнопку «🔗 Подключить» рядом с каналом и загрузите файл."
        )

    # Попытка обновить существующий токен без браузера
    if token_file.exists():
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request as GRequest
            creds = Credentials.from_authorized_user_file(str(token_file), _YT_SCOPES)
            if creds.valid and creds.has_scopes(_YT_SCOPES):
                return {"ok": True, "message": f"Канал «{channel}» уже авторизован"}
            if not creds.has_scopes(_YT_SCOPES):
                logger.info("Token for %s needs reauth: comment scope was added", channel)
            elif creds.expired and creds.refresh_token:
                creds.refresh(GRequest())
                token_file.write_text(creds.to_json(), encoding="utf-8")
                logger.info(f"Токен для {channel} обновлён")
                return {"ok": True, "message": f"Токен для «{channel}» обновлён"}
        except Exception as e:
            logger.warning(f"Не удалось обновить токен {channel}: {e}")

    # Строим redirect_uri по адресу сервера
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/api/auth/callback"

    try:
        from google_auth_oauthlib.flow import Flow
        state = _secrets_mod.token_urlsafe(16)
        flow = Flow.from_client_secrets_file(
            str(client_secret_path),
            scopes=_YT_SCOPES,
            redirect_uri=redirect_uri,
            state=state,
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        _cleanup_auth_flows()
        _pending_auth_flows[state] = (flow, token_file, channel,
                                      datetime.now(timezone.utc).timestamp())
        logger.info(f"OAuth-поток для {channel} создан, state={state}")
        return {"ok": True, "auth_url": auth_url, "channel": channel}
    except Exception as exc:
        logger.error(f"Ошибка создания OAuth-потока для {channel}: {exc}")
        raise HTTPException(500, str(exc))


@app.get("/api/auth/callback")
def auth_callback(code: str = None, state: str = None, error: str = None):
    """Шаг 2 OAuth: Google редиректит сюда с кодом авторизации."""

    def _html(body: str) -> HTMLResponse:
        return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>body{{font-family:sans-serif;display:flex;justify-content:center;
align-items:center;height:100vh;margin:0;background:#0d1117;color:#e6edf3}}</style>
</head><body><div style="text-align:center;max-width:400px">{body}</div></body></html>""")

    if error:
        msg = {"type": "auth_error", "error": error}
        return _html(
            f"<p style='color:#f85149'>❌ Ошибка авторизации: {error}</p>"
            f"<script>window.opener&&window.opener.postMessage({json.dumps(msg)},'*');"
            "setTimeout(()=>window.close(),3000)</script>"
            "<p style='color:#8b949e;font-size:13px'>Окно закроется через 3 секунды</p>"
        )

    if not state or state not in _pending_auth_flows:
        return _html(
            "<p style='color:#f85149'>❌ Неверный или устаревший запрос.<br>"
            "Закройте окно и попробуйте снова.</p>"
        )

    flow, token_file, channel, _created = _pending_auth_flows.pop(state)
    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
        logger.info(f"✓ Токен для канала «{channel}» сохранён: {token_file}")
        msg = {"type": "auth_success", "channel": channel}
        return _html(
            f"<p style='color:#3fb950;font-size:20px'>✅ Канал «{channel}» авторизован!</p>"
            f"<script>window.opener&&window.opener.postMessage({json.dumps(msg)},'*');"
            "setTimeout(()=>window.close(),1500)</script>"
            "<p style='color:#8b949e;font-size:13px'>Окно закроется автоматически...</p>"
        )
    except Exception as exc:
        logger.error(f"Ошибка OAuth callback для {channel}: {exc}")
        msg = {"type": "auth_error", "error": str(exc)}
        return _html(
            f"<p style='color:#f85149'>❌ Ошибка: {exc}</p>"
            f"<script>window.opener&&window.opener.postMessage({json.dumps(msg)},'*');"
            "setTimeout(()=>window.close(),5000)</script>"
        )


# ─────────────────────────────────────────────────────────────────
# Scheduler API
# ─────────────────────────────────────────────────────────────────


@app.get("/api/scheduler/status")
def scheduler_status():
    running = _scheduler_thread is not None and _scheduler_thread.is_alive()
    return {"running": running}


@app.post("/api/scheduler/start")
def start_scheduler_api():
    global _scheduler_thread, _scheduler_stop
    if _scheduler_thread and _scheduler_thread.is_alive():
        return {"ok": True, "message": "Уже запущен"}

    config = _load_config()
    if not _scheduler_enabled(config):
        raise HTTPException(
            423,
            "Планировщик YouTube отключён в config.yaml: youtube.uploads_enabled=false или youtube.scheduler_enabled=false.",
        )
    _scheduler_stop.clear()

    def _run():
        try:
            from modules.uploader import start_scheduler
            start_scheduler(config, stop_event=_scheduler_stop, pause_callback=_processing_busy)
        except Exception as exc:
            logger.error(f"Scheduler error: {exc}")

    _scheduler_thread = threading.Thread(target=_run, daemon=True)
    _scheduler_thread.start()
    return {"ok": True}


@app.post("/api/scheduler/stop")
def stop_scheduler_api():
    _scheduler_stop.set()
    return {"ok": True, "message": "Сигнал остановки отправлен"}


@app.post("/api/schedule/clean-failed")
def clean_failed_api():
    """
    Удалить все failed-записи, для которых уже есть успешная scheduled-запись
    с тем же channel+filename. Это мусор от сетевых сбоев / ретраев.
    """
    sf = _queue_dir() / "schedule.json"
    removed = schedule_store.clean_all_stale_failed(sf)
    return {"ok": True, "removed": removed}


# ─────────────────────────────────────────────────────────────────
# Settings — чтение и запись .env + Google credentials
# ─────────────────────────────────────────────────────────────────

_ENV_PATH = BASE_DIR / ".env"
_GOOGLE_CREDS_PATH = BASE_DIR / "secrets" / "google-service-account.json"

_ENV_KEYS = {
    "openai_api_key":  "OPENAI_API_KEY",
    "deepl_api_key":   "DEEPL_API_KEY",
}


def _read_env() -> dict[str, str]:
    """Прочитать .env как dict. Пустые или отсутствующие ключи → ''."""
    result: dict[str, str] = {k: "" for k in _ENV_KEYS}
    if not _ENV_PATH.exists():
        return result
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        for field, env_name in _ENV_KEYS.items():
            if key == env_name:
                result[field] = val
    return result


def _write_env(updates: dict[str, str]):
    """Обновить или добавить строки в .env. Остальные строки не трогает."""
    lines: list[str] = []
    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()

    written: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        env_name = stripped.split("=", 1)[0].strip()
        # Найти поле для этого env_name
        field = next((f for f, n in _ENV_KEYS.items() if n == env_name), None)
        if field and field in updates and updates[field]:
            new_lines.append(f"{env_name}={updates[field]}")
            written.add(field)
        else:
            new_lines.append(line)

    # Добавить новые ключи, которых ещё не было в файле
    for field, env_name in _ENV_KEYS.items():
        if field in updates and updates[field] and field not in written:
            new_lines.append(f"{env_name}={updates[field]}")

    # Обновить путь к Google credentials
    gc_field = "GOOGLE_APPLICATION_CREDENTIALS"
    if not any(gc_field in l for l in new_lines):
        new_lines.append(f"{gc_field}=secrets/google-service-account.json")

    _ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    # Перезагрузить в os.environ сразу
    load_dotenv(__import__("pathlib").Path(__file__).resolve().parent / ".env", override=True)


@app.get("/api/settings")
def get_settings():
    """Вернуть статус ключей (есть/нет) и маскированные значения."""
    vals = _read_env()
    google_ok = _GOOGLE_CREDS_PATH.exists() and _GOOGLE_CREDS_PATH.stat().st_size > 10

    def mask(v: str) -> str:
        if not v:
            return ""
        return v[:4] + "••••••••" + v[-4:] if len(v) > 12 else "••••••••"

    return {
        "openai_api_key":  {"configured": bool(vals["openai_api_key"]),  "masked": mask(vals["openai_api_key"])},
        "deepl_api_key":   {"configured": bool(vals["deepl_api_key"]),   "masked": mask(vals["deepl_api_key"])},
        "google_credentials": {"configured": google_ok, "masked": "service-account.json" if google_ok else ""},
    }


class SettingsRequest(BaseModel):
    openai_api_key:  Optional[str] = None
    deepl_api_key:   Optional[str] = None
    google_credentials_json: Optional[str] = None   # содержимое JSON как строка


@app.post("/api/settings")
def save_settings(req: SettingsRequest):
    """Сохранить ключи в .env и Google JSON в secrets/."""
    updates: dict[str, str] = {}
    if req.openai_api_key  is not None: updates["openai_api_key"]  = req.openai_api_key.strip()
    if req.deepl_api_key   is not None: updates["deepl_api_key"]   = req.deepl_api_key.strip()

    if updates:
        _write_env(updates)

    if req.google_credentials_json:
        try:
            json.loads(req.google_credentials_json)   # проверить валидность JSON
        except json.JSONDecodeError:
            raise HTTPException(400, "Невалидный JSON для Google Credentials")
        _GOOGLE_CREDS_PATH.parent.mkdir(exist_ok=True)
        _GOOGLE_CREDS_PATH.write_text(req.google_credentials_json, encoding="utf-8")

    return {"ok": True}


# ─────────────────────────────────────────────────────────────────
# Share API  —  публичные ссылки на проекты с клипами
# ─────────────────────────────────────────────────────────────────

_SHARES_FILE = BASE_DIR / "shares.json"
_shares_lock = threading.Lock()


def _load_shares() -> dict:
    if _SHARES_FILE.exists():
        try:
            return json.loads(_SHARES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_shares(data: dict):
    _SHARES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@app.post("/api/share/{video_id}")
def create_share(video_id: str):
    """Создать публичную share-ссылку для проекта (video_id)."""
    config = _load_config()
    output_dir = _output_dir(config)
    video_dir = output_dir / video_id
    if not video_dir.exists():
        raise HTTPException(404, "Проект не найден")

    clips_dir = video_dir / "clips_final"
    clips = sorted(clips_dir.glob("*.mp4")) if clips_dir.exists() else []
    if not clips:
        raise HTTPException(400, "В проекте нет клипов")

    # Попробуем прочитать заголовок из первого мета-файла
    title = video_id
    first_meta = clips[0].with_suffix(".json")
    if first_meta.exists():
        try:
            m = json.loads(first_meta.read_text(encoding="utf-8"))
            # убираем номер клипа из заголовка
            raw = m.get("title", video_id)
            title = " ".join(raw.split()[:-1]) if raw.split()[-1].isdigit() else raw
        except Exception:
            pass

    token = uuid.uuid4().hex[:12]
    with _shares_lock:
        shares = _load_shares()
        shares[token] = {
            "video_id":   video_id,
            "title":      title,
            "clip_count": len(clips),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_shares(shares)

    return {"token": token, "url": f"/share/{token}", "clip_count": len(clips)}


def _get_local_ip() -> str:
    """Надёжное определение локального сетевого IP (не 127.0.0.1)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.get("/api/server-info")
def server_info(request: Request):
    """Возвращает сетевой IP сервера для формирования share-ссылок."""
    port = request.url.port or 8088
    return {"local_ip": _get_local_ip(), "port": port}


# ─── ngrok туннель ────────────────────────────────────────────────
_NGROK_EXE = BASE_DIR.parent / "ngrok" / "ngrok.exe"
_ngrok_proc: Optional[subprocess.Popen] = None


def _ngrok_public_url() -> Optional[str]:
    """Получить текущий публичный URL от ngrok API."""
    try:
        import urllib.request, json as _json
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=3) as r:
            data = _json.loads(r.read())
        for t in data.get("tunnels", []):
            if t.get("proto") == "https":
                return t["public_url"]
    except Exception:
        pass
    return None


@app.post("/api/ngrok/start")
def ngrok_start():
    """Запустить ngrok туннель на порт 8088."""
    global _ngrok_proc
    # Если уже работает — просто вернуть URL
    url = _ngrok_public_url()
    if url:
        return {"url": url, "started": False}

    if not _NGROK_EXE.exists():
        raise HTTPException(404, "ngrok.exe не найден. Ожидается: " + str(_NGROK_EXE))

    authtoken = (os.getenv("NGROK_AUTHTOKEN") or "").strip()
    cmd = [str(_NGROK_EXE), "http", "8088"]
    if authtoken:
        cmd.extend(["--authtoken", authtoken])

    _ngrok_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    # Ждём до 10 секунд пока поднимется
    import time
    for _ in range(20):
        time.sleep(0.5)
        url = _ngrok_public_url()
        if url:
            return {"url": url, "started": True}

    raise HTTPException(500, "ngrok запустился, но URL не получен. Проверь токен авторизации.")


@app.post("/api/ngrok/stop")
def ngrok_stop():
    """Остановить ngrok туннель."""
    global _ngrok_proc
    if _ngrok_proc:
        _ngrok_proc.terminate()
        _ngrok_proc = None
    # На случай если запущен внешне
    import platform
    if platform.system() == "Windows":
        subprocess.run(["taskkill", "/f", "/im", "ngrok.exe"],
                       capture_output=True)
    return {"stopped": True}


@app.get("/api/ngrok/status")
def ngrok_status():
    """Проверить статус ngrok туннеля."""
    url = _ngrok_public_url()
    return {"active": bool(url), "url": url}


@app.get("/api/share")
def list_shares():
    """Список всех share-ссылок."""
    with _shares_lock:
        return _load_shares()


@app.delete("/api/share/{token}")
def delete_share(token: str):
    with _shares_lock:
        shares = _load_shares()
        if token not in shares:
            raise HTTPException(404, "Ссылка не найдена")
        del shares[token]
        _save_shares(shares)
    return {"ok": True}


@app.get("/api/share/{token}/clips")
def share_clips(token: str):
    """Данные клипов для публичной страницы."""
    with _shares_lock:
        shares = _load_shares()
    if token not in shares:
        raise HTTPException(404, "Ссылка не найдена или удалена")
    info = shares[token]
    config = _load_config()
    output_dir = _output_dir(config)
    clips_dir = output_dir / info["video_id"] / "clips_final"
    clips = []
    for mp4 in sorted(clips_dir.glob("*.mp4")):
        # Пробуем оба варианта имени файла метаданных
        meta_path = clips_dir / f"{mp4.stem}_meta.json"
        if not meta_path.exists():
            meta_path = mp4.with_suffix(".json")
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        clips.append({
            "filename":    mp4.name,
            "title":       meta.get("title", mp4.stem),
            "description": meta.get("description", ""),
            "tags":        meta.get("tags", []),
            "viral_score": _viral_score_payload(meta),
            "url":         f"/api/clips/video/{info['video_id']}/{mp4.name}",
            "download":    f"/api/clips/video/{info['video_id']}/{mp4.name}",
        })
    return {"title": info["title"], "created_at": info["created_at"], "clips": clips}


@app.get("/api/share/{token}/zip")
def download_zip(token: str):
    """Скачать все клипы проекта одним ZIP-архивом."""
    with _shares_lock:
        shares = _load_shares()
    if token not in shares:
        raise HTTPException(404, "Ссылка не найдена")
    info = shares[token]
    config = _load_config()
    output_dir = _output_dir(config)
    clips_dir = output_dir / info["video_id"] / "clips_final"
    mp4_files = sorted(clips_dir.glob("*.mp4"))
    if not mp4_files:
        raise HTTPException(404, "Клипы не найдены")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for mp4 in mp4_files:
            zf.write(mp4, mp4.name)
    buf.seek(0)

    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in info["title"])[:60]
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.zip"'},
    )


@app.get("/share/{token}", response_class=HTMLResponse)
def share_page(token: str):
    """Публичная страница проекта — открыта для всех у кого есть ссылка."""
    with _shares_lock:
        shares = _load_shares()
    if token not in shares:
        return HTMLResponse("<h2 style='font-family:sans-serif;padding:40px'>Ссылка не найдена или удалена.</h2>",
                            status_code=404)
    share_html = static_dir / "share.html"
    if share_html.exists():
        return HTMLResponse(share_html.read_text(encoding="utf-8"))
    return HTMLResponse("<h2>share.html not found</h2>", status_code=500)


# ─────────────────────────────────────────────────────────────────
# Static files & root
# ─────────────────────────────────────────────────────────────────
static_dir = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    ico = static_dir / "favicon.ico"
    return FileResponse(str(ico), media_type="image/x-icon")


@app.get("/")
def index():
    html = static_dir / "index.html"
    if not html.exists():
        return {"error": "static/index.html not found"}
    return FileResponse(
        str(html),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


# ─────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    local_ip = _get_local_ip()

    print("\n" + "=" * 55)
    print("  Video Clipper Web UI")
    print(f"  Локально:  http://127.0.0.1:8088")
    print(f"  В сети:    http://{local_ip}:8088")
    print("=" * 55 + "\n")

    uvicorn.run(app, host="127.0.0.1", port=8088, log_level="warning")

