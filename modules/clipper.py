"""
Модуль 2: Умная нарезка видео на клипы.

faster-whisper транскрипция -> LLM выбор фрагментов -> FFmpeg нарезка.
"""

import gc
import json
import logging
import math
import os
import re
import subprocess
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from openai import OpenAI

logger = logging.getLogger(__name__)


WHISPER_LANGUAGE_ALIASES = {
    # ISO 639-2 / YouTube often use "fil" for Filipino, but Whisper expects
    # the Tagalog code "tl".
    "fil": "tl",
    "tgl": "tl",
    "tagalog": "tl",
    "filipino": "tl",
}


def _normalize_whisper_language(language: Optional[str]) -> Optional[str]:
    """Return a language code accepted by faster-whisper, or None for auto."""
    if not language:
        return None

    code = str(language).strip().lower()
    if not code or code == "auto":
        return None

    return WHISPER_LANGUAGE_ALIASES.get(code, code)


def _cut_ffmpeg_tail(x264_preset: str = "veryslow", x264_crf: int = 15) -> list[str]:
    """
    Нарезка: перекодирование видео (точный старт кадра, синхрон с субтитрами).
    Параметры — из config video_encode (после кропа всё равно идёт libx264, но
    слабый первый проход накапливает артефакты).
    """
    return [
        "-c:v", "libx264",
        "-preset", (x264_preset or "veryslow").strip(),
        "-crf", str(int(x264_crf)),
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
    ]


def _cut_ffmpeg_tail_lossless() -> list[str]:
    """
    Нарезка без поколения H.264: FFV1 в MKV (точный старт кадра как у libx264).
    Дальше кроп в FFV1 → один финальный libx264 при прожиге субтитров.
    """
    return [
        "-c:v", "ffv1",
        "-level", "3",
        "-coder", "1",
        "-context", "1",
        "-c:a", "copy",
        "-avoid_negative_ts", "make_zero",
    ]

# Языки, где арабский скрипт в Whisper-выводе означает ошибку скрипта,
# и нужна конвертация ur→hi через Google Translate.
_ARABIC_RE = re.compile(r'[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]')

# Языки, требующие фикса скрипта: целевой язык перевода.
# Если Whisper выдаёт арабский/урду скрипт для этих языков,
# текст конвертируется через Google Translate ur→{target}.
_SCRIPT_FIX_LANGS: dict[str, str] = {
    "hi": "hi",   # хинди (деванагари)
    "mr": "mr",   # маратхи (деванагари)
    "bn": "bn",   # бенгальский (বাংলা)
    "pa": "pa",   # панджаби (гурмукхи) — у некоторых носителей пакистанский вариант в арабском шрифте
    "gu": "gu",   # гуджарати
    "te": "te",   # телугу
    "ta": "ta",   # тамильский
}


_TARGET_SCRIPT_RE: dict[str, re.Pattern] = {
    "bn": re.compile(r"[\u0980-\u09FF]"),
    "hi": re.compile(r"[\u0900-\u097F]"),
    "mr": re.compile(r"[\u0900-\u097F]"),
    "pa": re.compile(r"[\u0A00-\u0A7F]"),
    "gu": re.compile(r"[\u0A80-\u0AFF]"),
    "ta": re.compile(r"[\u0B80-\u0BFF]"),
    "te": re.compile(r"[\u0C00-\u0C7F]"),
    "am": re.compile(r"[\u1200-\u137F]"),
    "si": re.compile(r"[\u0D80-\u0DFF]"),
    "km": re.compile(r"[\u1780-\u17FF]"),
    "my": re.compile(r"[\u1000-\u109F]"),
}
_LATIN_RE = re.compile(r"[A-Za-z]")
_NUMBERISH_RE = re.compile(r"^[\s\d.,:;!?()\[\]{}+\-]+$")


def _word_probability(word: dict) -> float | None:
    try:
        value = word.get("probability")
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _is_script_noise_word(text: str, language: str, probability: float | None = None) -> bool:
    text = str(text or "").strip()
    if not text:
        return True

    script_re = _TARGET_SCRIPT_RE.get(language)
    if not script_re:
        return False

    has_target_script = bool(script_re.search(text))
    if _LATIN_RE.search(text) and not has_target_script:
        return True

    if probability is not None:
        if _NUMBERISH_RE.fullmatch(text) and probability < 0.35:
            return True
        if not has_target_script and probability < 0.15:
            return True

    return False


def clean_transcript_noise(transcript: dict, language: str) -> dict:
    """Remove Whisper watermark/OCR-like noise for non-Latin script languages."""
    language = _normalize_whisper_language(language) or str(language or "").lower()
    script_re = _TARGET_SCRIPT_RE.get(language)
    if not script_re or not isinstance(transcript, dict):
        return transcript

    segments = transcript.get("segments") or []
    cleaned_segments: list[dict] = []
    removed_words = 0
    removed_segments = 0

    for seg in segments:
        words = list(seg.get("words") or [])
        probs = [_word_probability(w) for w in words]
        finite_probs = [p for p in probs if p is not None]
        max_prob = max(finite_probs, default=1.0)
        latin_noise = [
            w for w in words
            if _LATIN_RE.search(str(w.get("word", "")))
            and not script_re.search(str(w.get("word", "")))
        ]

        # A short all-low-confidence segment with a Latin token is usually OCR/watermark
        # leakage, not speech. This catches cases like "17 ... iaqmeadbooks".
        if words and latin_noise and max_prob < 0.08:
            removed_words += len(words)
            removed_segments += 1
            continue

        if words:
            kept_words = []
            for w in words:
                word_text = str(w.get("word", "")).strip()
                if _is_script_noise_word(word_text, language, _word_probability(w)):
                    removed_words += 1
                    continue
                kept_words.append(w)

            if not kept_words:
                removed_segments += 1
                continue

            seg = dict(seg)
            seg["words"] = kept_words
            seg["text"] = " ".join(str(w.get("word", "")).strip() for w in kept_words).strip()
            cleaned_segments.append(seg)
            continue

        text = str(seg.get("text", "")).strip()
        if _is_script_noise_word(text, language, None):
            removed_segments += 1
            continue
        cleaned_segments.append(seg)

    if removed_words or removed_segments:
        logger.info(
            "Transcript noise cleanup [%s]: removed %s words, %s segments",
            language,
            removed_words,
            removed_segments,
        )

    transcript["segments"] = cleaned_segments
    transcript["full_text"] = " ".join(
        str(s.get("text", "")).strip() for s in cleaned_segments if str(s.get("text", "")).strip()
    )
    return transcript


