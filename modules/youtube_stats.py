"""
Сбор статистики YouTube (Data API v3) для дашборда аналитики.
Каналы и токены — из config.yaml.
Видео: все шорты с канала — плейлист «Загрузки» + фильтр по длительности
(API не отдаёт флаг Short; считаем ролик шортом, если duration ≤ short_max_sec).
Заголовки из schedule.json подмешиваются, если есть.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from modules.uploader import authenticate_channel

logger = logging.getLogger(__name__)

VIDEO_BATCH = 50
TOP_GLOBAL_N = 50
# YouTube Shorts до ~3 мин; можно переопределить: config analytics.short_max_duration_sec
DEFAULT_SHORT_MAX_SEC = 180
YOUTUBE_QUOTA_MESSAGE = (
    "Квота YouTube Data API исчерпана. "
    "Аналитика обновится после дневного сброса квоты или при подключении другого API-проекта."
)


def is_quota_exceeded_error(error: Any) -> bool:
    """True when Google reports exhausted YouTube Data API quota."""
    text = str(error or "").lower()
    return (
        "quotaexceeded" in text
        or "youtube.quota" in text
        or "квота youtube data api" in text
        or ("exceeded" in text and "quota" in text)
    )


def format_youtube_api_error(error: Any) -> str:
    """Short UI-safe message for YouTube API errors."""
    if is_quota_exceeded_error(error):
        return YOUTUBE_QUOTA_MESSAGE

    text = str(error or "").strip()
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    if len(text) > 220:
        text = text[:217] + "..."
    return text or "Ошибка YouTube API"


def _int_stat(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _paths_for_channel(base_dir: Path, ch_name: str, ch_cfg: dict, account_groups: dict) -> tuple[Path, Path]:
    token_file = base_dir / ch_cfg.get("token_file", f"tokens/{ch_name}.json")
    ag_name = ch_cfg.get("google_account", "")
    ag = account_groups.get(ag_name, {})
    secret_path = base_dir / ag.get("client_secret", f"secrets/{ch_name}_secret.json")
    return secret_path, token_file


def _short_max_sec(config: dict) -> int:
    a = config.get("analytics") or {}
    v = a.get("short_max_duration_sec", DEFAULT_SHORT_MAX_SEC)
    try:
        n = int(v)
        return max(15, min(n, 600))
    except (TypeError, ValueError):
        return DEFAULT_SHORT_MAX_SEC


def parse_iso8601_duration_seconds(iso: str | None) -> int | None:
    """YouTube contentDetails.duration: PT1H2M3S → секунды."""
    if not iso or not isinstance(iso, str) or not iso.startswith("PT"):
        return None
    h = m = s = 0
    for num, unit in re.findall(r"(\d+)([HMS])", iso):
        n = int(num)
        if unit == "H":
            h = n
        elif unit == "M":
            m = n
        elif unit == "S":
            s = n
    return h * 3600 + m * 60 + s


def iter_uploads_video_ids(service, uploads_playlist_id: str) -> tuple[list[str], str | None]:
    """Все video_id из плейлиста загрузок (постранично)."""
    out: list[str] = []
    page_token = None
    try:
        while True:
            kwargs: dict[str, Any] = {
                "part": "contentDetails",
                "playlistId": uploads_playlist_id,
                "maxResults": 50,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            r = service.playlistItems().list(**kwargs).execute()
            for it in r.get("items") or []:
                cd = it.get("contentDetails") or {}
                vid = (cd.get("videoId") or "").strip()
                if vid:
                    out.append(vid)
            page_token = r.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:
        return out, format_youtube_api_error(exc)
    return out, None


def _title_map_for_channel(schedule_entries: list, channel_name: str) -> dict[str, str]:
    """Последний известный заголовок из расписания по video_id."""
    m: dict[str, str] = {}
    for e in schedule_entries:
        if e.get("channel") != channel_name:
            continue
        vid = (e.get("youtube_id") or "").strip()
        if not vid:
            continue
        t = (e.get("title") or "").strip()
        if t:
            m[vid] = t
    return m


def fetch_channel_header(service, channel_id: str) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """
    Статистика канала + id плейлиста «Загрузки» (один запрос channels.list).
    Возвращает (payload, error, uploads_playlist_id).
    payload — dict subscriber_count, view_count, video_count или None.
    """
    try:
        r = service.channels().list(part="statistics,contentDetails", id=channel_id).execute()
    except Exception as exc:
        err = format_youtube_api_error(exc)
        logger.warning("channels.list failed for %s: %s", channel_id, err)
        return None, err, None
    items = r.get("items") or []
    if not items:
        return None, "Канал не найден по channel_id", None
    s = items[0].get("statistics") or {}
    cd = items[0].get("contentDetails") or {}
    rel = cd.get("relatedPlaylists") or {}
    upl = (rel.get("uploads") or "").strip() or None
    payload = {
        "subscriber_count": _int_stat(s.get("subscriberCount")),
        "view_count": _int_stat(s.get("viewCount")) or 0,
        "video_count": _int_stat(s.get("videoCount")) or 0,
    }
    return payload, None, upl


def fetch_videos_statistics(
    service, video_ids: list[str], *, include_duration: bool = False
) -> dict[str, dict[str, Any]]:
    """По id возвращает views, likes, comments, title; опционально duration_sec."""
    result: dict[str, dict[str, Any]] = {}
    if not video_ids:
        return result
    part = "statistics,snippet,contentDetails" if include_duration else "statistics,snippet"
    for i in range(0, len(video_ids), VIDEO_BATCH):
        batch = video_ids[i : i + VIDEO_BATCH]
        try:
            r = service.videos().list(
                part=part,
                id=",".join(batch),
            ).execute()
        except Exception as exc:
            logger.warning("videos.list batch failed: %s", format_youtube_api_error(exc))
            if is_quota_exceeded_error(exc):
                break
            continue
        for item in r.get("items") or []:
            vid = item.get("id")
            if not vid:
                continue
            st = item.get("statistics") or {}
            sn = item.get("snippet") or {}
            row: dict[str, Any] = {
                "views": _int_stat(st.get("viewCount")) or 0,
                "likes": _int_stat(st.get("likeCount")) or 0,
                "comments": _int_stat(st.get("commentCount")) or 0,
                "title": (sn.get("title") or "").strip(),
            }
            if include_duration:
                cd = item.get("contentDetails") or {}
                dur_iso = cd.get("duration")
                row["duration_sec"] = parse_iso8601_duration_seconds(dur_iso)
            result[vid] = row
    return result


def collect_analytics_snapshot(
    base_dir: Path,
    config: dict,
    schedule_entries: list,
) -> dict[str, Any]:
    """
    Полный снимок: каналы из config.
    Видео: все ролики из плейлиста загрузок с длительностью ≤ short_max_sec (шорты).
    """
    from datetime import datetime, timezone

    channels_cfg = config.get("channels") or {}
    account_groups = config.get("account_groups") or {}
    collected_at = datetime.now(timezone.utc).isoformat()
    short_max = _short_max_sec(config)

    channel_rows: list[dict[str, Any]] = []
    all_video_rows: list[dict[str, Any]] = []
    quota_exhausted_projects: set[str] = set()

    for ch_name in sorted(channels_cfg.keys()):
        ch_cfg = channels_cfg[ch_name]
        row: dict[str, Any] = {
            "name": ch_name,
            "channel_id": ch_cfg.get("channel_id") or "",
            "language": ch_cfg.get("language") or "",
            "error": None,
            "subscribers": None,
            "channel_views": None,
            "video_count": None,
            "videos": [],
        }

        cid = (row["channel_id"] or "").strip()
        if not cid:
            row["error"] = "Не задан channel_id"
            channel_rows.append(row)
            continue

        secret_path, token_file = _paths_for_channel(base_dir, ch_name, ch_cfg, account_groups)
        if not token_file.exists():
            row["error"] = "Нет токена — авторизуйте канал"
            channel_rows.append(row)
            continue
        if not secret_path.exists():
            row["error"] = "Нет client_secret"
            channel_rows.append(row)
            continue

        project_key = str(secret_path.resolve())
        if project_key in quota_exhausted_projects:
            row["error"] = YOUTUBE_QUOTA_MESSAGE
            row["error_code"] = "quota_exceeded"
            channel_rows.append(row)
            continue

        try:
            service = authenticate_channel(secret_path, token_file)
        except Exception as exc:
            logger.warning("[%s] authenticate: %s", ch_name, exc)
            row["error"] = f"OAuth: {exc}"
            channel_rows.append(row)
            continue

        stats, err, upl_id = fetch_channel_header(service, cid)
        if err:
            row["error"] = err
            if is_quota_exceeded_error(err):
                row["error_code"] = "quota_exceeded"
                quota_exhausted_projects.add(project_key)
            channel_rows.append(row)
            continue

        row["subscribers"] = stats["subscriber_count"]
        row["channel_views"] = stats["view_count"]
        row["video_count"] = stats["video_count"]

        titles_from_schedule = _title_map_for_channel(schedule_entries, ch_name)
        if not upl_id:
            row["error"] = "Нет плейлиста загрузок"
            row["videos"] = []
            channel_rows.append(row)
            continue

        all_upload_ids, list_err = iter_uploads_video_ids(service, upl_id)
        if list_err:
            logger.warning("[%s] playlistItems: %s", ch_name, list_err)
            row["error"] = f"Плейлист загрузок: {list_err}"
            if is_quota_exceeded_error(list_err):
                row["error"] = list_err
                row["error_code"] = "quota_exceeded"
                quota_exhausted_projects.add(project_key)
            row["videos"] = []
            channel_rows.append(row)
            continue

        vstats = fetch_videos_statistics(service, all_upload_ids, include_duration=True)

        videos_out: list[dict[str, Any]] = []
        for vid in all_upload_ids:
            st = vstats.get(vid) or {}
            dur = st.get("duration_sec")
            if dur is None or dur > short_max:
                continue
            title = st.get("title") or titles_from_schedule.get(vid) or vid
            url = f"https://www.youtube.com/watch?v={vid}"
            vr = {
                "youtube_id": vid,
                "title": title,
                "views": st.get("views", 0),
                "likes": st.get("likes", 0),
                "comments": st.get("comments", 0),
                "duration_sec": dur,
                "url": url,
            }
            videos_out.append(vr)
            all_video_rows.append(
                {**vr, "channel": ch_name, "language": ch_cfg.get("language") or ""}
            )

        row["videos"] = videos_out
        row["shorts_in_snapshot"] = len(videos_out)
        row["comments_total"] = sum(int(v.get("comments") or 0) for v in videos_out)
        channel_rows.append(row)

    # Агрегаты только по каналам без ошибки и с числовыми данными
    tot_sub = 0
    tot_views = 0
    tot_videos = 0
    tot_comments = 0
    ok_count = 0
    for r in channel_rows:
        if r.get("error"):
            continue
        if r.get("subscribers") is not None:
            tot_sub += r["subscribers"]
        if r.get("channel_views") is not None:
            tot_views += r["channel_views"]
        if r.get("video_count") is not None:
            tot_videos += r["video_count"]
        tot_comments += int(r.get("comments_total") or 0)
        ok_count += 1

    all_video_rows.sort(key=lambda x: x.get("views", 0), reverse=True)
    top_global = all_video_rows[:TOP_GLOBAL_N]

    return {
        "collected_at": collected_at,
        "short_max_duration_sec": short_max,
        "aggregates": {
            "total_subscribers": tot_sub,
            "total_channel_views": tot_views,
            "total_videos_on_channels": tot_videos,
            "total_comments_on_shorts": tot_comments,
            "channels_ok_count": ok_count,
            "channels_total": len(channel_rows),
        },
        "channels": channel_rows,
        "top_videos_global": top_global,
    }


def snapshot_to_csv(snapshot: dict[str, Any]) -> str:
    """Плоский CSV для Excel."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "language_code",
            "channel",
            "youtube_id",
            "title",
            "views",
            "likes",
            "comments",
            "duration_sec",
            "url",
            "channel_subscribers",
            "channel_views",
            "channel_comments_total",
            "channel_video_count",
            "channel_error",
        ]
    )
    collected = snapshot.get("collected_at", "")
    for ch in snapshot.get("channels") or []:
        cname = ch.get("name", "")
        err = ch.get("error") or ""
        subs = ch.get("subscribers")
        cv = ch.get("channel_views")
        cc = ch.get("comments_total")
        if cc is None:
            cc = sum(int(v.get("comments") or 0) for v in ch.get("videos") or [])
        vc = ch.get("video_count")
        lang_c = ch.get("language") or ""
        for v in ch.get("videos") or []:
            w.writerow(
                [
                    lang_c,
                    cname,
                    v.get("youtube_id", ""),
                    v.get("title", ""),
                    v.get("views", 0),
                    v.get("likes", 0),
                    v.get("comments", 0),
                    v.get("duration_sec", ""),
                    v.get("url", ""),
                    subs if subs is not None else "",
                    cv if cv is not None else "",
                    cc if cc is not None else "",
                    vc if vc is not None else "",
                    err,
                ]
            )
        if not ch.get("videos") and (err or subs is not None):
            w.writerow(
                [
                    lang_c,
                    cname,
                    "",  # youtube_id
                    "",  # title
                    "",  # views
                    "",  # likes
                    "",  # comments
                    "",  # duration_sec
                    "",  # url
                    subs or "",
                    cv or "",
                    cc or "",
                    vc or "",
                    err,
                ]
            )
    w.writerow([])
    w.writerow(["snapshot_collected_at", collected])
    return buf.getvalue()


