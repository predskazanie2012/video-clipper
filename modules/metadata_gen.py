"""
Модуль 7: Генерация метаданных (заголовок, описание, хештеги) через LLM.
"""

import logging
import json
import os
import re
from pathlib import Path
from typing import Optional

from openai import OpenAI
from modules.manual_metadata import write_manual_upload_text, write_plain_upload_text

logger = logging.getLogger(__name__)


def title_key(title: str) -> str:
    """Stable key for detecting duplicate YouTube titles."""
    text = re.sub(r"\s+", " ", str(title or "").casefold()).strip()
    return "".join(ch for ch in text if ch.isalnum() or ch.isspace()).strip()


def _trim_title(title: str, max_len: int = 100) -> str:
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    if len(title) <= max_len:
        return title
    cut = title[:max_len].rstrip()
    for sep in (".", "!", "?", ",", ":", ";", " "):
        pos = cut.rfind(sep)
        if pos > 60:
            return cut[:pos].rstrip(" ,:;")
    return cut.rsplit(" ", 1)[0].strip() or cut


def ensure_unique_title(
    title: str,
    existing_titles: Optional[list[str]] = None,
    *,
    fallback_hint: str = "",
    max_len: int = 100,
) -> str:
    """Make title unique against already generated titles without changing language."""
    title = _trim_title(title, max_len=max_len)
    existing_keys = {title_key(t) for t in (existing_titles or []) if title_key(t)}
    if not title_key(title) or title_key(title) not in existing_keys:
        return title

    hint = _trim_title(fallback_hint, max_len=42)
    candidates: list[str] = []
    if hint and title_key(hint) != title_key(title):
        candidates.append(_trim_title(f"{title}: {hint}", max_len=max_len))
        candidates.append(_trim_title(f"{hint}: {title}", max_len=max_len))

    for idx in range(2, 100):
        suffix = f" ({idx})"
        candidates.append(_trim_title(title, max_len=max_len - len(suffix)) + suffix)

    for candidate in candidates:
        if title_key(candidate) and title_key(candidate) not in existing_keys:
            logger.info("Title made unique: %s -> %s", title, candidate)
            return candidate
    return title


def generate_metadata(transcript: str, language: str = "en",
                      config: Optional[dict] = None,
                      description_suffix: str = "",
                      existing_titles: Optional[list[str]] = None) -> dict:
    """
    Сгенерировать заголовок, описание и хештеги для YouTube Shorts.

    Args:
        transcript: Транскрипт клипа на целевом языке.
        language: Язык (напр. "en", "ru", "fr").
        config: Конфиг с промптом (опционально).

    Returns:
        dict с полями:
            - title: str (до 100 символов).
            - description: str (2-3 предложения + призыв к действию).
            - tags: list[str] (5-10 хештегов).
            - language: str.
            - category_id: str (YouTube category).
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY не установлен в переменных окружения")
    
    client = OpenAI(api_key=api_key)
    
    # Промпт из конфига или дефолтный
    if config and "llm" in config:
        prompt_template = config["llm"].get("metadata_prompt", "")
    else:
        prompt_template = ""

    if not prompt_template:
        prompt_template = """Write YouTube Shorts metadata in {lang} language.

The clip is about: nanoplastics / microplastics and their impact on human health and the environment.

Clip transcript:
{transcript}

Write:
1) TITLE — viral hook, strictly under 100 characters (YouTube hard limit). Must be a COMPLETE, meaningful phrase — never cut mid-word. Make it shocking, specific, or curiosity-driven. Good patterns: numbers ("5 Foods With.."), questions ("Did You Know...?"), shocking facts ("You Eat 5g of Plastic...").
2) DESCRIPTION — 4–6 full sentences, no length limit. Structure: shocking hook → key fact from clip → health/environmental implication → what viewers can do → call to action (follow for more). Write complete sentences, never cut off.
3) HASHTAGS — 8–12 tags relevant to the clip (mix popular + niche).

RULES:
- EVERYTHING must be in {lang} — title, description, hashtags
- Title MUST be under 100 characters AND make complete sense
- Description must have NO truncation — every sentence must end properly
- Do NOT add "..." at the end of title or description