def _gtranslate_single(text: str, tl: str) -> str:
    """Перевести одну строку через Google Translate. При ошибке возвращает оригинал."""
    import requests as _req
    if not text.strip():
        return text
    url = "https://translate.googleapis.com/translate_a/single"
    params = {"client": "gtx", "sl": "ur", "tl": tl, "dt": "t", "q": text}
    try:
        r = _req.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        return "".join(chunk[0] for chunk in data[0] if chunk[0]).strip() or text
    except Exception:
        return text


def _gtranslate_chunk(texts: list[str], tl: str) -> list[str]:
    """
    Перевести небольшой список строк одним запросом.
    При несовпадении разделителя — переводит каждую строку отдельно.
    """
    import requests as _req
    if not texts:
        return texts
    if len(texts) == 1:
        return [_gtranslate_single(texts[0], tl)]

    SEP = "\n⏎\n"
    joined = SEP.join(texts)
    url = "https://translate.googleapis.com/translate_a/single"
    params = {"client": "gtx", "sl": "ur", "tl": tl, "dt": "t", "q": joined}
    try:
        r = _req.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        translated = "".join(chunk[0] for chunk in data[0] if chunk[0])
        parts = [p.strip() for p in translated.split("⏎")]
        if len(parts) == len(texts):
            return parts
        # Разделитель «съело» переводом — fallback поштучно
        logger.warning(
            f"Batch translate: ожидал {len(texts)} частей, получил {len(parts)} — "
            "переключаюсь на поштучный перевод"
        )
        return [_gtranslate_single(t, tl) for t in texts]
    except Exception as e:
        logger.warning(f"Google Translate chunk ошибка: {e} — переключаюсь на поштучный перевод")
        return [_gtranslate_single(t, tl) for t in texts]


def _gtranslate_batch(texts: list[str], tl: str) -> list[str]:
    """
    Перевести список строк через бесплатный Google Translate API.
    Разбивает на чанки по 15 элементов — иначе длинные видео переполняют URL
    и Google Translate ломает разделитель (→ урду-текст не конвертируется).
    """
    CHUNK_SIZE = 15
    results: list[str] = []
    for i in range(0, len(texts), CHUNK_SIZE):
        results.extend(_gtranslate_chunk(texts[i:i + CHUNK_SIZE], tl))
    return results


def fix_transcript_script(transcript: dict, language: str) -> dict:
    """Публичная обёртка для вызова из main.py (per-clip Whisper)."""
    fixed = _fix_transcript_script(transcript, language)
    return clean_transcript_noise(fixed, language)


def _fix_transcript_script(transcript: dict, language: str) -> dict:
    """
    Если транскрипт содержит арабский/урду скрипт, а язык — индийский/бенгальский/etc,
    конвертировать все тексты сегментов и слов в правильный скрипт.
    Использует Google Translate (ur→target) батчевыми запросами по 15 элементов.
    Покрывает: hi, mr, bn, pa, gu, te, ta и другие языки из _SCRIPT_FIX_LANGS.
    """
    tl = _SCRIPT_FIX_LANGS.get(language)
    if not tl:
        return transcript

    full_text = transcript.get("full_text", "")
    if not _ARABIC_RE.search(full_text):
        return transcript

    logger.warning(
        f"Whisper выдал арабский скрипт для языка '{language}' — "
        "конвертирую через Google Translate ur→hi..."
    )

    segments = transcript.get("segments", [])

    # Собираем уникальные слова для батча
    all_words: list[str] = []
    for seg in segments:
        for w in seg.get("words", []):
            all_words.append(w.get("word", ""))

    seg_texts = [seg.get("text", "") for seg in segments]

    # Два батчевых запроса: сегменты и слова
    translated_segs = _gtranslate_batch(seg_texts, tl) if seg_texts else []
    translated_words = _gtranslate_batch(all_words, tl) if all_words else []

    word_idx = 0
    for i, seg in enumerate(segments):
        if i < len(translated_segs):
            seg["text"] = translated_segs[i]
        for w in seg.get("words", []):
            if word_idx < len(translated_words):
                w["word"] = translated_words[word_idx]
            word_idx += 1

    transcript["full_text"] = " ".join(s["text"] for s in segments)
    return transcript


def extract_audio(video_path: Path, output_path: Path) -> Path:
    """
    Извлечь аудиодорожку из видео в WAV mono 16kHz.

    Args:
        video_path: Путь к видеофайлу.
        output_path: Путь для сохранения WAV.

    Returns:
        Path к WAV-файлу.
    """
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    logger.info(f"Извлечение аудио из {video_path.name}")
    
    cmd = [
        "ffmpeg",
        "-i", str(video_path),
        "-vn",  # Без видео
        "-acodec", "pcm_s16le",  # PCM 16-bit
        "-ar", "16000",  # 16kHz
        "-ac", "1",  # Mono
        "-y",  # Перезаписать если существует
        str(output_path)
    ]
    
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        logger.info(f"Аудио извлечено: {output_path}")
        return output_path
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка FFmpeg при извлечении аудио: {e.stderr}")
        raise RuntimeError(f"Не удалось извлечь аудио: {e.stderr}")


