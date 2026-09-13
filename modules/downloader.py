"""
Модуль 1: Скачивание видео (yt-dlp).

Скачивание видео по URL с YouTube или копирование из локальной папки.
Извлечение метаданных оригинала для контекста LLM.
"""

import logging
import json
import subprocess
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Поддерживаемые видеоформаты
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".wmv"}

# Дефолт: mux лучшего видео + лучшего аудио; merge mkv для VP9/AV1+Opus.
_DEFAULT_MERGE_FORMAT = "mkv"
# Пустая строка extractor_args = не передавать --extractor-args (дефолт yt-dlp,
# обычно android_vr + web_safari). Принудительный android,web даёт пропуск DASH-форматов
# без PO token и часто падает в 360p — см. wiki PO-Token / клиенты YouTube.
_DEFAULT_FORMAT_SORT_FORCE = "res,fps,br"

# Цепочка «сначала максимум по высоте»: для каждого порога — DASH (bv+ba), затем
# единый progressive best[height<=N]. Итог: 4K при наличии, иначе 1440, 1080, …
_DEFAULT_MAX_DOWNLOAD_HEIGHTS = (2160, 1440, 1080, 720)


COOKIES_PATH = Path("cookies/youtube.txt")
COOKIES_MAX_AGE_DAYS = 10
_BROWSER_CANDIDATES = ["chrome", "edge", "firefox"]

# Имена процессов для закрытия/перезапуска браузеров на Windows
_BROWSER_PROCESS: dict[str, str] = {
    "chrome": "chrome.exe",
    "edge":   "msedge.exe",
}
# Пути запуска браузеров для перезапуска
_BROWSER_LAUNCH: dict[str, str] = {
    "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "edge":   r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
}


def _is_browser_running(browser: str) -> bool:
    """Проверить, запущен ли браузер (только Chrome/Edge)."""
    proc = _BROWSER_PROCESS.get(browser)
    if not proc:
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/fi", f"imagename eq {proc}", "/fo", "csv", "/nh"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        return proc.lower() in result.stdout.lower()
    except Exception:
        return False


def _close_browser(browser: str) -> bool:
    """Закрыть Chrome/Edge через taskkill. Возвращает True если процесс был найден."""
    proc = _BROWSER_PROCESS.get(browser)
    if not proc or not _is_browser_running(browser):
        return False
    logger.info(f"Закрываю {browser} для извлечения куки (Chrome v130+ требует закрытый браузер)...")
    try:
        subprocess.run(["taskkill", "/f", "/im", proc], capture_output=True, timeout=15)
        time.sleep(3)  # ждём пока процесс завершится и файлы профиля освободятся
        logger.info(f"{browser} закрыт")
        return True
    except Exception as e:
        logger.debug(f"Не удалось закрыть {browser}: {e}")
        return False


def _reopen_browser(browser: str) -> None:
    """Перезапустить Chrome/Edge после извлечения куки. Chrome восстановит вкладки автоматически."""
    launch_path = _BROWSER_LAUNCH.get(browser)
    if not launch_path or not Path(launch_path).exists():
        logger.debug(f"Путь запуска {browser} не найден, пропускаю перезапуск")
        return
    try:
        subprocess.Popen([launch_path], creationflags=0x00000008)  # DETACHED_PROCESS
        logger.info(f"{browser} перезапущен (восстановит вкладки автоматически)")
    except Exception as e:
        logger.debug(f"Не удалось перезапустить {browser}: {e}")


def _extract_cookies_once(browser: str, cookies_path: Path) -> bool:
    """Одна попытка извлечь куки из браузера. Возвращает True при успехе."""
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--cookies-from-browser", browser,
                "--cookies", str(cookies_path),
                "--skip-download",
                "https://www.youtube.com/",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
        if result.returncode == 0 and cookies_path.exists() and cookies_path.stat().st_size > 1000:
            logger.info(f"Куки успешно извлечены из {browser} → {cookies_path}")
            return True
        logger.debug(f"Браузер {browser}: код {result.returncode}. stderr: {result.stderr[:300]}")
    except FileNotFoundError:
        logger.debug(f"Браузер {browser} не найден")
    except subprocess.TimeoutExpired:
        logger.debug(f"Таймаут при извлечении куки из {browser}")
    except Exception as e:
        logger.debug(f"Ошибка при извлечении куки из {browser}: {e}")
    return False


