"""
Модуль 5: Генерация и вшивание субтитров.

Whisper word-level timestamps -> ASS стилизация -> FFmpeg hardcoding.
"""

import logging
import subprocess
from pathlib import Path

import pysubs2

logger = logging.getLogger(__name__)

# Перед ass: сброс PTS (после нарезки/кропа метки «плывут» → залипание первого кадра в плеерах).
_VF_PTS_BEFORE_ASS = "setpts=PTS-STARTPTS"


# ─────────────────────────────────────────────────────────────────
# Пресеты субтитров
# alignment: 1-9 (ASS Numpad: 7=↖ 8=↑ 9=↗ / 4=← 5=● 6=→ / 1=↙ 2=↓ 3=↘)
# margin_v:  px от края (для 2=снизу; для 8=сверху; для 5=игнорируется)
# karaoke:   True → каждое слово подсвечивается accent_color по очереди
# ─────────────────────────────────────────────────────────────────
PRESETS: dict[str, dict] = {
    "classic": {
        "label":          "Classic",
        "font_name":      "Arial",
        "font_size":      56,
        "bold":           True,
        "primary":        "&H00FFFFFF",
        "outline_color":  "&H00000000",
        "back_color":     "&H00000000",
        "border_style":   1,
        "outline":        3.0,
        "shadow":         1.0,
        "alignment":      2,         # снизу по центру
        "margin_v":       120,
        "karaoke":        False,
        "words_per_line": 3,
        "preview_bg":     "#111",
        "preview_fg":     "#fff",
        "preview_stroke": "#000",
    },
    "beasty": {
        "label":          "Beasty",
        "font_name":      "Arial Black",
        "font_size":      74,
        "bold":           True,
        "primary":        "&H00FFFFFF",
        "outline_color":  "&H00000000",
        "back_color":     "&H00000000",
        "border_style":   1,
        "outline":        6.0,
        "shadow":         0.0,
        "alignment":      2,
        "margin_v":       340,       # выше — MrBeast-стиль
        "karaoke":        False,
        "words_per_line": 2,
        "preview_bg":     "#111",
        "preview_fg":     "#fff",
        "preview_stroke": "#000",
    },
    "mozi": {
        "label":          "Mozi",
        "font_name":      "Arial Black",
        # Было 240 — на вертикали 1080×1920 буквы занимали пол-кадра. ASS fontsize
        # в масштабе PlayResY=1920: ~72–84 даёт читаемый Shorts-стиль без «плаката».
        "font_size":      78,
        "bold":           True,
        "primary":        "&H00FFFFFF",   # белый базовый
        "accent_color":   "&H0000FF00",   # BGR зелёный = #00FF00
        "outline_color":  "&H00000000",
        "back_color":     "&H00000000",
        "border_style":   1,
        "outline":        5.0,
        "shadow":         0.0,
        "alignment":      2,
        "margin_v":       300,
        "karaoke":        True,
        "words_per_line": 2,
        "lines_per_block": 2,
        "preview_bg":     "#111",
        "preview_fg":     "#fff",
        "preview_stroke": "#000",
        "preview_accent": "#00ff00",
    },
    "neon": {
        "label":          "Neon",
        "font_name":      "Arial",
        "font_size":      58,
        "bold":           True,
        "primary":        "&H00FFFFFF",
        "accent_color":   "&H0000FFFF",   # BGR жёлтый = #FFFF00
        "outline_color":  "&H00000000",
        "back_color":     "&H00000000",
        "border_style":   1,
        "outline":        3.0,
        "shadow":         2.0,
        "alignment":      2,
        "margin_v":       120,
        "karaoke":        True,
        "words_per_line": 3,
        "preview_bg":     "#111",
        "preview_fg":     "#ffff00",
        "preview_stroke": "#000",
        "preview_accent": "#ffff00",
    },
    "fire": {
        "label":          "Fire",
        "font_name":      "Arial",
        "font_size":      58,
        "bold":           True,
        "primary":        "&H00FFFFFF",
        "accent_color":   "&H000069FF",   # BGR оранжевый = #FF6900
        "outline_color":  "&H000000AA",
        "back_color":     "&H00000000",
        "border_style":   1,
        "outline":        4.0,
        "shadow":         2.0,
        "alignment":      2,
        "margin_v":       120,
        "karaoke":        True,
        "words_per_line": 3,
        "preview_bg":     "#111",
        "preview_fg":     "#ff6900",
        "preview_stroke": "#aa0000",
        "preview_accent": "#ff6900",
    },
    "boxed": {
        "label":          "Boxed",
        "font_name":      "Arial",
        "font_size":      54,
        "bold":           True,
        "primary":        "&H00FFFFFF",
        "outline_color":  "&H00000000",
        "back_color":     "&HAA000000",   # полупрозрачный чёрный фон
        "border_style":   3,             # opaque box
        "outline":        6.0,
        "shadow":         0.0,
        "alignment":      2,
        "margin_v":       200,
        "karaoke":        False,
        "words_per_line": 4,
        "preview_bg":     "#333",
        "preview_fg":     "#fff",
        "preview_stroke": "transparent",
    },
    "shadow": {
        "label":          "Shadow",
        "font_name":      "Arial",
        "font_size":      56,
        "bold":           True,
        "primary":        "&H00FFFFFF",
        "outline_color":  "&H00FFFFFF",
        "back_color":     "&H90000000",
        "border_style":   1,
        "outline":        0.0,
        "shadow":         6.0,
        "alignment":      2,
        "margin_v":       120,
        "karaoke":        False,
        "words_per_line": 3,
        "preview_bg":     "#111",
        "preview_fg":     "#fff",
        "preview_stroke": "transparent",
    },
    "pink": {
        "label":          "Pink",
        "font_name":      "Arial",
        "font_size":      58,
        "bold":           True,
        "primary":        "&H00FFFFFF",
        "accent_color":   "&H00C800FF",   # BGR розовый = #FF00C8
        "outline_color":  "&H00C800FF",
        "back_color":     "&H00000000",
        "border_style":   1,
        "outline":        3.0,
        "shadow":         1.0,
        "alignment":      2,
        "margin_v":       120,
        "karaoke":        True,
        "words_per_line": 3,
        "preview_bg":     "#111",
        "preview_fg":     "#ff00c8",
        "preview_stroke": "transparent",
        "preview_accent": "#ff00c8",
    },
    "minimal": {
        "label":          "Minimal",
        "font_name":      "Arial",
        "font_size":      42,
        "bold":           False,
        "primary":        "&H00FFFFFF",
        "outline_color":  "&H00000000",
        "back_color":     "&H00000000",
        "border_style":   1,
        "outline":        1.5,
        "shadow":         0.0,
        "alignment":      8,         # сверху по центру
        "margin_v":       200,
        "karaoke":        False,
        "words_per_line": 5,
        "preview_bg":     "#111",
        "preview_fg":     "#ccc",
        "preview_stroke": "#000",
    },
}