def transcribe(audio_path: Path, language: str = "en",
               model_name: str = "large-v3", device: str = "cuda") -> dict:
    """
    Транскрибировать аудио с помощью faster-whisper (4-8x быстрее openai-whisper).

    Args:
        audio_path: Путь к WAV-файлу.
        language: Язык аудио (None или "auto" → автоопределение).
        model_name: Модель Whisper (tiny/base/small/medium/large-v3).
        device: "cuda" или "cpu".

    Returns:
        dict с полями:
            - segments: list[dict] с start, end, text, words[].
            - full_text: полный текст транскрипции.
            - language: определённый язык.
    """
    audio_path = Path(audio_path)

    # faster-whisper сам обнаруживает CUDA; явно переключаем при отсутствии
    try:
        import torch
        if device == "cuda" and not torch.cuda.is_available():
            logger.warning("CUDA недоступна, переключаюсь на CPU")
            device = "cpu"
    except ImportError:
        device = "cpu"

    # int8 на CPU быстрее; float16 на GPU — лучшее качество
    compute_type = "int8" if device == "cpu" else "float16"

    logger.info(f"Транскрипция {audio_path.name} "
                f"(faster-whisper {model_name}, {device}, {compute_type})")

    # language=None -> auto-detection; some app/Youtube codes need Whisper aliases.
    whisper_lang = _normalize_whisper_language(language)
    if language and str(language).strip().lower() not in {"auto", whisper_lang or ""}:
        logger.info(f"Whisper language alias: {language} -> {whisper_lang}")

    # initial_prompt с символами целевого скрипта — фикс для языков,
    # где Whisper путает скрипты (хинди ↔ урду, китайский ↔ кантонский и т.п.)
    SCRIPT_PROMPTS: dict[str, str] = {
        "hi": "नमस्ते। यह वीडियो हिंदी में है।",   # деванагари → не урду
        "pa": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ।",                    # гурмукхи
        "ta": "வணக்கம்.",                           # тамильский
        "te": "నమస్కారం.",                           # телугу
        "mr": "नमस्कार.",                           # маратхи (тоже деванагари)
        "gu": "નમસ્તે.",                            # гуджарати
        "yue": "你好，這是廣東話。",                  # кантонский (традиционные иероглифы)
        "bn": "নমস্কার। এই ভিডিওটি বাংলায়।",       # бенгальский скрипт → не урду/латиница
        "am": "ሰላም። ይህ ቪዲዮ በአማርኛ ነው።",            # амхарский (эфиопский скрипт)
        "si": "ආයුබෝවන්.",                          # сингальский
        "km": "សួស្តី។",                             # кхмерский
        "my": "မင်္ဂလာပါ။",                         # мьянманский (бирманский)
    }
    initial_prompt = SCRIPT_PROMPTS.get(whisper_lang) if whisper_lang else None

    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_name, device=device, compute_type=compute_type)

        # vad_filter=True — VAD встроенный, пропускает тишину и музыку без речи
        segments_gen, info = model.transcribe(
            str(audio_path),
            language=whisper_lang,
            initial_prompt=initial_prompt,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )

        # Материализуем генератор (faster-whisper возвращает lazy iterator)
        segments = []
        for seg in segments_gen:
            words = []
            if seg.words:
                for w in seg.words:
                    words.append({
                        "word": w.word,
                        "start": w.start,
                        "end": w.end,
                        "probability": w.probability,
                    })
            segments.append({
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip(),
                "words": words,
            })

        # Фильтруем silence/music маркеры: Whisper выводит ". . . . ." для тишины и музыки.
        # vad_filter их иногда пропускает. Без фильтрации эти сегменты занимают
        # большую часть транскрипта → GPT не видит реальный контент → 1 клип в итоге.
        _SILENCE_PAT = re.compile(r'^[\s.]+$')
        before_count = len(segments)
        segments = [s for s in segments if not _SILENCE_PAT.fullmatch(s["text"])]
        if len(segments) < before_count:
            logger.info(f"Отфильтровано {before_count - len(segments)} silence-сегментов "
                        f"('. . .' / тишина), осталось {len(segments)}")

        full_text = " ".join(s["text"] for s in segments)
        detected_lang = info.language or (language if language and language != "auto" else "en")

        logger.info(f"Транскрипция завершена: {len(segments)} сегментов, "
                    f"{len(full_text)} символов, язык: {detected_lang}")

        transcript_result = {
            "segments": segments,
            "full_text": full_text,
            "language": detected_lang,
        }

        # Фикс скрипта: если Whisper выдал арабский/урду вместо деванагари
        transcript_result = _fix_transcript_script(transcript_result, whisper_lang or detected_lang)
        transcript_result = clean_transcript_noise(transcript_result, whisper_lang or detected_lang)

        return transcript_result

    except Exception as e:
        logger.error(f"Ошибка при транскрипции: {e}")
        raise
    finally:
        # Явно освобождаем модель из памяти (RAM/VRAM) после каждого вызова.
        # Без этого faster-whisper держит веса до следующего GC-цикла,
        # что при батче 17+ видео с per-clip Whisper приводит к OOM.
        try:
            del model
        except NameError:
            pass
        gc.collect()


_CHUNK_DURATION = 600  # 10 минут — размер одного чанка при разбивке длинных видео

def _token_ends_sentence(word: str) -> bool:
    """Токен Whisper заканчивается знаком конца фразы (ASCII/Unicode)."""
    t = (word or "").strip()
    while t and t[-1] in "\"'»”')]}":
        t = t[:-1].rstrip()
    if not t:
        return False
    return t[-1] in ".!?…।።॥।。．？！؟"


def _flatten_words(segments: list[dict]) -> list[dict]:
    """Плоский список слов с индексом сегмента (для границ предложений)."""
    out: list[dict] = []
    for si, seg in enumerate(segments):
        for w in seg.get("words") or []:
            out.append({
                "word": w.get("word", ""),
                "start": float(w["start"]),
                "end": float(w["end"]),
                "seg_idx": si,
            })
    return out


def _first_word_idx_at_or_after(words: list[dict], t: float, eps: float = 0.06) -> int | None:
    """Первый индекс слова, которое ещё «слышимо» после момента t (не обрезано до t)."""
    for i, w in enumerate(words):
        if w["end"] > t - eps:
            return i
    return None


def _sentence_start_time_for_word_idx(words: list[dict], idx: int) -> float:
    """Время начала предложения (клаузы), в котором находится words[idx]."""
    s = idx
    while s > 0 and words[s - 1]["seg_idx"] == words[s]["seg_idx"]:
        if _token_ends_sentence(words[s - 1]["word"]):
            break
        s -= 1
    return float(words[s]["start"])


def _sentence_end_word_idx(words: list[dict], idx: int) -> int:
    """Индекс последнего слова предложения, начинающегося в idx (в том же сегменте)."""
    e = idx
    seg = words[idx]["seg_idx"]
    while e < len(words) and words[e]["seg_idx"] == seg:
        if _token_ends_sentence(words[e]["word"]):
            return e
        e += 1
    return max(idx, e - 1)