def _try_refresh_cookies(cookies_path: Path = COOKIES_PATH) -> bool:
    """
    Автоматически обновить куки YouTube из браузера.
    Для Chrome/Edge v130+: закрывает браузер → извлекает → перезапускает.
    Chrome восстановит все вкладки через встроенный session restore.
    Перебирает chrome → edge → firefox, останавливается на первом успехе.
    """
    import os
    cookies_path = Path(cookies_path)
    cookies_path.parent.mkdir(parents=True, exist_ok=True)

    browser_env = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
    browsers = (
        [browser_env] + [b for b in _BROWSER_CANDIDATES if b != browser_env]
        if browser_env
        else _BROWSER_CANDIDATES
    )

    for browser in browsers:
        logger.info(f"Автообновление куки: пробую {browser}...")

        # Firefox не требует закрытия — пробуем сразу
        if browser == "firefox":
            if _extract_cookies_once(browser, cookies_path):
                return True
            continue

        # Chrome/Edge v130+: сначала пробуем с открытым браузером
        if _extract_cookies_once(browser, cookies_path):
            return True

        # Не получилось — закрываем браузер и пробуем снова
        was_running = _close_browser(browser)
        if not was_running:
            logger.debug(f"{browser} не был запущен, извлечение уже пробовали — пропускаю")
            continue

        success = _extract_cookies_once(browser, cookies_path)
        _reopen_browser(browser)  # перезапускаем в любом случае

        if success:
            return True

    logger.warning("Автообновление куки не удалось ни для одного браузера.")
    return False


def _cookies_need_refresh(cookies_path: Path = COOKIES_PATH) -> bool:
    """True если файл куки отсутствует или старше COOKIES_MAX_AGE_DAYS дней."""
    p = Path(cookies_path)
    if not p.exists():
        return True
    age_days = (time.time() - p.stat().st_mtime) / 86400
    return age_days > COOKIES_MAX_AGE_DAYS


def _parse_height_ladder(raw) -> tuple[int, ...] | None:
    """Секция download.max_download_heights: список целых по убыванию желательно."""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        out: list[int] = []
        for x in raw:
            try:
                h = int(x)
                if h > 0:
                    out.append(h)
            except (TypeError, ValueError):
                continue
        return tuple(out) if out else None
    return None


def _max_quality_format_string(heights: tuple[int, ...] | None = None) -> str:
    """
    Селектор yt-dlp: на каждом уровне — лучшее видео не выше H + лучшее аудио,
    иначе лучший комбинированный поток не выше H; в конце — общие fallback.
    """
    hs = heights or _DEFAULT_MAX_DOWNLOAD_HEIGHTS
    parts: list[str] = []
    for h in hs:
        parts.append(f"bestvideo[height<={h}]+bestaudio")
        parts.append(f"best[height<={h}]")
    parts.append("bestvideo+bestaudio")
    parts.append("best")
    return "/".join(parts)


def _ytdlp_options_from_config(download_cfg: dict | None) -> dict:
    """Секция config.yaml → download."""
    d = download_cfg or {}
    fmt = (d.get("yt_dlp_format") or d.get("format") or "").strip()
    ladder_heights = _parse_height_ladder(d.get("max_download_heights"))
    if fmt:
        resolved_fmt = fmt
    else:
        resolved_fmt = _max_quality_format_string(ladder_heights)
    merge = (d.get("merge_output_format") or "").strip().lower()
    ex = (d.get("extractor_args") or "").strip()
    if "format_sort_force" not in d:
        fs_force = _DEFAULT_FORMAT_SORT_FORCE
    else:
        fsv = d.get("format_sort_force")
        if fsv is False:
            fs_force = ""
        else:
            fs_force = str(fsv or "").strip()
    cf_raw = d.get("concurrent_fragments")
    cf: int | None = None
    if cf_raw is not None:
        try:
            n = int(cf_raw)
            if n > 1:
                cf = n
        except (TypeError, ValueError):
            pass
    return {
        "format": resolved_fmt,
        "merge_output_format": merge or _DEFAULT_MERGE_FORMAT,
        "extractor_args": ex,
        "format_sort_force": fs_force,
        "concurrent_fragments": cf,
    }


