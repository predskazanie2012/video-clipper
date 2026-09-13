"""Guardrails that keep clips from being uploaded to the wrong language channel."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modules.planning import normalize_language


LANGUAGE_ALIASES = {
    "iw": "he",
    "nb": "no",
    "nn": "no",
    "tl": "fil",
    "cmn": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-sg": "zh",
    "zh-tw": "zh",
    "zh-hant": "zh",
    "pt-br": "pt",
    "pt-pt": "pt",
}


class LanguageChannelMismatch(ValueError):
    """Raised when clip metadata language does not match the target channel."""


def canonical_language(value: Any, channel_name: str = "") -> str:
    lang = normalize_language(value, channel_name)
    if not lang:
        return ""
    return LANGUAGE_ALIASES.get(lang, lang.split("-", 1)[0])


def metadata_language(metadata: dict | None) -> str:
    if not isinstance(metadata, dict):
        return ""
    return canonical_language(metadata.get("language") or metadata.get("lang") or "")


def channel_language(channel_config: dict | None, channel_name: str) -> str:
    cfg = channel_config or {}
    return canonical_language(cfg.get("language", ""), channel_name)


def assert_clip_language_matches_channel(
    metadata: dict | None,
    channel_name: str,
    channel_config: dict | None,
    video_name: str | Path = "clip",
) -> None:
    """Block upload/queueing when clip metadata points to another language."""
    clip_lang = metadata_language(metadata)
    expected_lang = channel_language(channel_config, channel_name)

    if not clip_lang or not expected_lang or clip_lang == expected_lang:
        return

    name = Path(video_name).name
    raise LanguageChannelMismatch(
        f"Язык клипа «{name}» = {clip_lang}, а канал «{channel_name}» ожидает {expected_lang}. "
        "Загрузка остановлена: переложите клип в очередь правильного языкового канала."
    )
