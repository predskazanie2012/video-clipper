"""
Deterministic viral-potential score for generated Shorts metadata.

The score is intentionally heuristic: it helps rank clips for manual review
without calling an LLM or external analytics service.
"""

from __future__ import annotations

import re
from typing import Any


STRONG_HOOK_WORDS = {
    "shocking", "danger", "dangerous", "hidden", "secret", "warning", "risk",
    "toxic", "truth", "myth", "mistake", "avoid", "never", "stop", "silent",
    "шок", "опас", "скрыт", "секрет", "предупреж", "риск", "токс", "правд",
    "миф", "ошиб", "избег", "никогда", "останов",
    "peligro", "riesgo", "tox", "secreto", "verdad", "mito",
    "danger", "risque", "toxique", "secret", "verite", "mythe",
    "gefahr", "risiko", "gift", "geheim", "wahrheit", "mythos",
    "perigo", "risco", "toxico", "segredo", "verdade", "mito",
}

QUESTION_PATTERNS = (
    "why", "what", "how", "did you know", "can", "could",
    "почему", "что", "как", "знаете", "может", "правда",
    "por que", "porque", "que", "como", "sabia",
    "pourquoi", "comment", "saviez",
    "warum", "wie", "wussten",
    "なぜ", "どう", "知って", "왜", "어떻게", "알고",
    "为什么", "怎么", "你知道", "لماذا", "كيف", "هل",
)

TOPIC_WORDS = {
    "nanoplastic", "nanoplastics", "microplastic", "microplastics", "plastic",
    "bpa", "pfas", "pollution", "health", "brain", "blood", "heart", "lung",
    "fertility", "hormone", "cancer", "children", "baby", "food", "water",
    "bottle", "tea", "salt", "seafood", "air",
    "нанопласт", "микропласт", "пластик", "здоров", "мозг", "кров", "серд",
    "легк", "ферт", "гормон", "рак", "дет", "еда", "вода", "бутыл",
    "nanoplastique", "microplastique", "sante", "cerveau", "sang", "eau",
    "nanoplastico", "microplastico", "salud", "cerebro", "sangre", "agua",
    "nanoplastik", "mikroplastik", "gesundheit", "gehirn", "blut", "wasser",
    "nanoplastico", "microplastico", "saude", "cerebro", "sangue", "agua",
}

EVERYDAY_WORDS = {
    "drink", "eat", "food", "water", "bottle", "cup", "tea", "coffee", "salt",
    "kitchen", "home", "daily", "every day", "children", "baby",
    "пить", "есть", "еда", "вода", "бутыл", "чай", "кофе", "соль", "кух",
    "дом", "каждый день", "дет",
    "beber", "comer", "agua", "botella", "cafe", "sal", "casa",
    "boire", "manger", "eau", "bouteille", "cafe", "sel", "maison",
    "trinken", "essen", "wasser", "flasche", "kaffee", "salz", "haus",
}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(_as_text(v) for v in value)
    return str(value)