def extract_video_id(url: str) -> Optional[str]:
    """
    Извлечь ID видео из YouTube URL.
    
    Args:
        url: YouTube URL (различные форматы).
    
    Returns:
        Video ID или None если не удалось извлечь.
    """
    patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'youtube\.com/embed/([a-zA-Z0-9_-]{11})',
        r'youtube\.com/v/([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def download_video(url: str, output_dir: Path, download_cfg: dict | None = None) -> dict:
    """
    Скачать видео с YouTube по URL.

    Args:
        url: YouTube URL.
        output_dir: Папка для сохранения (напр. temp/downloads/).

    Returns:
        dict с полями:
            - video_path: Path к скачанному файлу.
            - video_id: ID видео YouTube.
            - metadata: dict с title, description оригинала.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    video_id = extract_video_id(url)
    if not video_id:
        logger.error(f"Не удалось извлечь video_id из URL: {url}")
        raise ValueError(f"Некорректный YouTube URL: {url}")
    
    opts = _ytdlp_options_from_config(download_cfg)
    ex_args = (opts.get("extractor_args") or "").strip()
    fsf = (opts.get("format_sort_force") or "").strip()
    ex_log = repr(ex_args) if ex_args else "—"
    fsf_log = repr(fsf) if fsf else "—"
    logger.info(
        f"Скачивание видео {video_id} из {url} "
        f"(format={opts['format']!r}, merge={opts['merge_output_format']}, "
        f"extractor_args={ex_log}, format_sort_force={fsf_log})"
    )

    # Шаблон имени файла
    output_template = str(output_dir / f"{video_id}.%(ext)s")
    info_json_path = output_dir / f"{video_id}.info.json"
    
    # Команда yt-dlp
    cmd = [
        "yt-dlp",
        "--format", opts["format"],
        "--merge-output-format", opts["merge_output_format"],
        "--write-info-json",
        "--no-playlist",
        "--socket-timeout", "60",
        "--retries", "15",
        "--fragment-retries", "15",
        "--retry-sleep", "5",
        "--output", output_template,
    ]
    if ex_args:
        cmd.extend(["--extractor-args", ex_args])
    # --format-sort-force без аргумента (булев флаг); порядок полей — только через -S.
    if fsf:
        cmd.extend(["-S", fsf, "--format-sort-force"])
    if opts.get("concurrent_fragments"):
        cmd.extend(["--concurrent-fragments", str(opts["concurrent_fragments"])])
    
    import os

    # Node.js установлен — передать явно, чтобы yt-dlp не ждал Deno
    node_exe = os.environ.get("YTDLP_NODE_PATH", "node")
    cmd.extend(["--js-runtimes", f"node:{node_exe}"])

    # EJS-компонент для решения YouTube n-challenge (нужен для получения форматов)
    cmd.extend(["--remote-components", "ejs:github"])

    # Добавить proxy если задан в переменной окружения
    proxy = os.environ.get("YTDLP_PROXY") or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    if proxy:
        cmd.extend(["--proxy", proxy])
        logger.info(f"Использую прокси: {proxy}")

    # Авторизация через файл куки (обновляется вручную).
    if COOKIES_PATH.exists():
        if _cookies_need_refresh(COOKIES_PATH):
            logger.warning(
                f"Куки YouTube устарели (старше {COOKIES_MAX_AGE_DAYS} дней). "
                "Обновите вручную: скачайте cookies/youtube.txt через расширение «Get cookies.txt LOCALLY» на youtube.com."
            )
        cmd.extend(["--cookies", str(COOKIES_PATH)])
        logger.info(f"Используем cookies из файла {COOKIES_PATH}")
    else:
        logger.warning(
            "Куки YouTube не найдены. "
            "Скачайте cookies/youtube.txt через расширение «Get cookies.txt LOCALLY» на youtube.com."
        )

    cmd.append(url)

    def _run_ytdlp(command: list) -> None:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    try:
        _run_ytdlp(cmd)
        logger.info(f"yt-dlp завершён успешно для {video_id}")
    except subprocess.CalledProcessError as e:
        is_bot_error = "Sign in to confirm" in e.stderr or "not a bot" in e.stderr
        if is_bot_error:
            logger.error(
                "YouTube требует авторизацию. "
                "Обновите cookies/youtube.txt вручную через расширение «Get cookies.txt LOCALLY» на youtube.com."
            )
            raise RuntimeError(f"Не удалось скачать видео: {e.stderr}")
        else:
            logger.error(f"Ошибка при скачивании {url}: {e}")
            logger.error(f"stderr: {e.stderr}")
            raise RuntimeError(f"Не удалось скачать видео: {e.stderr}")
    
    # Найти скачанный файл
    video_path = output_dir / f"{video_id}.mp4"
    if not video_path.exists():
        # Попробовать другие расширения
        for ext in [".webm", ".mkv", ".mp4"]:
            alt_path = output_dir / f"{video_id}{ext}"
            if alt_path.exists():
                video_path = alt_path
                break
    
    if not video_path.exists():
        raise FileNotFoundError(f"Скачанный файл не найден: {video_path}")
    
    # Загрузить метаданные
    metadata = {}
    if info_json_path.exists():
        try:
            with open(info_json_path, "r", encoding="utf-8") as f:
                info = json.load(f)
                metadata = {
                    "title": info.get("title", ""),
                    "description": info.get("description", ""),
                    "uploader": info.get("uploader", ""),
                    "duration": info.get("duration", 0),
                    "view_count": info.get("view_count", 0),
                }
        except Exception as e:
            logger.warning(f"Не удалось загрузить метаданные из {info_json_path}: {e}")
    
    result = {
        "video_path": video_path,
        "video_id": video_id,
        "metadata": metadata,
    }
    
    logger.info(f"Видео скачано: {video_path} ({metadata.get('title', 'N/A')})")
    return result


def download_url_list(
    urls: list[str],
    output_dir: Path,
    download_cfg: dict | None = None,
) -> list[dict]:
    """
    Скачать видео по списку URL (порядок сохраняется; сбои по отдельным URL не рвут весь список).

    Args:
        urls: Список ссылок (пустые строки и строки с # в начале пропускаются).
        output_dir: Папка для сохранения.

    Returns:
        Список dict (как в download_video) для каждого успешно скачанного видео.
    """
    cleaned = [
        u.strip()
        for u in urls
        if u and str(u).strip() and not str(u).strip().startswith("#")
    ]
    if not cleaned:
        logger.warning("Список URL пуст после фильтрации")
        return []

    logger.info(f"Найдено {len(cleaned)} URL для скачивания (пакет)")
    results = []
    for i, url in enumerate(cleaned, 1):
        logger.info(f"[{i}/{len(cleaned)}] Обработка URL: {url}")
        try:
            results.append(download_video(url, output_dir, download_cfg=download_cfg))
        except Exception as e:
            logger.error(f"Не удалось скачать {url}: {e}")

    logger.info(f"Успешно скачано {len(results)} из {len(cleaned)} видео")
    return results


def download_batch(
    urls_file: Path,
    output_dir: Path,
    download_cfg: dict | None = None,
) -> list[dict]:
    """
    Скачать все видео из файла urls.txt.

    Args:
        urls_file: Путь к файлу со списком URL (по одному на строку).
        output_dir: Папка для сохранения.

    Returns:
        Список dict (как в download_video) для каждого успешно скачанного видео.
    """
    urls_file = Path(urls_file)
    if not urls_file.exists():
        raise FileNotFoundError(f"Файл URLs не найден: {urls_file}")

    with open(urls_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    urls = [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]

    logger.info(f"Найдено {len(urls)} URL для скачивания из {urls_file}")
    return download_url_list(urls, output_dir, download_cfg=download_cfg)


def get_local_videos(input_dir: Path) -> list[dict]:
    """
    Получить список локальных видеофайлов из папки или одиночного файла.

    Args:
        input_dir: Папка с видеофайлами или путь к одному видеофайлу.

    Returns:
        Список dict с video_path и video_id (имя файла без расширения).
    """
    input_path = Path(input_dir)
    if not input_path.exists():
        raise FileNotFoundError(f"Файл или папка не найдены: {input_path}")

    if input_path.is_file():
        if input_path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(f"Неподдерживаемый формат видео: {input_path.suffix}")
        logger.info(f"Загрузка локального видео из {input_path}")
        return [{"video_path": input_path, "video_id": input_path.stem, "metadata": {"title": input_path.stem}}]

    videos = []
    for file_path in sorted(input_path.iterdir()):
        if file_path.is_file() and file_path.suffix.lower() in VIDEO_EXTENSIONS:
            videos.append({
                "video_path": file_path,
                "video_id": file_path.stem,
                "metadata": {"title": file_path.stem},
            })

    logger.info(f"Найдено {len(videos)} локальных видеофайлов в {input_path}")
    return videos
