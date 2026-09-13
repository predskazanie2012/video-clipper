"""
Нормализация слишком тяжёлых локальных исходников перед основным пайплайном.

Для YouTube-ссылок downloader уже ограничивает качество по пресету. Локальным
файлам нужна такая же защита, иначе 4K-исходник делает smart crop и кодирование
неожиданно медленными. Оригинальные файлы никогда не изменяются.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


_LOCAL_NORMALIZE_ENCODE: dict[str, tuple[str, int]] = {
    "medium": ("medium", 20),
    "fast": ("veryfast", 22),
    "turbo": ("ultrafast", 25),
}


def probe_video_size(video_path: Path) -> tuple[int, int] | None:
    """Вернуть размер первой видеодорожки как (width, height), либо None при ошибке."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        streams = (json.loads(result.stdout or "{}").get("streams") or [])
        if not streams:
            return None
        return int(streams[0]["width"]), int(streams[0]["height"])
    except Exception as exc:
        logger.warning(f"Не удалось проверить разрешение {video_path.name}: {exc}")
        return None


def _local_normalized_path(src: Path, normalize_dir: Path, max_height: int) -> Path:
    src_resolved = src.resolve()
    st = src_resolved.stat()
    key = f"{src_resolved}|{st.st_size}|{st.st_mtime_ns}|{max_height}".encode(
        "utf-8",
        errors="ignore",
    )
    token = hashlib.sha1(key).hexdigest()[:10]
    safe_stem = "".join(
        ch if ch.isalnum() or ch in "._-" else "_"
        for ch in src.stem
    ).strip("._-")
    safe_stem = safe_stem[:60] or "video"
    return normalize_dir / f"{safe_stem}_{token}_{max_height}p.mp4"


def normalize_local_video(
    video: dict,
    temp_dir: Path,
    quality: str,
    max_height: int | None,
) -> dict:
    """Создать уменьшенную временную копию для слишком большого локального файла."""
    if max_height is None:
        return video

    src = Path(video["video_path"])
    size = probe_video_size(src)
    if not size:
        return video

    width, height = size
    if height <= max_height:
        logger.info(
            f"Локальный файл уже не выше {max_height}p: {src.name} ({width}x{height})"
        )
        return video

    normalize_dir = temp_dir / "local_normalized"
    normalize_dir.mkdir(parents=True, exist_ok=True)
    dst = _local_normalized_path(src, normalize_dir, max_height)
    preset, crf = _LOCAL_NORMALIZE_ENCODE.get(quality, ("veryfast", 22))

    reuse_existing = False
    if dst.exists() and dst.stat().st_size > 0:
        dst_size = probe_video_size(dst)
        if dst_size and dst_size[1] <= max_height:
            reuse_existing = True
            logger.info(f"Использую готовую нормализованную копию: {dst.name}")
        else:
            logger.info(
                f"Нормализованная копия повреждена или неверного размера, пересоздаю: {dst.name}"
            )

    if not reuse_existing:
        logger.info(
            f"Нормализация локального видео: {src.name} {width}x{height} -> {max_height}p "
            f"(пресет {quality}, оригинал не изменяется)"
        )
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i", str(src),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-sn",
            "-dn",
            "-vf", f"scale=-2:{max_height}:flags=lanczos,setsar=1",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "160k",
            "-movflags", "+faststart",
            str(dst),
        ]
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.CalledProcessError as exc:
            try:
                dst.unlink(missing_ok=True)
            except OSError:
                pass
            stderr_tail = (exc.stderr or "").strip().splitlines()[-5:]
            logger.warning(
                f"Нормализация не удалась для {src.name}, обрабатываю оригинал. "
                f"FFmpeg: {' | '.join(stderr_tail)}"
            )
            return video

    normalized = dict(video)
    metadata = dict(normalized.get("metadata") or {})
    metadata.setdefault("title", src.stem)
    metadata["source_filename"] = src.name
    metadata["source_path"] = str(src)
    metadata["normalized_from"] = str(src)
    metadata["normalized_to"] = str(dst)
    metadata["normalized_max_height"] = max_height
    normalized["metadata"] = metadata
    normalized["video_path"] = dst
    return normalized


def normalize_local_videos(
    videos: list[dict],
    temp_dir: Path,
    quality: str,
    max_height: int | None,
) -> list[dict]:
    """Нормализовать слишком большие локальные видео по активному пресету качества."""
    if max_height is None:
        return videos
    return [
        normalize_local_video(video, temp_dir, quality, max_height)
        for video in videos
    ]
