"""
Video Clipper — точка входа CLI.

Автоматическая нарезка длинных видео на короткие клипы для YouTube Shorts
с переводом на множество языков, заменой фонового аудио, субтитрами и
автоматической загрузкой по расписанию.

Использование:
    python main.py process --url "https://youtube.com/watch?v=..." --langs en,fr,de
    python main.py process --urls-file urls.txt --langs all
    python main.py process --input videos/ --langs en,fr
    python main.py clip --url "https://..." --no-translate
    python main.py auth --channel nanoplastic_fr
    python main.py scheduler start
    python main.py queue status
    python main.py upload --now
    python main.py quota status
"""

import argparse
import copy
import json
import logging
import os
import secrets
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv(__import__("pathlib").Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/processing.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("video-clipper")


def load_config(config_path: str = "config.yaml") -> dict:
    """Загрузить конфигурацию из YAML-файла."""
    path = Path(config_path)
    if not path.exists():
        logger.error(f"Файл конфигурации не найден: {config_path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# Пресеты качества: переопределяют секции video_encode и download из config.yaml.
# "high" — текущее поведение (берётся из config.yaml без изменений).
_QUALITY_OVERRIDES: dict[str, dict] = {
    "high": {},
    "medium": {
        "lossless_intermediate": False,
        "cut_x264_preset": "medium",  "cut_x264_crf": 20,
        "crop_x264_preset": "medium", "crop_x264_crf": 19,
        "burn_x264_preset": "medium", "burn_x264_crf": 18,
    },
    "fast": {
        "lossless_intermediate": False,
        "cut_x264_preset": "veryfast", "cut_x264_crf": 23,
        "crop_x264_preset": "veryfast", "crop_x264_crf": 22,
        "burn_x264_preset": "veryfast", "burn_x264_crf": 21,
    },
    "turbo": {
        "lossless_intermediate": False,
        "cut_x264_preset": "ultrafast", "cut_x264_crf": 28,
        "crop_x264_preset": "ultrafast", "crop_x264_crf": 27,
        "burn_x264_preset": "ultrafast", "burn_x264_crf": 26,
    },
}

# Максимальное разрешение скачивания по пресету (None = брать из config.yaml)
_QUALITY_MAX_HEIGHT: dict[str, int | None] = {
    "high": None,
    "medium": 1080,
    "fast": 1080,
    "turbo": 720,
}

_QUALITY_CROP_SIZE: dict[str, tuple[int, int] | None] = {
    "high": None,
    "medium": None,
    "fast": None,
    "turbo": (720, 1280),
}

# Второй проход Whisper (per-clip): None = брать из config.yaml
_QUALITY_PER_CLIP_WHISPER: dict[str, bool | None] = {
    "high": None,
    "medium": None,
    "fast": False,
    "turbo": False,
}


def _language_file_prefix(language: str) -> str:
    """Short safe language prefix for output filenames and clip titles."""
    raw = str(language or "").strip().lower()
    if not raw or raw in {"auto", "unknown", "und"}:
        return "xx"
    base = raw.replace("_", "-").split("-", 1)[0]
    prefix = "".join(ch for ch in base if ch.isalnum())
    return (prefix or "xx")[:8]


def _language_prefixed_stem(stem: str, language: str) -> str:
    prefix = _language_file_prefix(language)
    clean_stem = str(stem or "clip").strip() or "clip"
    if clean_stem.lower().startswith(f"{prefix}-"):
        return clean_stem
    return f"{prefix}-{clean_stem}"


def _language_prefixed_title(title: str, language: str, max_len: int = 100) -> str:
    prefix = _language_file_prefix(language)
    clean_title = " ".join(str(title or "").split())
    if clean_title.lower().startswith(f"{prefix}-"):
        return clean_title[:max_len].rstrip()
    out = f"{prefix}-{clean_title}" if clean_title else prefix
    if len(out) <= max_len:
        return out
    return out[:max_len].rstrip(" ,;:-")


def _video_encode_settings(config: dict, quality: str = "high") -> dict:
    """Параметры libx264 для нарезки, кропа и вшивания субтитров (секция video_encode)."""
    d = config.get("video_encode") or {}
    ov = _QUALITY_OVERRIDES.get(quality) or {}
    merged = {**d, **ov}
    crop_size = _QUALITY_CROP_SIZE.get(quality)
    tune = d.get("burn_x264_tune") or d.get("x264_tune")
    tune_s = str(tune).strip() if tune is not None else ""
    return {
        "cut_preset": str(merged.get("cut_x264_preset", "veryslow")),
        "cut_crf": int(merged.get("cut_x264_crf", 15)),
        "crop_preset": str(merged.get("crop_x264_preset", "veryslow")),
        "crop_crf": int(merged.get("crop_x264_crf", 14)),
        "burn_preset": str(merged.get("burn_x264_preset", "veryslow")),
        "burn_crf": int(merged.get("burn_x264_crf", 14)),
        "burn_tune": tune_s or None,
        # FFV1 на нарезке и кропе → один проход H.264 только при прожиге ASS.
        "lossless_intermediate": bool(merged.get("lossless_intermediate", False)),
        # None = не переопределять (брать из config.yaml / args)
        "per_clip_whisper_override": _QUALITY_PER_CLIP_WHISPER.get(quality),
        "crop_width": crop_size[0] if crop_size else None,
        "crop_height": crop_size[1] if crop_size else None,
    }


# ─────────────────────────────────────────────────────────────────
# Вспомогательные функции пайплайна
# ─────────────────────────────────────────────────────────────────

def _get_video_sources(args, config) -> list[dict]:
    """Шаг 1: получить список видео из URL / файла / локальной папки."""
    from modules import downloader
    from modules import video_normalizer

    paths = config.get("paths", {})
    temp_dir = Path(paths.get("temp_dir", "temp"))
    download_cfg = dict(config.get("download") or {})

    # Ограничение разрешения по пресету качества (medium/fast → max 1080p)
    quality = getattr(args, "encode_quality", "turbo")
    max_h = _QUALITY_MAX_HEIGHT.get(quality)
    if max_h is not None:
        current_heights = download_cfg.get("max_download_heights") or [2160, 1440, 1080, 720]
        download_cfg["max_download_heights"] = [h for h in current_heights if h <= max_h] or [max_h]
        logger.info(f"Пресет «{quality}»: ограничение скачивания до {max_h}p")

    urls_list = getattr(args, "urls_list", None)
    if urls_list:
        logger.info(f"Пакетное скачивание по списку из {len(urls_list)} URL")
        return downloader.download_url_list(
            urls_list, temp_dir / "downloads", download_cfg=download_cfg
        )

    if args.url:
        logger.info(f"Скачивание видео: {args.url}")
        return [
            downloader.download_video(
                args.url, temp_dir / "downloads", download_cfg=download_cfg
            )
        ]

    if getattr(args, "urls_file", None):
        logger.info(f"Пакетное скачивание из {args.urls_file}")
        return downloader.download_batch(
            Path(args.urls_file), temp_dir / "downloads", download_cfg=download_cfg
        )

    input_paths_list = getattr(args, "input_paths_list", None)
    if input_paths_list:
        logger.info(f"Локальные источники: {len(input_paths_list)} путей (файл или папка каждый)")
        merged: list[dict] = []
        for p in input_paths_list:
            merged.extend(downloader.get_local_videos(Path(p)))
        return video_normalizer.normalize_local_videos(merged, temp_dir, quality, max_h)

    if getattr(args, "input", None):
        logger.info(f"Загрузка локальных видео из {args.input}")
        videos = downloader.get_local_videos(Path(args.input))
        return video_normalizer.normalize_local_videos(videos, temp_dir, quality, max_h)

    logger.error("Необходимо указать --url, --urls-file или --input")
    return []


def _replace_background_audio(
    clip_path: Path,
    clip_meta: dict,
    config: dict,
    temp_dir: Path,
    video_id: str,
    clip_stem: str,
) -> Path:
    """
    Заменить фоновую музыку в клипе через Demucs + MusicGen.
    Возвращает путь к видео с новым аудио.
    """
    from modules import audio_separator
    from modules import clipper

    audio_config = config.get("audio", {})
    demucs_config = config.get("demucs", {})
    musicgen_config = config.get("musicgen", {})
    ducking_config = audio_config.get("ducking", {})
    bg_prompt = audio_config.get(
        "background_prompt",
        "dark ambient documentary texture, slow evolving drone"
    )

    clip_audio_path = temp_dir / f"{video_id}_{clip_stem}_audio.wav"
    clipper.extract_audio(clip_path, clip_audio_path)

    logger.info(f"  [1/5] Разделение аудио (Demucs)...")
    stems_dir = temp_dir / f"{video_id}_{clip_stem}_stems"
    stems = audio_separator.separate_audio(
        clip_audio_path,
        stems_dir,
        model=demucs_config.get("model", "htdemucs"),
        device=demucs_config.get("device", "cpu"),
    )
    vocals_path = stems["vocals"]
    background_path = stems["background"]

    logger.info(f"  [2/5] Генерация нового фона (MusicGen)...")
    new_bg_path = temp_dir / f"{video_id}_{clip_stem}_new_bg.wav"
    audio_separator.generate_background(
        background_path,
        duration=clip_meta["duration"],
        prompt=bg_prompt,
        model_name=musicgen_config.get("model", "facebook/musicgen-melody"),
        device=musicgen_config.get("device", "cpu"),
        output_path=new_bg_path,
    )

    logger.info(f"  [3/5] Применение ducking...")
    ducked_bg_path = temp_dir / f"{video_id}_{clip_stem}_ducked_bg.wav"
    audio_separator.apply_ducking(
        vocals_path,
        new_bg_path,
        duck_db=ducking_config.get("duck_db", -15.0),
        boost_db=ducking_config.get("boost_db", 6.0),
        threshold_db=ducking_config.get("threshold_db", -35.0),
        chunk_ms=ducking_config.get("chunk_ms", 500),
        output_path=ducked_bg_path,
    )

    logger.info(f"  [4/5] Микширование аудио...")
    final_audio_path = temp_dir / f"{video_id}_{clip_stem}_final_audio.wav"
    audio_separator.mix_audio(vocals_path, ducked_bg_path, final_audio_path)

    video_with_new_audio = temp_dir / f"{video_id}_{clip_stem}_new_audio.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-i", str(clip_path),
            "-i", str(final_audio_path),
            "-c:v", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            "-y",
            str(video_with_new_audio),
        ],
        check=True,
        capture_output=True,
    )
    return video_with_new_audio