def _duration_value(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _has_any(text: str, needles: set[str] | tuple[str, ...]) -> bool:
    return any(n and n in text for n in needles)


def _count_any(text: str, needles: set[str]) -> int:
    return sum(1 for n in needles if n and n in text)


def _add(points: list[tuple[int, str]], value: int, reason: str) -> None:
    points.append((value, reason))


def _label(score: int) -> tuple[str, str]:
    if score >= 75:
        return "high", "сильный потенциал"
    if score >= 55:
        return "medium", "средний потенциал"
    return "low", "слабый потенциал"


def score_clip(
    *,
    clip_text: str = "",
    title: str = "",
    description: str = "",
    tags: list[str] | None = None,
    hook: str = "",
    reason: str = "",
    duration: float | int | str | None = None,
) -> dict[str, Any]:
    """Return a 0-100 viral-potential payload for one clip."""

    tags = tags or []
    title = _as_text(title).strip()
    hook = _as_text(hook).strip()
    clip_text = _as_text(clip_text).strip()
    description = _as_text(description).strip()
    combined = " ".join([title, hook, clip_text, description, _as_text(tags), _as_text(reason)]).lower()
    combined = re.sub(r"\s+", " ", combined)

    points: list[tuple[int, str]] = []
    base = 35

    title_len = len(title)
    if 35 <= title_len <= 90:
        _add(points, 6, "заголовок нормальной длины")
    elif 18 <= title_len < 35:
        _add(points, 3, "заголовок короткий, но рабочий")
    elif title_len < 18:
        _add(points, -8, "заголовок слишком короткий")
    elif title_len > 100:
        _add(points, -10, "заголовок длиннее лимита YouTube")

    hook_text = f"{title} {hook}".lower()
    if "?" in hook_text or _has_any(hook_text, QUESTION_PATTERNS):
        _add(points, 8, "есть вопрос/curiosity hook")
    if re.search(r"\d|%|№|#|\btop\b", hook_text):
        _add(points, 7, "есть цифра или конкретный факт")
    if _has_any(hook_text, STRONG_HOOK_WORDS):
        _add(points, 8, "сильный эмоциональный триггер")

    topic_hits = _count_any(combined, TOPIC_WORDS)
    if topic_hits >= 4:
        _add(points, 10, "много релевантных слов по теме")
    elif topic_hits >= 2:
        _add(points, 8, "тема считывается явно")
    elif topic_hits == 1:
        _add(points, 4, "есть тематический сигнал")
    else:
        _add(points, -6, "мало тематической конкретики")

    if _has_any(combined, EVERYDAY_WORDS):
        _add(points, 6, "есть связь с повседневной жизнью")

    text_len = len(clip_text)
    if 120 <= text_len <= 900:
        _add(points, 7, "достаточно содержательный фрагмент")
    elif 50 <= text_len < 120:
        _add(points, 4, "фрагмент короткий, но понятный")
    elif text_len and text_len < 50:
        _add(points, -7, "слишком мало текста в клипе")

    dur = _duration_value(duration)
    if dur is not None:
        if 18 <= dur <= 45:
            _add(points, 7, "длительность хорошая для Shorts")
        elif 12 <= dur < 18 or 45 < dur <= 60:
            _add(points, 3, "длительность допустимая")
        else:
            _add(points, -6, "длительность менее удачная для удержания")

    tag_count = len([t for t in tags if _as_text(t).strip()])
    if 6 <= tag_count <= 15:
        _add(points, 4, "достаточно тегов")
    elif 3 <= tag_count < 6:
        _add(points, 2, "теги есть, но их мало")

    if len(description) >= 120:
        _add(points, 3, "описание достаточно полное")
    elif description:
        _add(points, 2, "описание заполнено")

    if title.endswith("...") or description.endswith("..."):
        _add(points, -8, "есть обрезанный текст")

    if not hook and not re.search(r"\d|%|\?", title) and not _has_any(hook_text, STRONG_HOOK_WORDS):
        _add(points, -5, "hook выглядит слишком нейтральным")

    total = base + sum(value for value, _ in points)
    score = max(0, min(100, int(round(total))))
    label, label_ru = _label(score)

    positive = sorted((p for p in points if p[0] > 0), key=lambda p: p[0], reverse=True)
    negative = sorted((p for p in points if p[0] < 0), key=lambda p: p[0])
    reasons = [reason for _, reason in positive[:3]]
    if negative:
        reasons.append(negative[0][1])

    return {
        "score": score,
        "label": label,
        "label_ru": label_ru,
        "reasons": reasons[:4],
        "signals": {
            "title_length": title_len,
            "topic_hits": topic_hits,
            "tag_count": tag_count,
            "duration": dur,
        },
    }


def score_metadata(metadata: dict[str, Any] | None, clip_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = metadata or {}
    clip_meta = clip_meta or {}
    tags = metadata.get("tags")
    if not isinstance(tags, list):
        tags = []
    return score_clip(
        title=_as_text(metadata.get("title")),
        description=_as_text(metadata.get("description")),
        tags=[_as_text(t) for t in tags],
        clip_text=_as_text(clip_meta.get("text") or metadata.get("transcript")),
        hook=_as_text(clip_meta.get("hook")),
        reason=_as_text(clip_meta.get("reason")),
        duration=clip_meta.get("duration") or metadata.get("duration"),
    )


def attach_viral_score(metadata: dict[str, Any], clip_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata["viral_score"] = score_metadata(metadata, clip_meta)
    return metadata