def _first_sentence_start_at_or_after(words: list[dict], t: float) -> float | None:
    """Первое начало полного предложения в момент >= t (по таймкодам слов)."""
    i = _first_word_idx_at_or_after(words, t)
    if i is None:
        return None
    guard = 0
    while i < len(words) and guard < 800:
        guard += 1
        st = _sentence_start_time_for_word_idx(words, i)
        if st + 1e-3 >= t:
            return st
        e = _sentence_end_word_idx(words, i)
        i = e + 1
    return None


def _best_sentence_end_before(words: list[dict], hi: float, lo: float) -> float | None:
    """Максимальный words[].end с пунктуацией конца фразы, где lo <= end <= hi."""
    best: float | None = None
    for w in words:
        if w["start"] > hi:
            break
        if not _token_ends_sentence(w["word"]):
            continue
        we = float(w["end"])
        if we < lo or we > hi:
            continue
        if best is None or we > best:
            best = we
    return best


def _first_sentence_end_on_or_after(words: list[dict], t: float, hi: float) -> float | None:
    """Минимальный конец предложения (по слову с пунктуацией) с end >= t и end <= hi."""
    best: float | None = None
    for w in words:
        if w["start"] > hi:
            break
        if not _token_ends_sentence(w["word"]):
            continue
        we = float(w["end"])
        if we < t or we > hi:
            continue
        if best is None or we < best:
            best = we
    return best


def _last_word_end_before(words: list[dict], t: float) -> float | None:
    last: float | None = None
    for w in words:
        if w["end"] <= t:
            last = float(w["end"])
        elif w["start"] >= t:
            break
    return last


def _clip_text_for_range(segments: list[dict], start: float, end: float) -> str:
    """Текст всех сегментов, пересекающих [start, end]."""
    word_parts: list[str] = []
    has_words = False
    for seg in segments:
        words = seg.get("words") or []
        if not words:
            continue
        has_words = True
        for word in words:
            try:
                ws = float(word.get("start"))
                we = float(word.get("end"))
            except (TypeError, ValueError):
                continue
            if we > start and ws < end:
                txt = str(word.get("word") or "").strip()
                if txt:
                    word_parts.append(txt)
    if has_words and word_parts:
        return " ".join(word_parts).strip()

    parts = [
        seg["text"] for seg in segments
        if seg["end"] > start and seg["start"] < end
    ]
    return " ".join(parts).strip()


_GENERIC_CONTENT_WORDS = {
    "nanoplastic", "nanoplastics", "microplastic", "microplastics", "plastic",
    "plastics", "health", "environment", "video", "clip", "people", "water",
    "нанопластик", "нанопластика", "микропластик", "микропластика", "пластик",
}


def _content_tokens(text: str) -> set[str]:
    text = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    words = {
        token for token in re.findall(r"[\w]{4,}", text, flags=re.UNICODE)
        if token not in _GENERIC_CONTENT_WORDS and not token.isdigit()
    }
    if len(words) >= 6:
        return words

    compact = re.sub(r"\s+", "", text)
    if len(compact) < 12:
        return words
    return {compact[i:i + 4] for i in range(max(0, len(compact) - 3))}


def _clip_similarity(a: dict, b: dict) -> float:
    text_a = " ".join(str(a.get(k, "") or "") for k in ("text", "hook", "reason"))
    text_b = " ".join(str(b.get(k, "") or "") for k in ("text", "hook", "reason"))
    ta = _content_tokens(text_a)
    tb = _content_tokens(text_b)
    if ta and tb:
        inter = len(ta & tb)
        union = len(ta | tb)
        containment = inter / max(1, min(len(ta), len(tb)))
        jaccard = inter / max(1, union)
        return max(jaccard, containment * 0.82)

    norm_a = re.sub(r"\s+", " ", text_a.casefold()).strip()
    norm_b = re.sub(r"\s+", " ", text_b.casefold()).strip()
    if len(norm_a) < 40 or len(norm_b) < 40:
        return 0.0
    return SequenceMatcher(None, norm_a[:900], norm_b[:900]).ratio()


def _duration_signature(clip: dict) -> int:
    try:
        return round(float(clip["end_time"]) - float(clip["start_time"]))
    except Exception:
        return -1


def _duration_repeats_too_much(clip: dict, accepted: list[dict]) -> bool:
    sig = _duration_signature(clip)
    if sig < 0:
        return False
    return sum(1 for item in accepted if abs(_duration_signature(item) - sig) <= 1) >= 3


def _snap_clips_to_natural_boundaries(
    clips: list[dict],
    segments: list[dict],
    min_duration: float,
    max_duration: float,
) -> list[dict]:
    """
    Подгонка границ клипов к началу/концу предложений по word-level Whisper.

    Устраняет старт «с середины фразы» и обрыв на полуслове; учитывает порядок клипов
    (следующий клип не начинается раньше конца предыдущего — ищется следующее предложение).
    """
    if not clips or not segments:
        return clips

    words = _flatten_words(segments)
    if not words:
        # Fallback: только границы сегментов
        out = []
        for clip in sorted(clips, key=lambda c: float(c["start_time"])):
            s, e = float(clip["start_time"]), float(clip["end_time"])
            for seg in segments:
                if seg["start"] <= s < seg["end"]:
                    s = float(min(s, seg["start"]))
                    break
            dur = e - s
            if dur >= min_duration:
                out.append({
                    **clip,
                    "start_time": s,
                    "end_time": e,
                    "duration": dur,
                    "text": _clip_text_for_range(segments, s, e),
                })
        return out

    out: list[dict] = []
    prev_end = 0.0
    gap = 0.05

    for clip in sorted(clips, key=lambda c: float(c["start_time"])):
        raw_s = float(clip["start_time"])
        raw_e = float(clip["end_time"])
        lo_start = max(raw_s, prev_end + gap)

        s = _first_sentence_start_at_or_after(words, lo_start)
        if s is None:
            s = lo_start
        if s < lo_start - 1e-3:
            s = lo_start

        lo_end = s + min_duration
        hi_end = s + max_duration
        pref_e = min(max(raw_e, lo_end), hi_end)

        e_sent = _best_sentence_end_before(words, hi=min(pref_e, hi_end), lo=lo_end)
        if e_sent is not None and e_sent >= lo_end:
            e = e_sent
        else:
            e_try = _first_sentence_end_on_or_after(words, t=lo_end, hi=hi_end)
            if e_try is not None:
                e = e_try
            else:
                le = _last_word_end_before(words, hi_end)
                e = min(hi_end, max(lo_end, le if le is not None else pref_e))

        if e - s < min_duration:
            e2 = _first_sentence_end_on_or_after(words, t=s + min_duration - 1e-3, hi=hi_end)
            if e2 is not None:
                e = e2
        if e - s < min_duration:
            logger.warning(
                f"Границы по предложениям: клип {raw_s:.1f}–{raw_e:.1f}s остаётся коротким "
                f"({e - s:.1f}s < {min_duration}s), оставляю как есть по словам"
            )
            e = min(float(segments[-1]["end"]), s + max(min_duration, raw_e - raw_s))

        if e - s > max_duration * 1.05:
            e = s + max_duration
            e_adj = _best_sentence_end_before(words, hi=e, lo=s + min_duration * 0.5)
            if e_adj is not None:
                e = e_adj

        text = _clip_text_for_range(segments, s, e)
        out.append({
            **{k: v for k, v in clip.items() if k not in ("start_time", "end_time", "duration", "text")},
            "start_time": s,
            "end_time": e,
            "duration": e - s,
            "text": text,
        })
        prev_end = e

    return out