def _ffmpeg_x264_transcode(src: Path, dst: Path, ve: dict) -> None:
    """Один проход libx264 (если после FFV1-кропа нет финального mp4 — нет слов ASS или сбой)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tune = ve.get("burn_tune")
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(src),
        "-c:v", "libx264",
        "-preset", ve["burn_preset"],
        "-crf", str(ve["burn_crf"]),
    ]
    if tune:
        cmd.extend(["-tune", tune])
    cmd.extend([
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(dst),
    ])
    subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _burn_subtitles(
    video_path: Path,
    clip_meta: dict,
    transcript: dict,
    args,
    config: dict,
    temp_dir: Path,
    video_id: str,
    clip_stem: str,
    overlap_buffer: float,
    detected_lang: str = "en",
    output_video_path: Path | None = None,
) -> None:
    """Вшить субтитры в видео на месте (заменяет файл) или в output_video_path."""
    from modules import subtitles as subtitles_mod
    from modules import clipper as clipper_mod

    subtitles_config = config.get("subtitles", {})
    sub_preset = getattr(args, "subtitle_preset", None) or subtitles_config.get("preset", "classic")
    sub_position = getattr(args, "subtitle_position", None) or None
    sub_size = getattr(args, "subtitle_size", None) or None
    if sub_size:
        try:
            sub_size = int(sub_size)
        except (ValueError, TypeError):
            sub_size = None

    _apc = getattr(args, "subtitle_per_clip_whisper", None)
    per_clip = bool(
        _apc if _apc is not None else subtitles_config.get("per_clip_whisper", True)
    )

    if per_clip:
        # Таймкоды отдельно на каждый готовый файл — совпадают с фактическим аудио
        # (обходит ошибки нумерации клипов, сдвиги после кропа и погрешность word-level).
        whisper_cfg = config.get("whisper", {})
        wav_clip = temp_dir / f"{video_id}_{clip_stem}_subs_src.wav"
        clipper_mod.extract_audio(video_path, wav_clip)
        wl = None if not detected_lang or detected_lang == "auto" else detected_lang
        local_tr = clipper_mod.transcribe(
            wav_clip,
            language=wl,
            model_name=whisper_cfg.get("model", "large-v3"),
            device=whisper_cfg.get("device", "cuda"),
        )
        # Исправить скрипт если Whisper выдал арабский/урду вместо целевого (bn/hi/etc)
        if wl:
            local_tr = clipper_mod.fix_transcript_script(local_tr, wl)
        clip_offset = 0.0
        clip_words = [
            {
                "word": w.get("word", ""),
                "start": max(0.0, w.get("start", 0)),
                "end": max(0.0, w.get("end", 0)),
                "probability": w.get("probability"),
            }
            for seg in local_tr.get("segments", [])
            for w in seg.get("words", [])
            if str(w.get("word", "")).strip()
        ]
        try:
            wav_clip.unlink(missing_ok=True)
        except OSError:
            pass
    else:
        clip_start = float(clip_meta["start_time"])
        clip_end = float(clip_meta["end_time"])
        margin = 1.0
        clip_offset = max(0.0, clip_start - overlap_buffer)
        clip_words = []
        for seg in transcript.get("segments", []):
            for w in seg.get("words", []):
                if not str(w.get("word", "")).strip():
                    continue
                ws = float(w.get("start", 0))
                we = float(w.get("end", 0))
                # Пересечение с интервалом клипа (не только «полностью внутри»)
                if we <= clip_start - margin or ws >= clip_end + margin:
                    continue
                clip_words.append({
                    "word": w.get("word", ""),
                    "start": max(0.0, ws - clip_offset),
                    "end": max(0.0, we - clip_offset),
                    "probability": w.get("probability"),
                })

    if not clip_words:
        logger.warning(f"  Нет слов для субтитров клипа {clip_stem}")
        return

    logger.info(f"  Субтитры [{sub_preset}] ({len(clip_words)} слов)...")
    ass_path = temp_dir / f"{video_id}_{clip_stem}_subs.ass"
    subtitles_mod.generate_ass(
        clip_words,
        ass_path,
        preset=sub_preset,
        position=sub_position,
        font_size_override=sub_size,
    )

    # Заголовок-хук поверх видео (первые N секунд сверху)
    hook_text = clip_meta.get("hook", "")
    show_hook = bool(getattr(args, "hook_title", False))
    if hook_text and show_hook:
        hook_style    = getattr(args, "hook_title_style", "card") or "card"
        hook_duration = float(getattr(args, "hook_title_duration", 3.5) or 3.5)
        subtitles_mod.add_hook_title(ass_path, hook_text, hook_duration, hook_style)

    subtitled_path = temp_dir / f"{video_id}_{clip_stem}_subtitled.mp4"
    ve = _video_encode_settings(config, quality=getattr(args, "encode_quality", "turbo"))
    subtitles_mod.burn_subtitles(
        video_path,
        ass_path,
        subtitled_path,
        x264_preset=ve["burn_preset"],
        x264_crf=ve["burn_crf"],
        x264_tune=ve.get("burn_tune"),
    )
    dest = output_video_path if output_video_path is not None else video_path
    # os.replace может упасть с WinError 5 если Windows Defender/антивирус держит файл.
    # Fallback: shutil.copy2 + удаление источника.
    import time as _time
    for _attempt in range(3):
        try:
            os.replace(str(subtitled_path), str(dest))
            break
        except OSError:
            if _attempt < 2:
                _time.sleep(1)
            else:
                import shutil as _shutil
                _shutil.copy2(str(subtitled_path), str(dest))
                subtitled_path.unlink(missing_ok=True)
    logger.info(f"  ✓ Субтитры вшиты")


def _process_single_clip(
    clip_path: Path,
    clip_meta: dict,
    clip_index: int,
    total_clips: int,
    transcript: dict,
    detected_lang: str,
    config: dict,
    args,
    temp_dir: Path,
    video_id: str,
    final_clips_dir: Path,
    overlap_buffer: float,
    used_titles: list[str] | None = None,
) -> dict | None:
    """
    Полная обработка одного клипа:
    аудио → кадрирование → субтитры → метаданные.

    Возвращает dict метаданных или None при ошибке.
    """
    from modules import cropper, metadata_gen
    from modules.viral_score import attach_viral_score

    audio_config = config.get("audio", {})
    crop_config = config.get("crop", {})
    ve = _video_encode_settings(config, quality=getattr(args, "encode_quality", "turbo"))
    use_lossless_chain = ve["lossless_intermediate"] and not getattr(args, "no_subtitles", False)
    # Если качество переопределяет per_clip_whisper и пользователь не задал явно — применяем
    if ve["per_clip_whisper_override"] is not None and getattr(args, "subtitle_per_clip_whisper", None) is None:
        args.subtitle_per_clip_whisper = ve["per_clip_whisper_override"]

    replace_background = audio_config.get("replace_background", True)
    crop_mode = getattr(args, "crop", None) or crop_config.get("mode", "center")
    crop_width = ve.get("crop_width") or crop_config.get("width", 1080)
    crop_height = ve.get("crop_height") or crop_config.get("height", 1920)
    crop_pan_frac = float(crop_config.get("min_vertical_pan_fraction", 0.10) or 0.10)

    clip_stem = clip_path.stem
    final_clip_stem = _language_prefixed_stem(clip_stem, detected_lang)
    logger.info(f"\n--- Обработка клипа {clip_index}/{total_clips}: {clip_path.name} ---")

    try:
        # Замена фона
        if replace_background:
            video_for_crop = _replace_background_audio(
                clip_path, clip_meta, config, temp_dir, video_id, clip_stem
            )
        else:
            logger.info(f"  [1/2] Замена аудио пропущена (replace_background: false)")
            video_for_crop = clip_path

        # Кадрирование 9:16 (+ субтитры: один H.264 при lossless_intermediate)
        step = "[2/2]" if not replace_background else "[5/5]"
        logger.info(f"  {step} Кадрирование {crop_mode} {crop_width}x{crop_height}...")
        final_video_path = final_clips_dir / f"{final_clip_stem}.mp4"
        cropper.set_crop_lossless_output(use_lossless_chain)
        cropped_temp: Path | None = None
        if use_lossless_chain:
            cropped_temp = temp_dir / f"{video_id}_{clip_stem}_cropped.mkv"
            cropper.crop_video(
                video_for_crop, cropped_temp, mode=crop_mode,
                width=crop_width, height=crop_height,
                min_vertical_pan_fraction=crop_pan_frac,
            )
        else:
            cropper.crop_video(
                video_for_crop, final_video_path, mode=crop_mode,
                width=crop_width, height=crop_height,
                min_vertical_pan_fraction=crop_pan_frac,
            )

        # Субтитры
        if not getattr(args, "no_subtitles", False):
            try:
                src_for_subs = cropped_temp if cropped_temp is not None else final_video_path
                _burn_subtitles(
                    src_for_subs, clip_meta, transcript,
                    args, config, temp_dir, video_id, clip_stem, overlap_buffer,
                    detected_lang=detected_lang,
                    output_video_path=final_video_path if cropped_temp is not None else None,
                )
            except Exception as sub_err:
                logger.warning(f"  Субтитры пропущены (ошибка): {sub_err}")

        if cropped_temp is not None:
            if not final_video_path.exists():
                logger.info("  Нет финального mp4 после кропа — кодирование из FFV1 без ASS...")
                _ffmpeg_x264_transcode(cropped_temp, final_video_path, ve)
            try:
                cropped_temp.unlink(missing_ok=True)
            except OSError:
                pass

        # Метаданные
        logger.info(f"  Генерация метаданных [{detected_lang}]...")
        desc_suffix = getattr(args, "description_suffix", "") or ""
        metadata = metadata_gen.generate_metadata(
            clip_meta.get("text", ""),
            language=detected_lang,
            config=config,
            description_suffix=desc_suffix,
            existing_titles=used_titles,
        )
        metadata["title"] = _language_prefixed_title(metadata.get("title", ""), detected_lang)
        attach_viral_score(metadata, clip_meta)
        metadata_gen.save_metadata(metadata, final_clips_dir / f"{final_clip_stem}_meta.json")
        if used_titles is not None and metadata.get("title"):
            used_titles.append(str(metadata["title"]))

        logger.info(f"  ✓ Клип {clip_index} готов: {final_video_path.name}")
        logger.info(f"    Заголовок: {metadata['title']}")
        viral = metadata.get("viral_score") or {}
        logger.info(f"    Viral score: {viral.get('score', '?')}/100")
        return metadata

    except Exception as exc:
        logger.error(f"  ✗ Ошибка при обработке клипа {clip_index}: {exc}", exc_info=True)
        return None


LANGUAGE_FOLDER_NAMES = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "pt": "Portuguese",
    "ru": "Russian",
    "de": "German",
    "ja": "Japanese",
    "hi": "Hindi",
    "ar": "Arabic",
    "zh": "Chinese",
    "bn": "Bengali",
    "it": "Italian",
    "ko": "Korean",
    "tr": "Turkish",
    "id": "Indonesian",
    "th": "Thai",
    "vi": "Vietnamese",
    "fil": "Filipino",
    "tl": "Filipino",
    "pa": "Punjabi",
    "te": "Telugu",
    "ms": "Malay",
    "ta": "Tamil",
    "mr": "Marathi",
    "gu": "Gujarati",
    "ro": "Romanian",
    "cs": "Czech",
    "hu": "Hungarian",
    "sv": "Swedish",
    "el": "Greek",
    "bg": "Bulgarian",
    "sr": "Serbian",
    "he": "Hebrew",
    "af": "Afrikaans",
    "hr": "Croatian",
    "fi": "Finnish",
    "da": "Danish",
    "no": "Norwegian",
    "sk": "Slovak",
    "lt": "Lithuanian",
    "sl": "Slovenian",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "ml": "Malayalam",
    "kn": "Kannada",
    "nl": "Dutch",
    "pl": "Polish",
    "yue": "Cantonese",
    "fa": "Persian",
    "uz": "Uzbek",
    "sw": "Swahili",
    "yo": "Yoruba",
    "am": "Amharic",
    "kk": "Kazakh",
    "ka": "Georgian",
    "hy": "Armenian",
}


def _language_folder_prefix(language: str) -> str:
    code = str(language or "").strip().lower().split("-")[0].split("_")[0]
    if not code or code == "auto":
        return ""
    return LANGUAGE_FOLDER_NAMES.get(code, code.upper())


def _new_project_folder_name(source_video_id: str, language: str = "") -> str:
    """
    Уникальное имя папки в output/ на каждый запуск.
    Один и тот же YouTube id → разные папки (не перезапись прошлой нарезки).
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suf = secrets.token_hex(2)
    vid = (source_video_id or "video").strip()
    prefix = _language_folder_prefix(language)
    prefix_part = f"{prefix}_" if prefix else ""
    max_vid_len = max(24, 120 - len(prefix_part) - len(stamp) - len(suf) - 2)
    base_vid = vid[:max_vid_len]
    base = f"{prefix_part}{base_vid}_{stamp}_{suf}"
    if len(base) > 120:
        base_vid = vid[:48]
        base = f"{prefix_part}{base_vid}_{stamp}_{suf}"
    return base


