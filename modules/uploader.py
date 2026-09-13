"""
Модуль 8: Загрузка видео на YouTube и планировщик.

YouTube Data API v3 upload + APScheduler для отложенной публикации.
"""

import logging
import json
import os
import uuid
import random
import socket
import ssl
from pathlib import Path
from datetime import datetime, timedelta, timezone
import time

import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import threading

from modules import schedule_store
from modules.comment_policy import effective_upload_limit
from modules.language_guard import LanguageChannelMismatch, assert_clip_language_matches_channel

logger = logging.getLogger(__name__)

_MAX_UPLOAD_RETRIES = 6
_RETRIABLE_HTTP_STATUS_CODES = {500, 502, 503, 504}
_RETRIABLE_UPLOAD_ERROR_MARKERS = (
    "EOF occurred in violation of protocol",
    "A connection attempt failed",
    "connected host has failed to respond",
    "Unable to find the server",
    "getaddrinfo failed",
    "The read operation timed out",
    "Connection reset",
    "Connection aborted",
    "ConnectionError",
    "socket.timeout",
    "TimeoutError",
    "RemoteDisconnected",
    "SSLError",
    "SSL",
)


def _is_retriable_upload_error(exc: Exception) -> bool:
    """Return True for transient network/server errors during resumable upload."""
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        return status in _RETRIABLE_HTTP_STATUS_CODES

    if isinstance(
        exc,
        (
            httplib2.HttpLib2Error,
            OSError,
            ssl.SSLError,
            socket.timeout,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return True

    err_str = str(exc)
    return any(marker in err_str for marker in _RETRIABLE_UPLOAD_ERROR_MARKERS)


def is_quota_error(exc: Exception) -> bool:
    """Return True when Google API quota is exhausted for the current project."""
    return "quotaExceeded" in str(exc)


def is_upload_limit_error(exc: Exception) -> bool:
    """Return True when YouTube temporarily refuses more uploads for the account."""
    return "uploadLimitExceeded" in str(exc)


def is_recoverable_queue_error(exc: Exception) -> bool:
    """Return True for errors where the clip should stay in queue and retry later."""
    return (
        is_quota_error(exc)
        or is_upload_limit_error(exc)
        or _is_retriable_upload_error(exc)
    )


def _upload_retry_delay(attempt: int) -> float:
    """Exponential backoff with a little jitter, capped at one minute."""
    return min(2 ** attempt + random.random(), 60.0)


def _minutes_since_slot(now_utc: datetime, time_str: str) -> float:
    hour, minute = map(int, time_str.split(':'))
    slot = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot > now_utc:
        slot -= timedelta(days=1)
    return (now_utc - slot).total_seconds() / 60

# Per-channel locks — предотвращают одновременный запуск двух process_queue для одного канала
_channel_locks: dict[str, threading.Lock] = {}
_channel_locks_mutex = threading.Lock()


def _get_channel_lock(channel_name: str) -> threading.Lock:
    with _channel_locks_mutex:
        if channel_name not in _channel_locks:
            _channel_locks[channel_name] = threading.Lock()
        return _channel_locks[channel_name]


def _lock_path(video_path: Path) -> Path:
    """Путь к атомарному lock-файлу для видео."""
    return video_path.with_name(video_path.name + ".lock")


def _try_lock_file(video_path: Path) -> bool:
    """
    Атомарно создать lock-файл (O_CREAT | O_EXCL).
    Возвращает True если лок успешно захвачен, False если файл уже заблокирован.
    """
    try:
        fd = os.open(str(_lock_path(video_path)), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_lock_file(video_path: Path) -> None:
    """Удалить lock-файл."""
    _lock_path(video_path).unlink(missing_ok=True)


def _cleanup_stale_locks(queue_dir: Path, max_age_sec: int = 7200) -> None:
    """Удалить зависшие lock-файлы старше max_age_sec секунд (умерший процесс, краш)."""
    for lf in queue_dir.glob("*.mp4.lock"):
        try:
            if time.time() - lf.stat().st_mtime > max_age_sec:
                lf.unlink(missing_ok=True)
                logger.warning(f"Удалён устаревший lock-файл: {lf.name}")
        except Exception:
            pass


# YouTube API scopes
SCOPES = ['https://www.googleapis.com/auth/youtube.upload',
          'https://www.googleapis.com/auth/youtube',
          'https://www.googleapis.com/auth/youtube.force-ssl']


def _missing_scopes(creds) -> list[str]:
    try:
        granted = set(creds.scopes or [])
        return [scope for scope in SCOPES if scope not in granted]
    except Exception:
        return []


def authenticate_channel(client_secret_path: Path, token_file: Path) -> object:
    """
    Авторизоваться для YouTube-канала через OAuth 2.0.
    При первом запуске — откроет браузер для авторизации.
    При повторном — использует сохранённый refresh-token.

    Args:
        client_secret_path: Путь к client_secret.json от Google Cloud.
        token_file: Путь для сохранения/загрузки токена.

    Returns:
        Авторизованный YouTube API service object.
    """
    client_secret_path = Path(client_secret_path)
    token_file = Path(token_file)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    
    creds = None
    
    # Загрузить существующий токен
    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
            logger.info(f"Токен загружен из {token_file}")
        except Exception as e:
            logger.warning(f"Не удалось загрузить токен: {e}")

    if creds:
        missing_scopes = _missing_scopes(creds)
        if missing_scopes:
            raise RuntimeError(
                "Токен YouTube устарел: не хватает новых прав API "
                f"({', '.join(missing_scopes)}). "
                "Откройте «Каналы» и нажмите «Обновить токен» для этого канала."
            )
    
    # Если токена нет или он недействителен
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Обновление токена...")
            creds.refresh(Request())
        else:
            logger.info("Необходима авторизация через браузер...")
            if not client_secret_path.exists():
                raise FileNotFoundError(f"Client secret не найден: {client_secret_path}")
            
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secret_path),
                SCOPES
            )
            creds = flow.run_local_server(port=8080)
        
        # Сохранить токен
        with open(token_file, 'w') as f:
            f.write(creds.to_json())
        logger.info(f"Токен сохранён: {token_file}")
    
    # Создать YouTube API service
    service = build('youtube', 'v3', credentials=creds)
    logger.info("YouTube API service авторизован")
    return service


def upload_video(service, video_path: Path, metadata: dict,
                 publish_at: datetime = None, privacy_status: str = "private") -> str:
    """
    Загрузить видео на YouTube.

    Args:
        service: YouTube API service (из authenticate_channel).
        video_path: Путь к видеофайлу.
        metadata: dict с title, description, tags, category_id.
        publish_at: Время публикации (UTC). Если None — публикуется сразу.
        privacy_status: "private", "public", "unlisted".

    Returns:
        video_id загруженного видео.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Видео не найдено: {video_path}")
    
    logger.info(f"Загрузка видео: {video_path.name}")
    logger.info(f"  Заголовок: {metadata.get('title', 'N/A')}")
    
    # Подготовка метаданных
    body = {
        'snippet': {
            'title': metadata.get('title', video_path.stem)[:100],
            'description': metadata.get('description', ''),
            'tags': metadata.get('tags', []),
            'categoryId': metadata.get('category_id', '28')
        },
        'status': {
            'privacyStatus': privacy_status,
            'selfDeclaredMadeForKids': False,
        }
    }
    
    # Отложенная публикация
    if publish_at:
        # YouTube требует ISO 8601 формат
        body['status']['publishAt'] = publish_at.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        body['status']['privacyStatus'] = 'private'  # Должно быть private для scheduled
        logger.info(f"  Отложенная публикация: {publish_at.strftime('%Y-%m-%d %H:%M UTC')}")
    
    # MediaFileUpload
    media = MediaFileUpload(
        str(video_path),
        chunksize=1024*1024,  # 1MB chunks
        resumable=True
    )
    
    # Запрос на загрузку
    request = service.videos().insert(
        part='snippet,status',
        body=body,
        media_body=media
    )
    
    # Загрузка с прогрессом
    response = None
    retry_count = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            retry_count = 0
            if status:
                progress = int(status.progress() * 100)
                logger.info(f"  Загружено: {progress}%")
        except Exception as e:
            if _is_retriable_upload_error(e) and retry_count < _MAX_UPLOAD_RETRIES:
                retry_count += 1
                delay = _upload_retry_delay(retry_count)
                logger.warning(
                    f"Временная ошибка загрузки ({retry_count}/{_MAX_UPLOAD_RETRIES}), "
                    f"повтор через {delay:.1f} с: {e}"
                )
                time.sleep(delay)
                continue
            if is_recoverable_queue_error(e):
                logger.warning(f"Загрузка отложена: {e}")
            else:
                logger.error(f"Ошибка при загрузке: {e}")
            raise
    
    video_id = response['id']
    logger.info(f"✓ Видео загружено: https://youtube.com/watch?v={video_id}")
    
    return video_id




def process_queue(channel_name: str, channel_config: dict,
                  queue_base_dir: Path, quota_manager=None,
                  account_groups: dict = None,
                  max_per_day: int = None,
                  schedule_file: Path = None) -> list[dict]:
    """
    Обработать очередь загрузки для одного канала.

    Args:
        channel_name: Имя канала из конфига.
        channel_config: Конфигурация канала.
        queue_base_dir: Базовая папка очередей (queue/).
        quota_manager: QuotaManager для отслеживания квот.
        max_per_day: Максимум загрузок в день на канал (None = без лимита).
        schedule_file: Путь к schedule.json для отслеживания дневных загрузок.

    Returns:
        Список dict с результатами загрузки (video_id, status).
    """
    lock = _get_channel_lock(channel_name)
    if not lock.acquire(blocking=False):
        logger.warning(f"[{channel_name}] process_queue уже выполняется — пропускаем дублирующий вызов")
        return []

    try:
        return _process_queue_locked(
            channel_name, channel_config, queue_base_dir,
            quota_manager, account_groups, max_per_day, schedule_file
        )
    finally:
        lock.release()


def _process_queue_locked(channel_name: str, channel_config: dict,
                          queue_base_dir: Path, quota_manager=None,
                          account_groups: dict = None,
                          max_per_day: int = None,
                          schedule_file: Path = None) -> list[dict]:
    """Внутренняя реализация process_queue — вызывается только под channel-lock."""
    queue_dir = queue_base_dir / channel_name
    if not queue_dir.exists():
        logger.info(f"Очередь для {channel_name} пуста (папка не существует)")
        return []

    # Убрать зависшие локи (краш / убитый процесс)
    _cleanup_stale_locks(queue_dir)

    # Найти видео в очереди — пропустить файлы с активным lock
    all_mp4 = sorted(queue_dir.glob("*.mp4"))
    video_files = [v for v in all_mp4 if not _lock_path(v).exists()]
    if not video_files:
        if all_mp4:
            logger.info(f"Очередь для {channel_name}: все файлы уже загружаются (lock active)")
        else:
            logger.info(f"Очередь для {channel_name} пуста")
        return []

    total_pending = len(video_files)
    logger.info(f"Обработка очереди {channel_name}: {total_pending} видео в очереди")

    # Применить дневной лимит
    if max_per_day:
        sf = schedule_file or (queue_base_dir / "schedule.json")
        uploaded_today = schedule_store.count_uploads_today(
            channel_name, schedule_store.load_safe(sf)
        )
        remaining = max_per_day - uploaded_today
        if remaining <= 0:
            logger.info(f"[{channel_name}] Дневной лимит {max_per_day} достигнут (загружено сегодня: {uploaded_today}), пропускаем")
            return []
        if remaining < total_pending:
            logger.info(f"[{channel_name}] Лимит {max_per_day}/день, загружено сегодня: {uploaded_today}, загружаем: {remaining} из {total_pending}")
            video_files = video_files[:remaining]
    
    # Авторизация — client_secret берём из account_groups по ключу google_account
    google_account = channel_config.get("google_account", "account_group_1")
    if account_groups and google_account in account_groups:
        client_secret = Path(account_groups[google_account].get(
            "client_secret", "secrets/client_1.json"
        ))
    else:
        client_secret = Path(channel_config.get("client_secret", "secrets/client_1.json"))
    token_file = Path(channel_config.get("token_file", f"tokens/{channel_name}.json"))
    
    try:
        service = authenticate_channel(client_secret, token_file)
    except Exception as e:
        logger.error(f"Не удалось авторизоваться для {channel_name}: {e}")
        return []
    
    # Проверка квоты
    if quota_manager:
        project_id = quota_manager.get_project_for_upload()
        if not project_id:
            logger.warning(f"Квоты исчерпаны для всех проектов. Загрузка отложена.")
            return []
    
    results = []
    schedule_times = channel_config.get("schedule_utc", [])
    language = channel_config.get("language", "")
    sf = schedule_file or (queue_base_dir / "schedule.json")

    for i, video_path in enumerate(video_files):
        # Атомарно захватить lock-файл — защита от конкурентных процессов/потоков
        if not _try_lock_file(video_path):
            logger.warning(f"[{channel_name}] {video_path.name} уже заблокирован другим процессом — пропускаем")
            continue

        # Загрузить метаданные — сначала {stem}_meta.json, fallback {stem}.json
        meta_path = video_path.parent / f"{video_path.stem}_meta.json"
        if not meta_path.exists():
            meta_path = video_path.with_suffix('.json')
        if meta_path.exists():
            with open(meta_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
        else:
            metadata = {
                "title": video_path.stem,
                "description": "",
                "tags": ["nanoplastics", "microplastics"],
                "category_id": "28"
            }

        try:
            assert_clip_language_matches_channel(metadata, channel_name, channel_config, video_path.name)
        except LanguageChannelMismatch as exc:
            logger.error(f"[{channel_name}] {exc}")
            _release_lock_file(video_path)
            results.append({
                "video_path": str(video_path),
                "video_id": None,
                "status": "error",
                "error": str(exc),
            })
            continue

        # Вычислить время публикации: каждый клип в свой уникальный слот
        # slot_idx — позиция внутри суток, day_offset — номер суток
        publish_at = None
        if schedule_times:
            now = datetime.now(timezone.utc)
            slot_idx = i % len(schedule_times)
            day_offset = i // len(schedule_times)
            hour, minute = map(int, schedule_times[slot_idx].split(':'))
            publish_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if publish_at <= now:
                publish_at += timedelta(days=1)
            if day_offset > 0:
                publish_at += timedelta(days=day_offset)

        # Создать запись в schedule.json до загрузки
        entry_id = str(uuid.uuid4())[:8]
        cal_entry = {
            "id": entry_id,
            "channel": channel_name,
            "language": language,
            "filename": video_path.name,
            "title": metadata.get("title", video_path.stem),
            "publish_date": publish_at.strftime("%Y-%m-%d") if publish_at else "",
            "publish_time": publish_at.strftime("%H:%M") if publish_at else "",
            "status": "uploading",
            "youtube_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        schedule_store.append_entry(sf, cal_entry)

        try:
            video_id = upload_video(service, video_path, metadata, publish_at)
            
            # Записать квоту
            if quota_manager:
                quota_manager.record_upload(google_account)

            schedule_store.update_entry(sf, entry_id, status="scheduled", youtube_id=video_id)
            # Убрать старые failed-записи для этого файла (мусор от прошлых неудач)
            removed = schedule_store.clean_stale_failed(sf, channel_name, video_path.name)
            if removed:
                logger.info(f"[{channel_name}] Удалено {removed} устаревших failed-записей для {video_path.name}")

            results.append({
                "video_path": str(video_path),
                "video_id": video_id,
                "status": "success",
                "publish_at": publish_at.isoformat() if publish_at else None
            })
            
            # Удалить из очереди вместе с lock-файлом
            video_path.unlink(missing_ok=True)
            if meta_path.exists():
                meta_path.unlink(missing_ok=True)
            _release_lock_file(video_path)

        except Exception as e:
            err_str = str(e)
            if is_recoverable_queue_error(e):
                logger.warning(f"Загрузка отложена для {video_path.name}: {e}")
            else:
                logger.error(f"Ошибка при загрузке {video_path.name}: {e}")

            is_quota = is_quota_error(e)
            is_limit = is_upload_limit_error(e)
            # Временные сетевые ошибки — файл остаётся в очереди для повторной попытки
            is_network = _is_retriable_upload_error(e)

            if is_quota or is_limit:
                # Файл остаётся в очереди — убираем lock чтобы следующий слот смог попробовать
                schedule_store.remove_entry(sf, entry_id)
                _release_lock_file(video_path)
                reason = "Квота GCP исчерпана" if is_quota else "Лимит загрузок YouTube исчерпан"
                logger.warning(f"[{channel_name}] {reason} — прерываем загрузку, файлы остаются в очереди")
                break
            elif is_network:
                # Сетевая ошибка — файл остаётся в очереди, попробуем в следующий слот
                schedule_store.remove_entry(sf, entry_id)
                _release_lock_file(video_path)
                logger.warning(f"[{channel_name}] Сетевая ошибка: {err_str[:120]} — файл остаётся в очереди")
            else:
                schedule_store.update_entry(sf, entry_id, status="failed", error=err_str)
                _release_lock_file(video_path)

            results.append({
                "video_path": str(video_path),
                "video_id": None,
                "status": "error",
                "error": err_str
            })
    
    return results


def start_scheduler(
    config: dict,
    stop_event: "threading.Event | None" = None,
    pause_callback=None,
):
    """
    Запустить фоновый планировщик загрузки.
    Блокирует поток до тех пор пока stop_event не будет установлен
    (или до KeyboardInterrupt при запуске из CLI).

    Args:
        config:     Полный конфиг приложения.
        stop_event: threading.Event для мягкой остановки из app.py.
    """
    import threading as _threading
    from modules.quota_manager import QuotaManager

    yt_cfg = config.get("youtube") or {}
    if not bool(yt_cfg.get("uploads_enabled", False)):
        logger.warning("YouTube uploads disabled in config.yaml; scheduler not started")
        return
    if not bool(yt_cfg.get("scheduler_enabled", False)):
        logger.warning("YouTube scheduler disabled in config.yaml; scheduler not started")
        return

    scheduler = BackgroundScheduler(job_defaults={"misfire_grace_time": 300})

    channels       = config.get("channels", {})
    account_groups = config.get("account_groups", {})
    queue_dir      = Path(config.get("paths", {}).get("queue_dir", "queue"))

    quota_manager = QuotaManager(account_groups)
    schedule_file = queue_dir / "schedule.json"
    max_per_day   = config.get("youtube", {}).get("max_uploads_per_day", None)
    startup_cutoff = datetime.now(timezone.utc)
    catchup_minutes = int(config.get("youtube", {}).get("startup_catchup_minutes", 90))

    if max_per_day:
        logger.info(f"Дневной лимит загрузок: {max_per_day} видео/канал")

    def _guarded_process_queue(
        channel_name,
        channel_config,
        queue_dir,
        quota_manager,
        account_groups,
        max_per_day,
        schedule_file,
    ):
        if pause_callback and pause_callback():
            logger.warning("YouTube upload skipped: video cutting is active")
            return []
        limit_info = effective_upload_limit(config, channel_name)
        channel_limit = limit_info.get("effective_max_uploads_per_day", max_per_day)
        if limit_info.get("reason") == "comments_active" and channel_limit != max_per_day:
            logger.info(
                "[%s] comment-aware limit: %s uploads/day (base %s, comments %s)",
                channel_name,
                channel_limit,
                max_per_day,
                limit_info.get("comments_total", 0),
            )
        return process_queue(
            channel_name,
            channel_config,
            queue_dir,
            quota_manager,
            account_groups,
            channel_limit,
            schedule_file,
        )

    interrupted = schedule_store.clean_interrupted_uploading(schedule_file, queue_dir, startup_cutoff)
    if interrupted:
        logger.warning(f"Очищено зависших записей uploading после перезапуска: {interrupted}")

    logger.info(f"Запуск планировщика для {len(channels)} каналов")

    for channel_name, channel_config in channels.items():
        if not channel_config.get("channel_id"):
            continue
        schedule_times = channel_config.get("schedule_utc", [])
        if not schedule_times:
            logger.warning(f"Нет расписания для {channel_name}, пропускаем")
            continue
        for time_str in schedule_times:
            hour, minute = map(int, time_str.split(':'))
            scheduler.add_job(
                func=_guarded_process_queue,
                trigger=CronTrigger(hour=hour, minute=minute, timezone='UTC'),
                args=[channel_name, channel_config, queue_dir, quota_manager,
                      account_groups, max_per_day, schedule_file],
                id=f"{channel_name}_{time_str}",
                name=f"Upload {channel_name} at {time_str} UTC",
                replace_existing=True,
            )
            logger.info(f"  Добавлено: {channel_name} в {time_str} UTC")

    scheduler.start()
    logger.info("Планировщик запущен.")

    if catchup_minutes > 0:
        now_utc = datetime.now(timezone.utc)
        for channel_name, channel_config in channels.items():
            if not channel_config.get("channel_id"):
                continue
            schedule_times = channel_config.get("schedule_utc", [])
            if not schedule_times:
                continue
            minutes_after_slot = min(_minutes_since_slot(now_utc, t) for t in schedule_times)
            if 0 <= minutes_after_slot <= catchup_minutes:
                logger.warning(
                    f"[{channel_name}] catch-up пропущенного слота "
                    f"({minutes_after_slot:.0f} мин назад) — запускаем очередь сейчас"
                )
                threading.Thread(
                    target=_guarded_process_queue,
                    args=[
                        channel_name, channel_config, queue_dir, quota_manager,
                        account_groups, max_per_day, schedule_file,
                    ],
                    daemon=True,
                ).start()

    try:
        if stop_event is not None:
            # Режим app.py — ждём сигнала остановки
            while not stop_event.is_set():
                time.sleep(1)
        else:
            # Режим CLI — ждём Ctrl+C
            while True:
                time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Остановка планировщика...")
        scheduler.shutdown(wait=False)
        logger.info("Планировщик остановлен")