def _finalize_clip_timestamps(
    clips: list[dict],
    segments: list[dict],
    min_duration: float,
    max_duration: float,
    target_clips: int,
) -> list[dict]:
    """
    Обрезка по target, два цикла snap+dedupe (после dedupe границы снова выравниваем по фразам).
    """
    clips = clips[:target_clips]
    for _ in range(2):
        clips = _snap_clips_to_natural_boundaries(
            clips, segments, min_duration, max_duration
        )
        clips = _deduplicate_clips(clips, min_duration)
    if len(clips) > target_clips:
        clips = clips[:target_clips]
    logger.info(
        f"Границы клипов выровнены по предложениям (word timestamps): {len(clips)} клипов"
    )
    return clips


def select_clips(transcript: dict, max_clips: int = 10,
                 min_duration: float = 15, max_duration: float = 60,
                 config: Optional[dict] = None) -> list[dict]:
    # max_clips трактуется как минимум: автоматически увеличивается
    # чтобы покрыть всё видео (~1 клип на max_duration секунд)
    """
    LLM (GPT-4o-mini) делит транскрипт на клипы; границы выравниваются по словам/предложениям.

    Для длинных видео (> 15 мин) или больших запросов (> 20 клипов)
    автоматически разбивает транскрипт на 15-минутные чанки и запрашивает
    пропорциональное количество клипов из каждого, затем объединяет результат.

    Args:
        transcript: Результат transcribe().
        max_clips: Максимум клипов.
        min_duration: Минимальная длительность клипа (сек).
        max_duration: Максимальная длительность клипа (сек).
        context: Тематический контекст.
        config: Конфиг с промптом (опционально).

    Returns:
        Список dict с полями: start_time, end_time, reason, text.
    """
    segments = transcript.get("segments", [])
    video_duration = max((s["end"] for s in segments), default=0) if segments else 0

    # Реальный речевой спан: от первого до последнего сегмента.
    # Отличается от video_duration когда в начале/конце много тишины или музыки
    # (Whisper silence-маркеры уже отфильтрованы в transcribe(), но учитываем на всякий случай).
    content_start = segments[0]["start"] if segments else 0
    content_span = video_duration - content_start  # фактическая длина речи

    # Автомасштаб: не меньше min_clips, но и достаточно чтобы покрыть весь контент
    target_clips = max(max_clips, math.ceil(content_span / max_duration))
    if target_clips != max_clips:
        logger.info(
            f"Автомасштаб клипов: {max_clips} (минимум) → {target_clips} "
            f"(контент {content_span/60:.1f} мин / {max_duration}с)"
        )

    # Физический потолок: нельзя запросить больше клипов, чем вмещает реальный контент.
    # Считаем по content_span, а не по video_duration — иначе длинные интро/музыка
    # раздувают цифру: GPT просят 12 клипов из 50 сек речи → все проваливают min_duration.
    max_feasible = max(1, math.floor(content_span / min_duration))
    if target_clips > max_feasible:
        logger.warning(
            f"Запрошено {target_clips} клипов, но реальный контент {content_span:.0f}с "
            f"вмещает максимум {max_feasible} клипов по {min_duration}с — "
            f"уменьшаем запрос до {max_feasible}"
        )
        target_clips = max_feasible

    if video_duration > _CHUNK_DURATION:
        clips = _select_clips_chunked(
            transcript, target_clips, min_duration, max_duration, config
        )
    else:
        clips = _select_clips_single(
            transcript, target_clips, min_duration, max_duration, config
        )

    # Гарантия минимума: если LLM вернул мало клипов, добиваем по таймлайну.
    if len(clips) < target_clips:
        need = target_clips - len(clips)
        logger.warning(
            f"LLM вернул {len(clips)} клипов при цели {target_clips} — "
            f"добавляю fallback-клипы: +{need}"
        )
        clips = _fill_shortfall_clips(
            clips=clips,
            segments=segments,
            target_count=target_clips,
            min_duration=min_duration,
            max_duration=max_duration,
        )

    return _finalize_clip_timestamps(
        clips, segments, min_duration, max_duration, target_clips
    )