Return JSON:
{{
  "title": "...",
  "description": "...",
  "tags": ["tag1", "tag2", ...]
}}"""

    # Языковые подсказки для лучшего результата
    lang_names = {
        "en": "English",
        "ru": "Russian (русский)",
        "fr": "French (français)",
        "de": "German (Deutsch)",
        "es": "Spanish (español)",
        "pt": "Portuguese (português)",
        "it": "Italian (italiano)",
        "zh": "Chinese (中文)",
        "ja": "Japanese (日本語)",
        "ko": "Korean (한국어)",
        "ar": "Arabic (العربية)",
        "hi": "Hindi (हिन्दी)",
        "tr": "Turkish (Türkçe)",
        "pl": "Polish (polski)",
        "nl": "Dutch (Nederlands)",
        "uk": "Ukrainian (українська)",
    }

    lang_display = lang_names.get(language, language)

    prompt = prompt_template.format(
        lang=lang_display,
        transcript=transcript[:2500]  # Увеличено с 1000 до 2500 символов
    )
    if existing_titles:
        used = [_trim_title(t, 100) for t in existing_titles if str(t or "").strip()]
        used = used[-60:]
        if used:
            prompt += (
                "\n\nAlready used titles in this language/project. "
                "Create a NEW, meaningfully different title and do not repeat these:\n- "
                + "\n- ".join(used)
            )
    
    logger.info(f"Генерация метаданных для клипа на языке: {language}")
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": f"Ты эксперт по созданию вирусного контента для YouTube Shorts. "
                               f"Пишешь на языке: {lang_display}."
                },
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.8  # Больше креативности для заголовков
        )
        
        content = response.choices[0].message.content
        result = json.loads(content)
        
        # Валидация и форматирование
        title = result.get("title", "").strip()
        if len(title) > 100:
            # Обрезать по последнему знаку препинания или пробелу, не добавляя "..."
            cut = title[:100]
            for sep in (".", "!", "?", ",", " "):
                pos = cut.rfind(sep)
                if pos > 60:  # не обрезать слишком короткий заголовок
                    title = cut[:pos].rstrip(" ,")
                    break
            else:
                title = cut.rsplit(" ", 1)[0]  # по последнему слову
            logger.warning(f"Заголовок обрезан до {len(title)} символов: {title}")
        title = ensure_unique_title(title, existing_titles, fallback_hint=transcript[:180])
        description = result.get("description", "").strip()
        if description_suffix and description_suffix.strip():
            description = description.rstrip() + "\n\n" + description_suffix.strip()
        tags = result.get("tags", [])

        # Убрать # из тегов если есть
        tags = [tag.strip().lstrip("#").strip() for tag in tags if tag]

        # Базовые теги на языке видео (только если английский — иначе не вставляем английские)
        if language == "en":
            base_tags = ["nanoplastics", "microplastics", "health", "environment"]
            for base_tag in base_tags:
                if base_tag not in [t.lower() for t in tags]:
                    tags.append(base_tag)

        # Ограничить количество тегов
        tags = tags[:15]
        
        # Определить категорию YouTube
        # 28 = Science & Technology, 29 = Nonprofits & Activism
        category_id = "28"  # По умолчанию Science & Technology
        
        metadata = {
            "title": title,
            "description": description,
            "tags": tags,
            "language": language,
            "category_id": category_id
        }
        
        logger.info(f"Метаданные сгенерированы:")
        logger.info(f"  Заголовок: {title}")
        logger.info(f"  Теги: {', '.join(tags[:5])}...")
        
        return metadata
        
    except Exception as e:
        logger.error(f"Ошибка при генерации метаданных: {e}")
        raise


def save_metadata(metadata: dict, output_path: Path) -> Path:
    """
    Сохранить метаданные в JSON-файл.

    Args:
        metadata: dict из generate_metadata().
        output_path: Путь для сохранения .json файла.

    Returns:
        Path к сохранённому файлу.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    stem = output_path.stem
    clip_stem = stem[:-5] if stem.endswith("_meta") else stem
    video_path = output_path.with_name(f"{clip_stem}.mp4")
    try:
        write_manual_upload_text(video_path, metadata, output_path.with_name(f"{clip_stem}_youtube.txt"))
        write_plain_upload_text(video_path, metadata, output_path.with_name(f"{clip_stem}.txt"))
    except Exception as exc:
        logger.warning("Manual upload text was not saved for %s: %s", output_path.name, exc)
    
    logger.info(f"Метаданные сохранены: {output_path}")
    return output_path
