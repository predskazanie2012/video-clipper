"""Helpers for manual YouTube upload sidecar text files."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _clean_hashtag(tag: Any) -> str:
    text = str(tag or "").strip().lstrip("#").strip()
    if not text:
        return ""
    # YouTube hashtags cannot contain spaces. Keep unicode letters/numbers.
    compact = "".join(ch for ch in text if ch.isalnum() or ch == "_")
    return compact


def hashtags_from_tags(tags: Any, *, limit: int = 15) -> list[str]:
    if not isinstance(tags, list):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        clean = _clean_hashtag(tag)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(f"#{clean}")
        if len(out) >= limit:
            break
    return out


def hashtags_line(metadata: dict[str, Any]) -> str:
    return " ".join(hashtags_from_tags(metadata.get("tags") or []))


def youtube_description(metadata: dict[str, Any]) -> str:
    description = str(metadata.get("description") or "").strip()
    tags = hashtags_line(metadata)
    if description and tags:
        return f"{description}\n\n{tags}"
    return description or tags


def manual_upload_text(metadata: dict[str, Any], *, filename: str = "") -> str:
    title = str(metadata.get("title") or Path(filename).stem or "").strip()
    description = youtube_description(metadata)

    parts = [title]
    if description:
        parts.extend(["", description])
    return "\n".join(parts).rstrip() + "\n"


def sidecar_path_for(video_path: Path) -> Path:
    return video_path.with_name(f"{video_path.stem}_youtube.txt")


def plain_text_path_for(video_path: Path) -> Path:
    return video_path.with_suffix(".txt")


def write_manual_upload_text(
    video_path: Path,
    metadata: dict[str, Any],
    out_path: Path | None = None,
) -> Path:
    video_path = Path(video_path)
    target = Path(out_path) if out_path else sidecar_path_for(video_path)
    target.write_text(
        manual_upload_text(metadata, filename=video_path.name),
        encoding="utf-8",
    )
    return target


def write_plain_upload_text(
    video_path: Path,
    metadata: dict[str, Any],
    out_path: Path | None = None,
) -> Path:
    video_path = Path(video_path)
    target = Path(out_path) if out_path else plain_text_path_for(video_path)
    target.write_text(
        manual_upload_text(metadata, filename=video_path.name),
        encoding="utf-8",
    )
    return target