def _select_clips_chunked(transcript: dict, max_clips: int,
                          min_duration: float, max_duration: float,
                          config: Optional[dict]) -> list[dict]:
    """
    Разбивает транскрипт на 15-минутные чанки, запрашивает LLM отдельно
    для каждого, объединяет и дедуплицирует результат.
    """
    segments = transcript.get("segments", [])
    video_duration = max((s["end"] for s in segments), default=0)

    num_chunks = math.ceil(video_duration / _CHUNK_DURATION)
    clips_per_chunk = math.ceil(max_clips / num_chunks)
    # Не запрашивать больше клипов у чанка, чем в него физически влезет
    chunk_feasible = max(1, math.floor(_CHUNK_DURATION / min_duration))
    clips_per_chunk = min(clips_per_chunk, chunk_feasible)

    logger.info(
        f"Длинное видео {video_duration / 60:.0f} мин → "
        f"{num_chunks} чанков по {_CHUNK_DURATION // 60} мин, "
        f"{clips_per_chunk} клипов/чанк (итого до {max_clips})"
    )

    all_clips = []
    for i in range(num_chunks):
        chunk_start = i * _CHUNK_DURATION
        chunk_end = (i + 1) * _CHUNK_DURATION
        chunk_segs = [
            s for s in segments
            if s["end"] > chunk_start and s["start"] < chunk_end
        ]
        if not chunk_segs:
            continue

        chunk_transcript = {
            "full_text": " ".join(s["text"] for s in chunk_segs),
            "segments": chunk_segs,
            "language": transcript.get("language", "en"),
        }

        logger.info(
            f"  Чанк {i + 1}/{num_chunks}: "
            f"{chunk_start / 60:.0f}–{chunk_end / 60:.0f} мин "
            f"({len(chunk_segs)} сегментов)"
        )

        try:
            chunk_clips = _select_clips_single(
                chunk_transcript, clips_per_chunk, min_duration, max_duration, config
            )
            logger.info(f"    → {len(chunk_clips)} клипов выбрано")
            all_clips.extend(chunk_clips)
        except Exception as exc:
            logger.error(f"  Ошибка в чанке {i + 1}: {exc}")

    all_clips.sort(key=lambda c: c["start_time"])
    deduped = _deduplicate_clips(all_clips, min_duration)
    logger.info(
        f"Итого клипов из всех чанков: {len(deduped)} (запрошено: {max_clips})"
    )
    return deduped[:max_clips]


def _select_clips_single(transcript: dict, max_clips: int,
                         min_duration: float, max_duration: float,
                         config: Optional[dict]) -> list[dict]:
    """Один GPT-запрос для выбора клипов из (части) транскрипта."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY не установлен в переменных окружения")
    
    client = OpenAI(api_key=api_key)
    
    full_text = transcript.get("full_text", "")
    segments = transcript.get("segments", [])
    
    if not full_text:
        logger.warning("Транскрипт пустой, не могу выбрать клипы")
        return []
    
    # Промпт из конфига или дефолтный
    if config and "llm" in config:
        prompt_template = config["llm"].get("clip_selection_prompt", "")
    else:
        prompt_template = ""

    if not prompt_template:
        prompt_template = """You are a transcript segmentation engine for YouTube Shorts about nanoplastics.

TASK: DIVIDE the transcript below into exactly {max_clips} clips.
Work sequentially from the FIRST timestamp to the LAST. Cover the ENTIRE transcript.

RULES:
1. Start at the very first timestamp. Create clip 1, then clip 2, then clip 3... until you reach the last timestamp.
2. Clips must NOT overlap and must NOT leave large gaps (max 5 seconds gap between clips).
3. Each clip: {min_duration}–{max_duration} seconds. start_time MUST be the timestamp of the FIRST word of a complete sentence (from the [Xs - Ys]: line where that word begins — never mid-sentence). end_time MUST be after the last word of a closing sentence (. ? ! …) when possible.
4. If the transcript is too short for {max_clips} clips, return as many as possible.
5. This is a DIVISION task, not a selection task. Include ALL content, not just "viral" moments.
6. Each clip must focus on a different fact, example, or idea. Do not repeat the same claim with different timestamps.
7. Vary clip lengths naturally by sentence/topic boundaries. Do not return a batch of same-size clips unless the source forces it.

Return JSON:
{{
  "clips": [
    {{
      "start_time": 10.5,
      "end_time": 45.2,
      "hook": "One sentence hook",
      "reason": "Topic of this clip"
    }}
  ]
}}