def snapshot_to_markdown_for_ai(snapshot: dict[str, Any]) -> str:
    """Текст для вставки в чат с ИИ."""
    lines: list[str] = []
    agg = snapshot.get("aggregates") or {}
    lines.append("# Снимок YouTube (Video Clipper)")
    lines.append(f"Дата снимка (UTC): {snapshot.get('collected_at', '—')}")
    sm = snapshot.get("short_max_duration_sec")
    if sm is not None:
        lines.append(
            f"Шорты: все ролики из плейлиста «Загрузки» с длительностью ≤ **{sm}** с "
            f"(эвристика; в API нет флага Short)."
        )
    lines.append("")
    lines.append("## Сводка по всем каналам")
    lines.append(f"- Подписчиков (сумма по каналам, не уникальные люди): **{agg.get('total_subscribers', 0)}**")
    lines.append(f"- Просмотров по каналам (сумма): **{agg.get('total_channel_views', 0)}**")
    lines.append(f"- Видео на каналах (сумма счётчиков): **{agg.get('total_videos_on_channels', 0)}**")
    lines.append(f"- Комментариев на шортах из загрузок (сумма): **{agg.get('total_comments_on_shorts', 0)}**")
    lines.append(
        f"- Каналов с успешным ответом: **{agg.get('channels_ok_count', 0)}** / {agg.get('channels_total', 0)}"
    )
    lines.append("")
    lines.append("## Каналы (таблица)")
    lines.append("| Канал | Подписчики | Просмотры канала | Комментарии на шортах | Видео на канале | Ошибка |")
    lines.append("|-------|------------|------------------|-----------------------|-----------------|--------|")
    for ch in snapshot.get("channels") or []:
        err = (ch.get("error") or "").replace("|", "/")
        comments_total = ch.get("comments_total")
        if comments_total is None:
            comments_total = sum(int(v.get("comments") or 0) for v in ch.get("videos") or [])
        lines.append(
            f"| {ch.get('name','')} | {ch.get('subscribers') if ch.get('subscribers') is not None else '—'} | "
            f"{ch.get('channel_views') if ch.get('channel_views') is not None else '—'} | "
            f"{comments_total} | "
            f"{ch.get('video_count') if ch.get('video_count') is not None else '—'} | {err or '—'} |"
        )
    lines.append("")
    lines.append("## Топ видео по всей сети (по просмотрам)")
    lines.append("| # | Language (config) | Channel id | Просмотры | Комментарии | Заголовок | URL |")
    lines.append("|---|-------------------|------------|-----------|-------------|-----------|-----|")
    for i, v in enumerate(snapshot.get("top_videos_global") or [], 1):
        title = (v.get("title") or "")[:80].replace("|", "/")
        lines.append(
            f"| {i} | {v.get('language','')} | {v.get('channel','')} | {v.get('views',0)} | {v.get('comments',0)} | {title} | {v.get('url','')} |"
        )
    lines.append("")
    lines.append("## По каналам: шорты из загрузок (по убыванию просмотров)")
    for ch in snapshot.get("channels") or []:
        if ch.get("error"):
            continue
        vids = sorted(ch.get("videos") or [], key=lambda x: x.get("views", 0), reverse=True)
        if not vids:
            continue
        lines.append(f"### {ch.get('name')}")
        for v in vids:
            t = (v.get("title") or "").replace("\n", " ")[:120]
            lines.append(f"- {v.get('views', 0)} просм., {v.get('comments', 0)} комм. — {t} — {v.get('url', '')}")
        lines.append("")
    return "\n".join(lines)