DEFAULT_PRESET = "mozi"

# ─────────────────────────────────────────────────────────────────
# Стили заголовка-хука (показывается первые N секунд сверху)
# BorderStyle=3 → непрозрачный прямоугольник под текстом.
# Все цвета в формате ASS: &HAABBGGRR
# ─────────────────────────────────────────────────────────────────
HOOK_TITLE_STYLES: dict[str, dict] = {
    "card": {
        "font_name":    "Arial Black",
        "font_size":    50,
        "bold":         True,
        "primary":      "&H00FFFFFF",   # белый текст
        "outline_color":"&H00111111",   # почти чёрная рамка (padding box)
        "back_color":   "&H99000000",   # 40% прозрачность чёрная подложка
        "border_style": 3,
        "outline":      14.0,           # отступ внутри box
        "shadow":       0.0,
        "alignment":    8,              # сверху по центру
        "margin_v":     70,
        "margin_l":     70,
        "margin_r":     70,
    },
    "neon": {
        "font_name":    "Arial Black",
        "font_size":    50,
        "bold":         True,
        "primary":      "&H0000FFFF",   # жёлтый текст (BGR)
        "outline_color":"&H00111111",
        "back_color":   "&HAA000000",   # 33% прозрачность
        "border_style": 3,
        "outline":      14.0,
        "shadow":       0.0,
        "alignment":    8,
        "margin_v":     70,
        "margin_l":     70,
        "margin_r":     70,
    },
    "fire": {
        "font_name":    "Arial Black",
        "font_size":    50,
        "bold":         True,
        "primary":      "&H000069FF",   # оранжевый текст (BGR)
        "outline_color":"&H00000000",
        "back_color":   "&HBB000000",
        "border_style": 3,
        "outline":      14.0,
        "shadow":       0.0,
        "alignment":    8,
        "margin_v":     70,
        "margin_l":     70,
        "margin_r":     70,
    },
    "clean": {
        "font_name":    "Arial Black",
        "font_size":    54,
        "bold":         True,
        "primary":      "&H00FFFFFF",   # белый текст
        "outline_color":"&H00000000",   # чёрный контур
        "back_color":   "&H00000000",
        "border_style": 1,              # только контур, без box
        "outline":      6.0,
        "shadow":       4.0,
        "alignment":    8,
        "margin_v":     80,
        "margin_l":     60,
        "margin_r":     60,
    },
    "pink": {
        "font_name":    "Arial Black",
        "font_size":    50,
        "bold":         True,
        "primary":      "&H00FFFFFF",
        "outline_color":"&H00111111",
        "back_color":   "&HAA3300CC",   # тёмно-розовая подложка (BGR)
        "border_style": 3,
        "outline":      14.0,
        "shadow":       0.0,
        "alignment":    8,
        "margin_v":     70,
        "margin_l":     70,
        "margin_r":     70,
    },
}