Transcript ({first_ts}s – {last_ts}s):
{transcript_with_times}"""

    # Форматирование транскрипта с таймингами
    transcript_lines = []
    for seg in segments:
        transcript_lines.append(
            f"[{seg['start']:.1f}s - {seg['end']:.1f}s]: {seg['text']}"
        )
    transcript_with_times = "\n".join(transcript_lines)

    min_clips = max(1, max_clips - 2)
    first_ts = f"{segments[0]['start']:.1f}" if segments else "0"
    last_ts = f"{segments[-1]['end']:.1f}" if segments else "0"
    prompt = prompt_template.format(
        max_clips=max_clips,
        min_clips=min_clips,
        min_duration=min_duration,
        max_duration=max_duration,
        first_ts=first_ts,
        last_ts=last_ts,
        transcript_with_times=transcript_with_times
    )

    logger.info(f"Запрос к GPT-4o-mini: цель {max_clips} клипов, минимум {min_clips}")

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a content extraction engine for YouTube Shorts. "
                        "Cover the ENTIRE transcript from first to last timestamp with no large gaps. "
                        "Every clip MUST begin at the first word of a complete sentence or clause "
                        "(start_time on a line where a new thought begins, never mid-phrase or after a comma only). "
                        "Every clip MUST end after a full stop / question / exclamation when possible. "
                        "You NEVER cut a thought mid-sentence at start or end. "
                        "You NEVER use overlapping time ranges. "
                        "You avoid near-duplicate clips and vary durations naturally. "
                        "When asked for N clips, return N clips if the content allows it."
                    )
                },
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.35
        )
        
        content = response.choices[0].message.content
        result = json.loads(content)
        
        # Парсинг результата (может быть {"clips": [...]}, {"selections": [...]}, или просто [...])
        clips = []
        if isinstance(result, list):
            clips = result
        elif isinstance(result, dict):
            # Попробуем найти список в разных возможных ключах
            clips = result.get("clips") or result.get("selections") or result.get("moments") or result.get("fragments")
            if clips is None:
                # Если нет стандартных ключей, попробуем найти первый список в словаре
                for key, value in result.items():
                    if isinstance(value, list) and len(value) > 0:
                        clips = value
                        logger.info(f"Нашел список клипов в ключе '{key}'")
                        break
        
        if not isinstance(clips, list):
            logger.error(f"LLM вернул неожиданный формат: {type(clips)}, содержимое: {result}")
            return []
        
        # Валидация и добавление текста
        validated_clips = []
        for clip in clips:
            if not all(k in clip for k in ["start_time", "end_time"]):
                logger.warning(f"Пропущен клип с неполными данными: {clip}")
                continue
            
            start = float(clip["start_time"])
            end = float(clip["end_time"])
            duration = end - start

            # Мягкая валидация: допускаем +10% сверх max чтобы не обрывать законченные мысли
            hard_max = max_duration * 1.1
            if duration < min_duration:
                logger.warning(f"Клип {start:.1f}-{end:.1f}s ({duration:.1f}s) слишком короткий, пропускаем")
                continue
            if duration > hard_max:
                logger.warning(f"Клип {start:.1f}-{end:.1f}s ({duration:.1f}s) слишком длинный, обрезаем до {max_duration}s")
                end = start + max_duration
                duration = max_duration
            
            # Извлечь текст клипа из транскрипта (пересечение интервалов)
            clip_text = _clip_text_for_range(segments, start, end)
            
            validated_clips.append({
                "start_time": start,
                "end_time": end,
                "duration": duration,
                "reason": clip.get("reason", ""),
                "hook": clip.get("hook", ""),
                "text": clip_text.strip()
            })
        
        validated_clips.sort(key=lambda c: c["start_time"])
        deduped = _deduplicate_clips(validated_clips, min_duration)

        skipped = len(validated_clips) - len(deduped)
        if skipped:
            logger.info(f"Удалено {skipped} перекрывающихся клипов, осталось {len(deduped)}")

        logger.info(f"Итого клипов после фильтрации: {len(deduped)}")
        return deduped[:max_clips]
        
    except Exception as e:
        logger.error(f"Ошибка при запросе к LLM: {e}")
        raise


def _deduplicate_clips(clips: list[dict], min_duration: float) -> list[dict]:
    """
    Убрать перекрывающиеся клипы из отсортированного по start_time списка.

    - Перекрытие > 20% длительности нового клипа → пропустить.
    - Небольшое перекрытие → сдвинуть start_time нового клипа к концу предыдущего.
    """
    deduped: list[dict] = []
    for clip in clips:
        if not deduped:
            deduped.append(clip)
            continue

        last = deduped[-1]
        overlap = max(0.0, min(clip["end_time"], last["end_time"])
                          - max(clip["start_time"], last["start_time"]))
        clip_dur = clip["end_time"] - clip["start_time"]

        if clip_dur > 0 and overlap / clip_dur > 0.20:
            logger.warning(
                f"Клип {clip['start_time']:.1f}-{clip['end_time']:.1f}s "
                f"перекрывается с предыдущим ({overlap:.1f}s), пропускаем"
            )
            continue

        if overlap > 0:
            clip = dict(clip)
            clip["start_time"] = last["end_time"]
            clip["duration"] = clip["end_time"] - clip["start_time"]
            if clip["duration"] < min_duration:
                logger.warning(
                    f"После сдвига клип {clip['start_time']:.1f}s слишком короткий, пропускаем"
                )
                continue

        duplicate_of = None
        duplicate_score = 0.0
        for prev in deduped:
            sim = _clip_similarity(clip, prev)
            if sim > duplicate_score:
                duplicate_score = sim
                duplicate_of = prev
        if duplicate_of is not None and duplicate_score >= 0.78:
            logger.warning(
                f"Клип {clip['start_time']:.1f}-{clip['end_time']:.1f}s похож на уже выбранный "
                f"{duplicate_of['start_time']:.1f}-{duplicate_of['end_time']:.1f}s "
                f"(similarity {duplicate_score:.2f}), пропускаем"
            )
            continue

        if _duration_repeats_too_much(clip, deduped):
            logger.info(
                f"Клип {clip['start_time']:.1f}-{clip['end_time']:.1f}s имеет повторяющуюся длительность "
                f"{_duration_signature(clip)}s; оставляю, но LLM prompt теперь просит больше вариативности"
            )

        deduped.append(clip)

    return deduped


def _fill_shortfall_clips(
    clips: list[dict],
    segments: list[dict],
    target_count: int,
    min_duration: float,
    max_duration: float,
) -> list[dict]:
    """
    Добрать недостающие клипы равномерными окнами по таймлайну.
    Используется как fallback, когда LLM возвращает слишком мало фрагментов.
    """
    if not segments or len(clips) >= target_count:
        return clips

    # Быстрый индекс занятых отрезков
    busy = [(float(c["start_time"]), float(c["end_time"])) for c in clips]
    busy.sort(key=lambda x: x[0])

    first_ts = float(segments[0]["start"])
    last_ts = float(segments[-1]["end"])
    span = max(0.0, last_ts - first_ts)
    if span <= 0:
        return clips

    # Пытаемся уместить target_count окон, но в рамках ограничений длительности.
    window = span / max(1, target_count)
    window = max(min_duration, min(max_duration, window))
    step = max(min_duration, window)

    def _overlaps(a: float, b: float) -> bool:
        for s, e in busy:
            if min(b, e) - max(a, s) > 0.0:
                return True
        return False

    out = list(clips)
    cursor = first_ts
    duration_variants = (0.82, 1.0, 1.18, 0.92, 1.08)
    guard = 0
    while len(out) < target_count and cursor + min_duration <= last_ts and guard < 20000:
        guard += 1
        start = cursor
        variant = duration_variants[len(out) % len(duration_variants)]
        varied_window = max(min_duration, min(max_duration, window * variant))
        end = min(last_ts, start + varied_window)
        dur = end - start
        if dur < min_duration:
            cursor += min_duration
            continue
        if _overlaps(start, end):
            cursor += min_duration
            continue

        clip_text = _clip_text_for_range(segments, start, end)
        if not clip_text:
            cursor += min_duration
            continue

        out.append({
            "start_time": start,
            "end_time": end,
            "duration": dur,
            "reason": "fallback timeline split",
            "hook": "",
            "text": clip_text,
        })
        busy.append((start, end))
        busy.sort(key=lambda x: x[0])
        cursor = end

    out.sort(key=lambda c: c["start_time"])
    out = _deduplicate_clips(out, min_duration)
    return out


def _probe_duration_seconds(video_path: Path) -> float | None:
    """Best-effort media duration from ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        duration = (json.loads(result.stdout or "{}").get("format") or {}).get("duration")
        return float(duration) if duration is not None else None
    except Exception:
        return None