def _collect_existing_titles(config: dict, output_base: Path, language: str) -> list[str]:
    """Collect existing titles so new metadata does not repeat old uploads/clips."""
    paths = config.get("paths", {})
    roots = [output_base, Path(paths.get("queue_dir", "queue"))]
    titles: list[str] = []
    seen: set[str] = set()
    lang = str(language or "").strip()

    for root in roots:
        if not root.exists():
            continue
        for meta_path in root.rglob("*_meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            title = str(meta.get("title") or "").strip()
            if not title:
                continue
            meta_lang = str(meta.get("language") or "").strip()
            if lang and meta_lang and meta_lang != lang:
                continue
            key = title.casefold()
            if key in seen:
                continue
            seen.add(key)
            titles.append(title)
            prefix = _language_file_prefix(lang or meta_lang)
            if title.lower().startswith(f"{prefix}-"):
                unprefixed = title[len(prefix) + 1 :].strip()
                unprefixed_key = unprefixed.casefold()
                if unprefixed and unprefixed_key not in seen:
                    seen.add(unprefixed_key)
                    titles.append(unprefixed)
    return titles[-500:]


def _process_video(
    video: dict,
    video_idx: int,
    total_videos: int,
    config: dict,
    args,
    output_base: Path,
    temp_dir: Path,
) -> None:
    """Полная обработка одного видео: транскрипция → нарезка → обработка клипов."""
    from modules import clipper

    video_path = video["video_path"]
    video_id = video["video_id"]
    source_meta = video.get("metadata", {})
    source_lang = str(config.get("source_lang", "en") or "en").strip().lower()
    project_folder = _new_project_folder_name(video_id, source_lang)

    safe_title = source_meta.get("title", "N/A").encode("utf-8", errors="replace").decode("utf-8")
    logger.info("=" * 60)
    logger.info(f"[{video_idx}/{total_videos}] Видео: {video_path.name}")
    if source_meta.get("normalized_from"):
        original_name = source_meta.get("source_filename") or Path(source_meta["normalized_from"]).name
        logger.info(f"Исходный локальный файл: {original_name}")
        logger.info(f"Временная нормализованная копия: {video_path}")
    logger.info(f"ID источника: {video_id} | Папка проекта: {project_folder}")
    logger.info(f"Название: {safe_title}")
    logger.info("=" * 60)

    video_output_dir = output_base / project_folder
    video_output_dir.mkdir(parents=True, exist_ok=True)

    # Сохраняем метаданные проекта для UI (имя исходного файла, заголовок, язык)
    project_json_path = video_output_dir / "project.json"
    base_project_meta = {
        "video_id": video_id,
        "project_folder": project_folder,
        "source_filename": source_meta.get("source_filename") or video_path.name,
        "source_title": source_meta.get("title", ""),
    }
    if source_meta.get("normalized_from"):
        base_project_meta["normalized_from"] = source_meta.get("normalized_from")
        base_project_meta["normalized_to"] = source_meta.get("normalized_to")
        base_project_meta["normalized_max_height"] = source_meta.get("normalized_max_height")
    try:
        project_json_path.write_text(
            json.dumps(base_project_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    # Параметры из конфига
    clip_config = config.get("clip", {})
    max_clips = getattr(args, "max_clips", None) or clip_config.get("max_clips_per_video", 10)
    min_duration = clip_config.get("min_duration", 15)
    max_duration = clip_config.get("max_duration", 60)
    overlap_buffer = clip_config.get("overlap_buffer", 0.5)

    whisper_config = config.get("whisper", {})
    whisper_model = whisper_config.get("model", "large-v3")
    whisper_device = whisper_config.get("device", "cuda")

    try:
        # Этап 1: Извлечение аудио
        logger.info("[Этап 1/4] Извлечение аудио...")
        audio_path = temp_dir / f"{video_id}_audio.wav"
        clipper.extract_audio(video_path, audio_path)

        # Этап 2: Транскрипция
        logger.info("[Этап 2/4] Транскрипция Whisper...")
        whisper_lang = source_lang if source_lang and source_lang != "auto" else None
        transcript = clipper.transcribe(
            audio_path,
            language=whisper_lang,
            model_name=whisper_model,
            device=whisper_device,
        )
        detected_lang = transcript.get("language", source_lang)
        # Если пользователь явно указал язык (не "auto") — он имеет приоритет над
        # Whisper-детектом. Whisper при Bengali/Hindi аудио часто возвращает "ur"
        # (урду, арабский шрифт), что ломает per-clip Whisper и subtitle-фикс.
        if source_lang and source_lang not in ("auto", ""):
            if detected_lang != source_lang:
                logger.info(
                    f"Whisper определил язык '{detected_lang}', но явно указан '{source_lang}' — использую указанный"
                )
                detected_lang = source_lang
                transcript["language"] = detected_lang
        else:
            # Хинди и урду — одна речь, но разные скрипты; при авто-детекции
            # Whisper может выбрать ur (арабский шрифт) вместо hi (деванагари).
            if detected_lang == "ur":
                logger.warning("Whisper определил язык как 'ur' (урду) — заменяю на 'hi' (хинди) для деванагари-субтитров")
                transcript["language"] = "hi"
                detected_lang = "hi"
        if detected_lang != source_lang:
            logger.info(f"Whisper определил язык: {detected_lang} (в конфиге: {source_lang})")
        try:
            proj = dict(base_project_meta)
            if project_json_path.exists():
                proj = json.loads(project_json_path.read_text(encoding="utf-8")) or proj
            proj["language"] = detected_lang or ""
            project_json_path.write_text(
                json.dumps(proj, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

        transcript_path = video_output_dir / f"{project_folder}_transcript.json"
        with open(transcript_path, "w", encoding="utf-8") as f:
            json.dump(transcript, f, ensure_ascii=False, indent=2)
        logger.info(f"Транскрипт сохранён: {transcript_path}")

        # Этап 3: Выбор клипов LLM
        logger.info("[Этап 3/4] Выбор лучших клипов (LLM)...")
        selected_clips = clipper.select_clips(
            transcript,
            max_clips=max_clips,
            min_duration=min_duration,
            max_duration=max_duration,
            config=config,
        )
        selected_clips = clipper.validate_clips_for_source(
            video_path,
            selected_clips,
            min_duration=min_duration,
            buffer=overlap_buffer,
            transcript=transcript,
        )
        if not selected_clips:
            logger.warning(f"Не выбрано ни одного клипа для {video_id}")
            return

        clips_meta_path = video_output_dir / f"{project_folder}_clips.json"
        with open(clips_meta_path, "w", encoding="utf-8") as f:
            json.dump(selected_clips, f, ensure_ascii=False, indent=2)

        # Этап 4: Нарезка видео
        logger.info(f"[Этап 4/4] Нарезка на {len(selected_clips)} клипов...")
        clips_dir = video_output_dir / "clips_raw"
        ve = _video_encode_settings(config, quality=getattr(args, "encode_quality", "turbo"))
        from modules import cropper as cropper_mod

        cropper_mod.set_crop_encode(ve["crop_preset"], ve["crop_crf"])
        clip_paths = clipper.cut_clips(
            video_path,
            selected_clips,
            clips_dir,
            buffer=overlap_buffer,
            x264_preset=ve["cut_preset"],
            x264_crf=ve["cut_crf"],
            lossless_intermediate=ve["lossless_intermediate"],
        )
        n_ok = sum(1 for p in clip_paths if p is not None)
        if n_ok == 0:
            logger.warning(f"Не удалось нарезать клипы для {video_id}")
            return
        logger.info(f"✓ Нарезано {n_ok} клипов")

        # Обработка каждого клипа
        final_clips_dir = video_output_dir / "clips_final"
        final_clips_dir.mkdir(parents=True, exist_ok=True)
        used_titles = _collect_existing_titles(config, output_base, detected_lang)
        if used_titles:
            logger.info(f"Title uniqueness guard: {len(used_titles)} existing titles loaded for {detected_lang}")

        idx = 0
        for clip_path, clip_meta in zip(clip_paths, selected_clips):
            if clip_path is None:
                logger.warning(
                    f"Пропуск клипа {clip_meta.get('start_time', '?')}"
                    f"-{clip_meta.get('end_time', '?')}s: нарезка не удалась"
                )
                continue
            idx += 1
            _process_single_clip(
                clip_path, clip_meta, idx, n_ok,
                transcript, detected_lang,
                config, args, temp_dir, video_id, final_clips_dir, overlap_buffer,
                used_titles=used_titles,
            )

        logger.info(f"\n{'='*60}")
        logger.info(f"✓ Видео {video_id} полностью обработано (проект: {project_folder})")
        logger.info(f"  Финальные клипы: {final_clips_dir}")

        # Добавить клипы в очередь YouTube
        if getattr(args, "add_to_queue", False):
            from modules import queue_manager
            queue_base_dir = Path(config.get("paths", {}).get("queue_dir", "queue"))
            queue_language = detected_lang if source_lang in ("auto", "") else source_lang
            distributed = queue_manager.distribute_clips(
                final_clips_dir,
                config.get("channels", {}),
                queue_base_dir,
                language=queue_language,
            )
            for channel, count in distributed.items():
                logger.info(f"    → {channel}: {count} клипов")

        logger.info(f"{'='*60}")

    except Exception as exc:
        logger.error(f"✗ Ошибка при обработке {video_id}: {exc}", exc_info=True)


# ─────────────────────────────────────────────────────────────────
# CLI-команды
# ─────────────────────────────────────────────────────────────────

def cmd_process(args, config):
    """Обработка видео: скачивание → нарезка → субтитры → очередь."""
    logger.info("=== Запуск обработки видео ===")

    if not hasattr(args, "no_subtitles"):
        args.no_subtitles = False
    if not hasattr(args, "add_to_queue"):
        args.add_to_queue = False

    if args.langs and args.langs != "auto":
        config["source_lang"] = args.langs

    paths = config.get("paths", {})
    output_base = Path(paths.get("output_dir", "output"))
    temp_dir = Path(paths.get("temp_dir", "temp"))

    videos = _get_video_sources(args, config)
    if not videos:
        logger.error("Нет видео для обработки")
        return

    logger.info(f"Всего видео для обработки: {len(videos)}")
    cancel_event = getattr(args, "cancel_event", None)
    for idx, video in enumerate(videos, 1):
        if cancel_event and cancel_event.is_set():
            logger.info("⏹ Обработка остановлена пользователем.")
            raise InterruptedError("Отменено пользователем")
        try:
            _process_video(video, idx, len(videos), config, args, output_base, temp_dir)
        except InterruptedError:
            raise
        except Exception as exc:
            vid_id = video.get("video_id", f"видео #{idx}")
            logger.error(
                f"✗ Необработанная ошибка при обработке {vid_id} [{idx}/{len(videos)}]: {exc}",
                exc_info=True,
            )
            logger.info("⚠ Пропускаем это видео, продолжаем батч...")

    logger.info(f"\n{'='*60}")
    logger.info("=== Обработка завершена ===")
    logger.info(f"Результаты в: {output_base}")
    logger.info(f"{'='*60}")


def cmd_clip(args, config):
    """Только нарезка (без перевода)."""
    logger.info("=== Нарезка без перевода ===")
    args_copy = copy.copy(args)
    args_copy.langs = "none"
    args_copy.urls_file = None
    if not getattr(args_copy, "max_clips", None):
        args_copy.max_clips = 10
    if not getattr(args_copy, "crop", None):
        args_copy.crop = "center"
    if not hasattr(args_copy, "add_to_queue"):
        args_copy.add_to_queue = False
    if not hasattr(args_copy, "no_subtitles"):
        args_copy.no_subtitles = False
    cmd_process(args_copy, config)


def cmd_auth(args, config):
    """Авторизация YouTube-канала (OAuth 2.0)."""
    from modules import uploader

    channel_name = args.channel
    logger.info(f"=== Авторизация канала: {channel_name} ===")

    channels = config.get("channels", {})
    if channel_name not in channels:
        logger.error(f"Канал {channel_name} не найден в config.yaml")
        logger.info(f"Доступные каналы: {', '.join(channels.keys())}")
        return

    channel_config = channels[channel_name]
    google_account = channel_config.get("google_account")
    account_groups = config.get("account_groups", {})

    if google_account not in account_groups:
        logger.error(f"Google account {google_account} не найден в config.yaml")
        return

    account_config = account_groups[google_account]
    client_secret = Path(account_config.get("client_secret"))
    token_file = Path(channel_config.get("token_file"))

    try:
        uploader.authenticate_channel(client_secret, token_file)
        logger.info(f"✓ Авторизация успешна для {channel_name}")
        logger.info(f"Токен сохранён: {token_file}")
    except Exception as exc:
        logger.error(f"Ошибка авторизации: {exc}", exc_info=True)


def cmd_scheduler(args, config):
    """Управление планировщиком загрузки."""
    from modules import uploader

    action = args.action
    logger.info(f"=== Планировщик: {action} ===")

    if action == "start":
        logger.info("Запуск фонового планировщика...")
        try:
            uploader.start_scheduler(config)
        except KeyboardInterrupt:
            logger.info("Планировщик остановлен пользователем")

    elif action == "stop":
        logger.info("Для остановки планировщика нажмите Ctrl+C в окне где он запущен")

    elif action == "status":
        channels = config.get("channels", {})
        logger.info("Конфигурация планировщика:")
        for channel_name, channel_config in channels.items():
            schedule = channel_config.get("schedule_utc", [])
            times = ", ".join(schedule) if schedule else "нет расписания"
            logger.info(f"  {channel_name}: {times} UTC")

    else:
        logger.error(f"Неизвестное действие: {action}")


def cmd_queue(args, config):
    """Просмотр очереди загрузки."""
    from modules import queue_manager

    action = args.action
    queue_dir = Path(config.get("paths", {}).get("queue_dir", "queue"))

    if action == "status":
        logger.info("=== Статус очереди ===")
        status = queue_manager.get_queue_status(queue_dir, config.get("channels", {}))
        if not any(status.values()):
            logger.info("Все очереди пусты")
            return
        total = 0
        for channel_name, count in sorted(status.items()):
            if count > 0:
                print(f"  {channel_name}: {count} клипов")
                total += count
        print(f"\nВсего в очередях: {total} клипов")

    elif action == "clear":
        if not getattr(args, "channel", None):
            logger.error("Укажите канал: --channel <channel_name>")
            return
        logger.info(f"Очистка очереди {args.channel}...")
        queue_manager.clear_queue(args.channel, queue_dir)

    else:
        logger.error(f"Неизвестное действие: {action}")


def cmd_upload(args, config):
    """Немедленная загрузка всей очереди."""
    from modules import uploader, quota_manager

    logger.info("=== Немедленная загрузка ===")
    if not args.now:
        logger.info("Используйте --now для подтверждения немедленной загрузки")
        return

    channels = config.get("channels", {})
    account_groups = config.get("account_groups", {})
    queue_dir = Path(config.get("paths", {}).get("queue_dir", "queue"))
    qm = quota_manager.QuotaManager(account_groups)
    total_uploaded = 0

    for channel_name, channel_config in channels.items():
        logger.info(f"\n--- Обработка канала: {channel_name} ---")
        try:
            results = uploader.process_queue(
                channel_name, channel_config, queue_dir,
                quota_manager=qm, account_groups=account_groups,
            )
            uploaded = sum(1 for r in results if r.get("status") == "success")
            total_uploaded += uploaded
            logger.info(f"Загружено: {uploaded}/{len(results)} видео")
        except Exception as exc:
            logger.error(f"Ошибка при обработке {channel_name}: {exc}", exc_info=True)

    logger.info(f"\n{'='*60}")
    logger.info(f"Всего загружено: {total_uploaded} видео")
    logger.info(f"{'='*60}")


def cmd_quota(args, config):
    """Показать статус квот YouTube API."""
    from modules.quota_manager import QuotaManager

    qm = QuotaManager(config.get("account_groups", {}))
    status = qm.get_status()
    print("\n=== Статус квот YouTube API ===\n")
    for group_id, info in status.items():
        print(f"  {group_id}:")
        print(f"    Использовано: {info['used']}/{info['quota']} units")
        print(f"    Осталось: {info['remaining']} units ({info['uploads_left']} загрузок)")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Video Clipper — нарезка и дистрибуция видео для YouTube"
    )
    subparsers = parser.add_subparsers(dest="command", help="Команда")

    p_process = subparsers.add_parser("process", help="Обработать видео (полный пайплайн)")
    p_process.add_argument("--url", help="YouTube URL для обработки")
    p_process.add_argument("--urls-file", help="Файл со списком URL")
    p_process.add_argument("--input", help="Папка с локальными видео")
    p_process.add_argument("--langs", default="all",
                           help="Целевые языки через запятую или 'all'")
    p_process.add_argument("--crop", default="smart",
                           choices=["center", "face", "smart", "template", "dual", "stretch_bg"],
                           help="Режим кадрирования (по умолчанию: smart)")
    p_process.add_argument("--max-clips", type=int, default=10,
                           help="Максимум клипов из одного видео")
    p_process.add_argument("--add-to-queue", action="store_true",
                           help="Добавить готовые клипы в очередь YouTube")
    p_process.add_argument("--no-subtitles", action="store_true",
                           help="Не вшивать субтитры в клипы")
    p_process.add_argument("--hook-title", action="store_true",
                           help="Показать заголовок-хук первые секунды (по умолчанию выкл.)")
    p_process.add_argument("--encode-quality", default="turbo",
                           choices=["high", "medium", "fast", "turbo"],
                           help="Encoding quality preset")
    p_process.add_argument("--config", default="config.yaml",
                           help="Путь к файлу конфигурации")

    p_clip = subparsers.add_parser("clip", help="Только нарезка (без перевода)")
    p_clip.add_argument("--url", help="YouTube URL")
    p_clip.add_argument("--input", help="Локальный файл или папка")
    p_clip.add_argument("--no-translate", action="store_true",
                        help="Без перевода (только исходный язык)")
    p_clip.add_argument("--max-clips", type=int, default=None,
                        help="Максимум клипов (по умолчанию: 10)")
    p_clip.add_argument("--crop", default="smart",
                        choices=["center", "face", "smart", "template", "dual", "stretch_bg"],
                        help="Режим кадрирования (по умолчанию: smart)")
    p_clip.add_argument("--add-to-queue", action="store_true",
                        help="Добавить готовые клипы в очередь YouTube")
    p_clip.add_argument("--no-subtitles", action="store_true",
                        help="Не вшивать субтитры в клипы")
    p_clip.add_argument("--hook-title", action="store_true",
                        help="Показать заголовок-хук первые секунды (по умолчанию выкл.)")
    p_clip.add_argument("--encode-quality", default="turbo",
                        choices=["high", "medium", "fast", "turbo"],
                        help="Encoding quality preset")
    p_clip.add_argument("--config", default="config.yaml")

    p_auth = subparsers.add_parser("auth", help="Авторизовать YouTube-канал")
    p_auth.add_argument("--channel", required=True, help="Имя канала из конфига")
    p_auth.add_argument("--config", default="config.yaml")

    p_sched = subparsers.add_parser("scheduler", help="Управление планировщиком")
    p_sched.add_argument("action", choices=["start", "stop", "status"])
    p_sched.add_argument("--config", default="config.yaml")

    p_queue = subparsers.add_parser("queue", help="Статус очереди загрузки")
    p_queue.add_argument("action", nargs="?", default="status",
                         choices=["status", "clear"])
    p_queue.add_argument("--channel", help="Имя канала (для action=clear)")
    p_queue.add_argument("--config", default="config.yaml")

    p_upload = subparsers.add_parser("upload", help="Загрузить очередь сейчас")
    p_upload.add_argument("--now", action="store_true",
                          help="Загрузить немедленно")
    p_upload.add_argument("--config", default="config.yaml")

    p_quota = subparsers.add_parser("quota", help="Статус квот YouTube API")
    p_quota.add_argument("action", nargs="?", default="status",
                         choices=["status", "reset"])
    p_quota.add_argument("--config", default="config.yaml")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    config = load_config(getattr(args, "config", "config.yaml"))

    commands = {
        "process":   cmd_process,
        "clip":      cmd_clip,
        "auth":      cmd_auth,
        "scheduler": cmd_scheduler,
        "queue":     cmd_queue,
        "upload":    cmd_upload,
        "quota":     cmd_quota,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args, config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
