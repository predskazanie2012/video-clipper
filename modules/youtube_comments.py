"""YouTube comments inbox, AI drafts, and moderated replies."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from openai import OpenAI


COMMENT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

LANGUAGE_RU = {
    "af": "африкаанс", "am": "амхарский", "ar": "арабский", "bn": "бенгальский",
    "bg": "болгарский", "cs": "чешский", "da": "датский", "de": "немецкий",
    "el": "греческий", "en": "английский", "es": "испанский", "fa": "персидский",
    "fi": "финский", "fil": "филиппинский", "fr": "французский", "gu": "гуджарати",
    "he": "иврит", "hi": "хинди", "hr": "хорватский", "hu": "венгерский",
    "id": "индонезийский", "it": "итальянский", "ja": "японский", "ko": "корейский",
    "ka": "грузинский", "kk": "казахский", "lt": "литовский", "ms": "малайский",
    "mr": "маратхи", "nl": "нидерландский",
    "no": "норвежский", "pa": "панджаби", "pl": "польский", "pt": "португальский",
    "ro": "румынский", "ru": "русский", "sk": "словацкий", "sl": "словенский",
    "sr": "сербский", "sv": "шведский", "sw": "суахили", "ta": "тамильский",
    "te": "телугу", "th": "тайский", "tr": "турецкий", "uk": "украинский",
    "uz": "узбекский", "vi": "вьетнамский", "yo": "йоруба",
    "yue": "кантонский", "zh": "китайский", "hy": "армянский",
}


def language_ru(code: str | None) -> str:
    code = (code or "").strip().lower()
    return LANGUAGE_RU.get(code, code.upper() if code else "не указан")


def _load_service(token_file: Path):
    if not token_file.exists():
        return None, "not_authenticated"
    creds = Credentials.from_authorized_user_file(str(token_file), COMMENT_SCOPES)
    if not creds.has_scopes(COMMENT_SCOPES):
        return None, "reauth_required"
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_file.write_text(creds.to_json(), encoding="utf-8")
    if not creds.valid:
        return None, "reauth_required"
    return build("youtube", "v3", credentials=creds, cache_discovery=False), None


def fetch_recent_comments(
    base_dir: Path,
    channel_name: str,
    channel_config: dict[str, Any],
    *,
    max_results: int = 25,
) -> dict[str, Any]:
    token_file = base_dir / channel_config.get("token_file", f"tokens/{channel_name}.json")
    channel_id = (channel_config.get("channel_id") or "").strip()
    if not channel_id:
        return {"ok": False, "error": "no_channel_id", "comments": []}

    service, err = _load_service(token_file)
    if err:
        return {"ok": False, "error": err, "comments": []}

    res = service.commentThreads().list(
        part="snippet,replies",
        allThreadsRelatedToChannelId=channel_id,
        maxResults=max(1, min(int(max_results or 25), 100)),
        order="time",
        textFormat="plainText",
    ).execute()

    comments = []
    for item in res.get("items") or []:
        sn = item.get("snippet") or {}
        top = (sn.get("topLevelComment") or {})
        top_sn = top.get("snippet") or {}
        replies = []
        for reply in ((item.get("replies") or {}).get("comments") or []):
            r_sn = reply.get("snippet") or {}
            author_channel = r_sn.get("authorChannelId") or {}
            author_channel_id = (
                author_channel.get("value")
                if isinstance(author_channel, dict)
                else str(author_channel or "")
            )
            replies.append({
                "id": reply.get("id") or "",
                "author": r_sn.get("authorDisplayName") or "",
                "author_channel_id": author_channel_id,
                "text": r_sn.get("textOriginal") or r_sn.get("textDisplay") or "",
                "published_at": r_sn.get("publishedAt") or "",
            })
        has_channel_reply = bool(
            channel_id
            and any(r.get("author_channel_id") == channel_id for r in replies)
        )
        comments.append({
            "thread_id": item.get("id") or "",
            "comment_id": top.get("id") or "",
            "video_id": sn.get("videoId") or "",
            "video_title": "",
            "author": top_sn.get("authorDisplayName") or "",
            "text": top_sn.get("textOriginal") or top_sn.get("textDisplay") or "",
            "published_at": top_sn.get("publishedAt") or "",
            "like_count": top_sn.get("likeCount") or 0,
            "reply_count": sn.get("totalReplyCount") or 0,
            "has_channel_reply": has_channel_reply,
            "can_reply": bool(sn.get("canReply", True)),
            "replies": replies,
            "url": f"https://www.youtube.com/watch?v={sn.get('videoId')}&lc={top.get('id')}",
        })
    return {"ok": True, "comments": comments}


def _json_from_model(content: str) -> dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start : end + 1])
        raise


def draft_comment_reply(
    *,
    comment_text: str,
    target_language: str,
    channel_name: str,
    video_title: str = "",
    russian_instruction: str = "",
    previous_reply: str = "",
) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY не задан")

    lang_name = language_ru(target_language)
    client = OpenAI(api_key=api_key)
    prompt = f"""