def _has_readable_video_stream(video_path: Path) -> bool:
    """Return False only when ffprobe confirms there is no usable video stream."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,codec_type",
                "-of", "json",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        streams = json.loads(result.stdout or "{}").get("streams") or []
        for stream in streams:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            if stream.get("codec_type") == "video" and width > 0 and height > 0:
                return True
        return False
    except Exception as exc:
        logger.warning(f"Не удалось проверить видеодорожку {video_path.name}: {exc}")
        return True


def validate_clips_for_source(
    video_path: Path,
    clips: list[dict],
    min_duration: float,
    buffer: float = 0.5,
    transcript: dict | None = None,
) -> list[dict]:
    """
    Drop or clamp clips that cannot exist in the source media.

    This catches Whisper/LLM/fallback timestamp drift before FFmpeg can create
    audio-only fragments at the end of a file.
    """
    source_duration = _probe_duration_seconds(video_path)
    if source_duration is None or not clips:
        return clips

    segments = (transcript or {}).get("segments", [])
    valid: list[dict] = []
    skipped = 0
    clamped = 0

    for idx, clip in enumerate(clips, 1):
        try:
            start = max(0.0, float(clip["start_time"]))
            end = float(clip["end_time"])
        except (KeyError, TypeError, ValueError):
            logger.warning(f"Клип {idx} пропущен: неполные таймкоды")
            skipped += 1
            continue

        if start >= source_duration:
            logger.warning(
                f"Клип {idx} пропущен до нарезки: старт {start:.1f}s "
                f"за пределами видео ({source_duration:.1f}s)"
            )
            skipped += 1
            continue

        if end > source_duration:
            logger.warning(
                f"Клип {idx} обрезан до конца источника: {end:.1f}s -> {source_duration:.1f}s"
            )
            end = source_duration
            clamped += 1

        actual_cut_duration = min(source_duration, end + buffer) - max(0.0, start - buffer)
        if end <= start or actual_cut_duration < min_duration:
            logger.warning(
                f"Клип {idx} пропущен до нарезки: доступно {actual_cut_duration:.1f}s "
                f"из нужных минимум {min_duration:.1f}s"
            )
            skipped += 1
            continue

        fixed = dict(clip)
        fixed["start_time"] = start
        fixed["end_time"] = end
        fixed["duration"] = end - start
        if segments:
            fixed["text"] = _clip_text_for_range(segments, start, end)
        valid.append(fixed)

    if skipped or clamped:
        logger.warning(
            f"Проверка границ видео: оставлено {len(valid)} из {len(clips)} клипов "
            f"(пропущено {skipped}, обрезано {clamped})"
        )

    return valid


def cut_clips(video_path: Path, clips: list[dict], output_dir: Path,
              buffer: float = 0.5,
              x264_preset: str = "veryslow",
              x264_crf: int = 15,
              lossless_intermediate: bool = False) -> list[Path]:
    """
    Нарезать видео на клипы по таймкодам (FFmpeg).

    Args:
        video_path: Путь к исходному видео.
        clips: Список клипов из select_clips().
        output_dir: Папка для сохранения клипов.
        buffer: Запас при нарезке (сек) для чистого среза на ключевом кадре.
        lossless_intermediate: если True — клипы как ``clip_XXX.mkv`` (FFV1), иначе mp4 (libx264).

    Returns:
        Список Path к нарезанным клипам.
    """
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    logger.info(f"Нарезка {video_path.name} на {len(clips)} клипов")
    
    # Длина списка всегда = len(clips): None = нарезка не удалась.
    # Иначе zip(paths, clips) смещает метаданные → субтитры не совпадают с аудио.
    output_paths: list[Path | None] = []
    source_duration = _probe_duration_seconds(video_path)

    for i, clip in enumerate(clips, 1):
        start = max(0, clip["start_time"] - buffer)
        end = clip["end_time"] + buffer
        if source_duration is not None:
            if start >= source_duration:
                logger.warning(
                    f"  [{i}/{len(clips)}] Клип пропущен: старт {start:.1f}s "
                    f"за пределами видео ({source_duration:.1f}s)"
                )
                output_paths.append(None)
                continue
            if end > source_duration:
                logger.warning(
                    f"  [{i}/{len(clips)}] Конец клипа {end:.1f}s за пределами видео "
                    f"({source_duration:.1f}s), обрезаю до конца источника"
                )
                end = source_duration
        duration = end - start
        if duration <= 0.25:
            logger.warning(f"  [{i}/{len(clips)}] Клип пропущен: слишком короткая длительность {duration:.2f}s")
            output_paths.append(None)
            continue

        ext = ".mkv" if lossless_intermediate else ".mp4"
        output_path = output_dir / f"clip_{i:03d}{ext}"

        logger.info(f"  [{i}/{len(clips)}] Нарезка клипа {start:.1f}s - {end:.1f}s "
                    f"({duration:.1f}s) -> {output_path.name}"
                    f"{' (FFV1 промежуточно)' if lossless_intermediate else ''}")

        vf_tail = (
            _cut_ffmpeg_tail_lossless()
            if lossless_intermediate
            else _cut_ffmpeg_tail(x264_preset, x264_crf)
        )
        cmd = [
            "ffmpeg",
            # Достраиваем PTS, если в контейнере кривые метки — меньше артефактов на старте клипа.
            "-fflags", "+genpts",
            "-ss", str(start),
            "-i", str(video_path),
            "-t", str(duration),
            # Перекодируем видео чтобы первый кадр был точно на start (не на keyframe).
            # Без этого fast-seek прыгает на ближайший I-frame до start,
            # и субтитры уезжают вперёд на 0.5–3 секунды.
            *vf_tail,
            "-y",
            str(output_path)
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
            if not _has_readable_video_stream(output_path):
                logger.warning(f"  [{i}/{len(clips)}] Клип пропущен: после нарезки нет видеодорожки ({output_path.name})")
                output_paths.append(None)
                continue
            output_paths.append(output_path)

        except subprocess.CalledProcessError as e:
            logger.error(f"Ошибка FFmpeg при нарезке клипа {i}: {e.stderr}")
            output_paths.append(None)

    ok = sum(1 for p in output_paths if p is not None)
    logger.info(f"Нарезано {ok} из {len(clips)} клипов")
    return output_paths