DEFAULT_HOOK_STYLE = "card"

# Маппинг строковой позиции → (alignment, margin_v)
POSITION_MAP = {
    "top":     (8, 100),
    "top-mid": (8, 380),
    "center":  (5, 0),
    "bot-mid": (2, 380),
    "bottom":  (2, 100),
}


def _parse_color(color_str: str) -> pysubs2.Color:
    """&HAABBGGRR → pysubs2.Color(r, g, b, a)"""
    hex_str = color_str.replace("&H", "").replace("&h", "")
    hex_str = hex_str.zfill(8)
    a = int(hex_str[0:2], 16)
    b = int(hex_str[2:4], 16)
    g = int(hex_str[4:6], 16)
    r = int(hex_str[6:8], 16)
    return pysubs2.Color(r, g, b, a)


def _bbggrr(ass_color: str) -> str:
    """&H00BBGGRR → 'BBGGRR' для ASS inline тегов \\c&H{BBGGRR}&"""
    return ass_color.replace("&H", "").replace("&h", "").zfill(8)[2:]


def generate_ass(words: list[dict], output_path: Path,
                 preset: str = DEFAULT_PRESET,
                 position: str | None = None,
                 font_size_override: int | None = None,
                 # Устаревший API (совместимость)
                 font_name: str | None = None,
                 primary_color: str | None = None,
                 outline_color: str | None = None,
                 outline_width: int | None = None,
                 alignment_override: int | None = None,
                 margin_v_override: int | None = None) -> Path:
    """
    Создать файл субтитров ASS по выбранному пресету.

    Args:
        words:              Список [{word, start, end}], времена в секундах.
        output_path:        Путь для сохранения .ass файла.
        preset:             Название пресета из PRESETS.
        position:           Переопределить позицию: "top"/"top-mid"/"center"/"bot-mid"/"bottom".
        font_size_override: Переопределить размер шрифта (px).
        alignment_override: Прямое число 1-9 (приоритет над position).
        margin_v_override:  Прямой отступ px (вместе с alignment_override).
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    p = dict(PRESETS.get(preset, PRESETS[DEFAULT_PRESET]))

    # Устаревший API
    if font_name    is not None: p["font_name"]     = font_name
    if primary_color is not None: p["primary"]      = primary_color
    if outline_color is not None: p["outline_color"] = outline_color
    if outline_width is not None: p["outline"]       = float(outline_width)

    # Позиция: alignment_override > position (строка) > пресет
    if alignment_override is not None:
        final_align   = alignment_override
        final_marginv = margin_v_override if margin_v_override is not None else p.get("margin_v", 120)
    elif position and position in POSITION_MAP:
        final_align, final_marginv = POSITION_MAP[position]
    else:
        final_align   = p.get("alignment", 2)
        final_marginv = p.get("margin_v", 120)

    # Размер шрифта
    final_size = font_size_override if font_size_override is not None else p["font_size"]

    # ── Построить ASS файл ─────────────────────────────────────────
    subs = pysubs2.SSAFile()
    subs.info["PlayResX"] = "1080"
    subs.info["PlayResY"] = "1920"

    style = subs.styles["Default"]
    style.fontname     = p["font_name"]
    style.fontsize     = final_size
    style.bold         = p["bold"]
    style.primarycolor = _parse_color(p["primary"])
    style.outlinecolor = _parse_color(p["outline_color"])
    style.backcolor    = _parse_color(p["back_color"])
    style.borderstyle  = p["border_style"]
    style.outline      = p["outline"]
    style.shadow       = p["shadow"]
    style.alignment    = final_align
    style.marginv      = final_marginv
    style.marginl      = 40
    style.marginr      = 40

    # Фильтрация
    valid_words = [
        w for w in words
        if "start" in w and "end" in w and str(w.get("word", "")).strip()
    ]

    if not valid_words:
        subs.save(str(output_path))
        logger.warning("Нет слов для субтитров — сохранён пустой ASS")
        return output_path

    wpl        = p.get("words_per_line", 3)
    lpb        = p.get("lines_per_block", 1)
    block_size = wpl * lpb

    def _build_text_lines(chunk, wpl, lpb, highlight_idx=None, acc=None, pri=None):
        """Собрать текст блока с переносами строк \\N через каждые wpl слов."""
        lines = []
        for line_idx in range(lpb):
            start = line_idx * wpl
            line_words = chunk[start:start + wpl]
            if not line_words:
                break
            parts = []
            for k_rel, w in enumerate(line_words):
                k_abs = start + k_rel
                wtext = str(w["word"]).strip()
                if highlight_idx is not None and k_abs == highlight_idx:
                    parts.append(f"{{\\c&H{acc}&}}{wtext}{{\\c&H{pri}&}}")
                else:
                    parts.append(wtext)
            lines.append(" ".join(parts))
        return r"\N".join(lines)

    if p.get("karaoke") and p.get("accent_color"):
        # ── Karaoke режим: каждое слово подсвечивается по очереди ──
        acc   = _bbggrr(p["accent_color"])   # e.g. "00FF00"
        pri   = _bbggrr(p["primary"])        # e.g. "FFFFFF"

        for i in range(0, len(valid_words), block_size):
            chunk = valid_words[i:i + block_size]
            chunk_end = int(chunk[-1]["end"] * 1000) + 200
            if i + block_size < len(valid_words):
                chunk_end = min(chunk_end, int(valid_words[i + block_size]["start"] * 1000))

            for j, word in enumerate(chunk):
                w_start = int(word["start"] * 1000)
                if j + 1 < len(chunk):
                    w_end = int(chunk[j + 1]["start"] * 1000)
                else:
                    w_end = chunk_end

                text = _build_text_lines(chunk, wpl, lpb, highlight_idx=j, acc=acc, pri=pri)
                subs.append(pysubs2.SSAEvent(start=w_start, end=w_end, text=text))
    else:
        # ── Обычный режим: группами по block_size слов ──
        for i in range(0, len(valid_words), block_size):
            chunk = valid_words[i:i + block_size]
            start_ms = int(chunk[0]["start"] * 1000)
            end_ms   = int(chunk[-1]["end"] * 1000) + 200
            if i + block_size < len(valid_words):
                end_ms = min(end_ms, int(valid_words[i + block_size]["start"] * 1000))
            text = _build_text_lines(chunk, wpl, lpb)
            subs.append(pysubs2.SSAEvent(start=start_ms, end=end_ms, text=text))

    subs.save(str(output_path))
    logger.info(f"ASS субтитры [{preset}] сохранены: {output_path} ({len(subs)} строк)")
    return output_path


def burn_subtitles(video_path: Path, ass_path: Path,
                   output_path: Path,
                   x264_preset: str = "veryslow",
                   x264_crf: int = 14,
                   x264_tune: str | None = None) -> Path:
    """
    Вшить субтитры в видео через FFmpeg.
    Запускает FFmpeg из папки с .ass файлом — обход проблем с Windows-путями.
    x264_tune: например \"film\" — чуть лучше на естественном видео при том же CRF.
    """
    video_path  = Path(video_path).resolve()
    ass_path    = Path(ass_path).resolve()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not video_path.exists():
        raise FileNotFoundError(f"Видео не найдено: {video_path}")
    if not ass_path.exists():
        raise FileNotFoundError(f"ASS файл не найден: {ass_path}")

    # Порядок важен: сначала сброс меток, потом ass (libass привязывается к PTS кадра).
    vf = f"{_VF_PTS_BEFORE_ASS},ass={ass_path.name}"
    tune = (x264_tune or "").strip()
    vcodec: list[str] = [
        "-c:v", "libx264",
        "-preset", (x264_preset or "veryslow").strip(),
        "-crf", str(int(x264_crf)),
    ]
    if tune:
        vcodec.extend(["-tune", tune])
    vcodec.extend(["-pix_fmt", "yuv420p"])
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-i", str(video_path),
        "-vf", vf,
        *vcodec,
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-y",
        str(output_path),
    ]

    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ass_path.parent)
        )
        logger.info(f"Субтитры вшиты: {output_path.name}")
        return output_path
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка FFmpeg при вшивании субтитров: {e.stderr[-500:]}")
        raise RuntimeError(f"Не удалось вшить субтитры: {e.stderr[-300:]}")


def add_hook_title(ass_path: Path, title_text: str,
                   duration_sec: float = 3.5,
                   style: str = DEFAULT_HOOK_STYLE) -> Path:
    """
    Добавить заголовок-хук в начало существующего ASS файла.

    Создаёт отдельный стиль «HookTitle» и вставляет событие на первые
    duration_sec секунд с плавным появлением/исчезновением (fade 350ms).

    Args:
        ass_path:     Путь к уже существующему .ass файлу субтитров.
        title_text:   Текст заголовка (1–2 предложения).
        duration_sec: Длительность показа (по умолчанию 3.5 сек).
        style:        Ключ из HOOK_TITLE_STYLES.
    """
    ass_path = Path(ass_path)
    if not ass_path.exists():
        logger.warning(f"[Hook Title] ASS не найден: {ass_path}")
        return ass_path
    title_text = (title_text or "").strip()
    if not title_text:
        return ass_path

    s = dict(HOOK_TITLE_STYLES.get(style, HOOK_TITLE_STYLES[DEFAULT_HOOK_STYLE]))

    subs = pysubs2.SSAFile.load(str(ass_path))

    hook_style = pysubs2.SSAStyle()
    hook_style.fontname    = s["font_name"]
    hook_style.fontsize    = s["font_size"]
    hook_style.bold        = s["bold"]
    hook_style.primarycolor = _parse_color(s["primary"])
    hook_style.outlinecolor = _parse_color(s["outline_color"])
    hook_style.backcolor    = _parse_color(s["back_color"])
    hook_style.borderstyle  = s["border_style"]
    hook_style.outline      = s["outline"]
    hook_style.shadow       = s["shadow"]
    hook_style.alignment    = s["alignment"]
    hook_style.marginv      = s["margin_v"]
    hook_style.marginl      = s["margin_l"]
    hook_style.marginr      = s["margin_r"]

    subs.styles["HookTitle"] = hook_style

    end_ms = int(duration_sec * 1000)
    # \fad(fade_in_ms, fade_out_ms) — плавное появление и исчезновение
    text_with_fade = r"{\fad(350,350)}" + title_text

    event = pysubs2.SSAEvent(start=0, end=end_ms,
                             style="HookTitle", text=text_with_fade)
    subs.events.insert(0, event)

    subs.save(str(ass_path))
    logger.info(f"[Hook Title] «{title_text[:60]}» добавлен ({duration_sec}s, стиль={style})")
    return ass_path