Комментарий зрителя:
{comment_text}

Канал: {channel_name}
Язык автора: {target_language} ({lang_name})
Название видео, если известно: {video_title or "не указано"}
Текущий черновик, если есть: {previous_reply or "нет"}
Правка владельца по-русски: {russian_instruction or "нет"}

Сделай:
1. Переведи комментарий на русский.
2. Подготовь ответ на языке автора.
3. Дай обратный перевод ответа на русский.

Стиль ответа: коротко и по делу. Обычно 1 предложение, максимум 2 коротких предложения и примерно до 180 символов. Не начинай с общих благодарностей и не используй пустые фразы вроде "спасибо за поддержку, мне приятно"; если комментарий просто emoji/поддержка, ответь очень коротко ("Danke 😊", "Thank you 😊", "Взаимно 😊" по смыслу). Если нет содержательной пользы от ответа, поставь should_reply=false. Если есть вопрос/скепсис, отвечай нейтрально, спокойно, на основе содержания видео или осторожных научных формулировок. Хейт — это только прямые оскорбления, травля, унижение или явно токсичный выпад; обычный скепсис/несогласие не считай хейтом. Для hate ставь should_reply=false. Эмоции сглаживать. Немного мягкого юмора можно, но без сарказма над человеком. Не выдумывай источники и не делай медицинских обещаний.

Верни JSON:
{{
  "comment_ru": "...",
  "reply_text": "...",
  "reply_ru": "...",
  "category": "question|support|skepticism|hate|spam|other",
  "should_reply": true,
  "risk_note": ""
}}
"""
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_COMMENTS_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": "Ты ассистент владельца YouTube-канала. Пиши только валидный JSON."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.45,
    )
    data = _json_from_model(response.choices[0].message.content or "{}")
    return {
        "comment_ru": str(data.get("comment_ru") or "").strip(),
        "reply_text": str(data.get("reply_text") or "").strip(),
        "reply_ru": str(data.get("reply_ru") or "").strip(),
        "category": str(data.get("category") or "other").strip(),
        "should_reply": bool(data.get("should_reply", True)),
        "risk_note": str(data.get("risk_note") or "").strip(),
    }


def translate_comments_ru(comments: list[dict[str, Any]]) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY не задан")

    payload = []
    for i, item in enumerate(comments[:40]):
        payload.append({
            "idx": i,
            "language": item.get("language") or "",
            "text": item.get("text") or "",
        })

    client = OpenAI(api_key=api_key)
    prompt = f"""
Переведи комментарии на русский, чтобы владелец канала быстро понял смысл.
Не отвечай на комментарии, только перевод и классификация.
Если комментарий состоит только из emoji, оставь emoji и коротко поясни эмоцию, если она понятна.
Классифицируй строго: hate — только прямые оскорбления, агрессия, травля, унижение человека/группы или явно токсичный выпад. Обычный скепсис, несогласие, тревога, критический вопрос или эмоциональная жалоба — это skepticism/other, не hate. Для hate ставь should_reply=false.

Верни JSON:
{{"items":[{{"idx":0,"comment_ru":"...","category":"question|support|skepticism|hate|spam|other","should_reply":true}}]}}

Комментарии:
{json.dumps(payload, ensure_ascii=False)}
"""
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_COMMENTS_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": "Ты переводчик комментариев YouTube. Пиши только валидный JSON."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    data = _json_from_model(response.choices[0].message.content or "{}")
    items = data.get("items") or []
    return {"items": items if isinstance(items, list) else []}


def send_comment_reply(
    base_dir: Path,
    channel_name: str,
    channel_config: dict[str, Any],
    *,
    parent_id: str,
    text: str,
) -> dict[str, Any]:
    token_file = base_dir / channel_config.get("token_file", f"tokens/{channel_name}.json")
    service, err = _load_service(token_file)
    if err:
        return {"ok": False, "error": err}
    res = service.comments().insert(
        part="snippet",
        body={"snippet": {"parentId": parent_id, "textOriginal": text}},
    ).execute()
    return {"ok": True, "id": res.get("id") or ""}
