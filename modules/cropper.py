"""
Модуль 6: Кадрирование видео под вертикальный формат (9:16).

Режимы: center, face, smart, template, dual, stretch_bg (размытый фон cover + чёткое видео по ширине кадра по центру).
"""

import logging
import subprocess
import json
import math
from pathlib import Path

import cv2
try:
    import mediapipe as mp
except ImportError:
    mp = None  # Center crop remains available without the optional face detector.
import numpy as np

logger = logging.getLogger(__name__)

_ACOPY_FASTSTART = ["-c:a", "copy", "-movflags", "+faststart"]


def _clamp_vertical_pan_fraction(value: float) -> float:
    """Доля высоты источника под вертикальный pan (face/smart); вне [0.02, 0.45] — clamp."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        x = 0.10
    return max(0.02, min(0.45, x))

# x264 при кропе: задаётся через set_crop_encode() из config (video_encode).
_crop_x264_preset: str = "veryslow"
_crop_x264_crf: int = 14
# True → кроп в FFV1 (без H.264), финальный H.264 только при прожиге субтитров.
_crop_lossless_output: bool = False


def set_crop_encode(preset: str = "veryslow", crf: int = 14) -> None:
    """Пресет/CRF для libx264 на этапе кропа (вызывается из main по config.yaml)."""
    global _crop_x264_preset, _crop_x264_crf
    _crop_x264_preset = (preset or "veryslow").strip()
    _crop_x264_crf = int(crf)


def set_crop_lossless_output(enabled: bool) -> None:
    """Включает выход кропа в FFV1 вместо libx264 (парное использование с lossless_intermediate)."""
    global _crop_lossless_output
    _crop_lossless_output = bool(enabled)


def _x264_crop_args() -> list[str]:
    return [
        "-c:v", "libx264",
        "-preset", _crop_x264_preset,
        "-crf", str(_crop_x264_crf),
        "-pix_fmt", "yuv420p",
    ]


def _ffv1_crop_args() -> list[str]:
    return [
        "-c:v", "ffv1",
        "-level", "3",
        "-coder", "1",
        "-context", "1",
    ]


def _crop_encode_args() -> list[str]:
    return _ffv1_crop_args() if _crop_lossless_output else _x264_crop_args()


def _capture_video_info(
    cap: cv2.VideoCapture,
    video_path: Path,
    log_prefix: str,
) -> tuple[int, int, float, int]:
    """Return basic video metadata, failing before OpenCV can hang on audio-only clips."""
    src_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    if not cap.isOpened() or src_width <= 0 or src_height <= 0 or total_frames <= 0:
        cap.release()
        raise ValueError(
            f"{log_prefix} cannot read video frames from {video_path.name} "
            f"(width={src_width}, height={src_height}, frames={total_frames}). "
            "The clip may be audio-only or corrupted."
        )

    return src_width, src_height, fps, total_frames


def _resize_bgr(frame: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Масштаб: AREA при уменьшении, LANCZOS4 при увеличении — меньше мыла."""
    nw, nh = size
    h, w = frame.shape[:2]
    if min(nw / w, nh / h) < 1.0:
        interp = cv2.INTER_AREA
    elif abs(nw - w) < 2 and abs(nh - h) < 2:
        interp = cv2.INTER_LINEAR
    else:
        interp = cv2.INTER_LANCZOS4
    return cv2.resize(frame, size, interpolation=interp)


def get_video_dimensions(video_path: Path) -> tuple[int, int]:
    """
    Получить размеры видео через FFprobe.
    
    Returns:
        (width, height)
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        str(video_path)
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    return int(stream["width"]), int(stream["height"])


def _gaussian_smooth(values: list[float], sigma: float) -> list[float]:
    """Gaussian smoothing для траектории движения камеры."""
    if not values:
        return values
    arr = np.array(values, dtype=float)
    radius = int(3 * sigma)
    kernel_size = 2 * radius + 1
    kernel = np.array([
        math.exp(-0.5 * ((i - radius) / sigma) ** 2)
        for i in range(kernel_size)
    ])
    kernel /= kernel.sum()
    # Reflect padding чтобы не было артефактов на краях
    padded = np.pad(arr, radius, mode='reflect')
    return [float(np.dot(padded[i:i + kernel_size], kernel)) for i in range(len(arr))]


def _clamp(val: float, lo: int, hi: int) -> int:
    return max(lo, min(int(val), hi))


def _render_via_pipe(
    video_path: Path,
    output_path: Path,
    width: int,
    height: int,
    fps: float,
    total_frames: int,
    frame_processor,   # (frame: np.ndarray, frame_idx: int) -> np.ndarray
    log_prefix: str = "",
) -> None:
    """
    Проход 2: покадровый рендеринг через FFmpeg pipe.
    stderr=DEVNULL предотвращает дедлок на Windows (буфер ~4 KB переполняется прогрессом FFmpeg).
    """
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error", "-hide_banner",
        "-f", "rawvideo", "-pixel_format", "bgr24",
        "-video_size", f"{width}x{height}",
        "-framerate", str(fps),
        "-i", "pipe:0",
        "-i", str(video_path),
        "-map", "0:v", "-map", "1:a?",
        *_crop_encode_args(),
        *_ACOPY_FASTSTART,
        "-shortest",
        str(output_path),
    ]
    proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    cap = cv2.VideoCapture(str(video_path))
    frame_idx = 0
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            proc.stdin.write(frame_processor(frame, frame_idx).tobytes())
            frame_idx += 1
            if frame_idx % 100 == 0:
                logger.info(f"{log_prefix}  Кодирование {frame_idx}/{total_frames}")
    finally:
        cap.release()
        proc.stdin.close()

    proc.wait()
    if proc.returncode != 0:
        logger.error(f"{log_prefix} FFmpeg завершился с кодом {proc.returncode}")
        raise RuntimeError("Не удалось закодировать видео через FFmpeg pipe.")


def crop_center(video_path: Path, output_path: Path,
                width: int = 1080, height: int = 1920) -> Path:
    """
    Центральный кроп видео из горизонтального в вертикальный формат.

    Args:
        video_path: Путь к исходному видео.
        output_path: Путь для сохранения.
        width: Ширина выхода (1080 для Shorts).
        height: Высота выхода (1920 для Shorts).

    Returns:
        Path к обрезанному видео.
    """
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    logger.info(f"Центральный кроп {video_path.name} в {width}x{height}")
    
    # Получить размеры исходного видео
    src_width, src_height = get_video_dimensions(video_path)
    
    # Вычислить crop parameters
    target_aspect = width / height  # 9/16 = 0.5625
    src_aspect = src_width / src_height
    
    if src_aspect > target_aspect:
        # Горизонтальное видео — обрезаем по бокам
        crop_width = int(src_height * target_aspect)
        crop_height = src_height
        crop_x = (src_width - crop_width) // 2
        crop_y = 0
    else:
        # Уже вертикальное или квадратное — обрезаем сверху/снизу
        crop_width = src_width
        crop_height = int(src_width / target_aspect)
        crop_x = 0
        crop_y = (src_height - crop_height) // 2
    
    # FFmpeg команда
    cmd = [
        "ffmpeg",
        "-i", str(video_path),
        "-vf", (
            f"crop={crop_width}:{crop_height}:{crop_x}:{crop_y},"
            f"scale={width}:{height}:flags=lanczos,setpts=PTS-STARTPTS"
        ),
        *_crop_encode_args(),
        *_ACOPY_FASTSTART,
        "-y",
        str(output_path)
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, encoding='utf-8')
        logger.info(f"Видео обрезано: {output_path}")
        return output_path
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка FFmpeg при кропе: {e.stderr}")
        raise RuntimeError(f"Не удалось обрезать видео: {e.stderr}")


def crop_face(video_path: Path, output_path: Path,
              width: int = 1080, height: int = 1920,
              smoothing_sigma: float = 30.0,
              face_vertical_bias: float = 0.35,
              min_vertical_pan_fraction: float = 0.10) -> Path:
    """
    Покадровый динамический кроп с отслеживанием лица (MediaPipe Face Detection).

    Проход 1: анализ каждого кадра — получаем позицию лица.
    Проход 2: для каждого кадра применяем индивидуальный кроп по сглаженной
              траектории и пишем результат через FFmpeg pipe.

    Args:
        video_path: Путь к исходному видео.
        output_path: Путь для сохранения.
        width: Ширина выхода (1080 для Shorts).
        height: Высота выхода (1920 для Shorts).
        smoothing_sigma: Сигма Гауссова сглаживания (кадров). Чем больше —
                         тем плавнее движение камеры (рекомендуется 25–40).
        face_vertical_bias: Позиция лица по вертикали (0.0 = верх, 1.0 = низ).
                            0.35 = лицо в верхней трети кадра.
        min_vertical_pan_fraction: Доля высоты источника, оставляемая «под сдвиг» окна
            по вертикали. Старый дефолт 0.30 сильно уменьшает окно на 16:9 → upscale в
            1080×1920 и заметная зернистость; 0.08–0.12 обычно лучше для 4K.

    Returns:
        Path к обрезанному видео.
    """
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"[Face Crop] Покадровый анализ: {video_path.name} → {width}x{height}")

    # --- Открыть видео ---
    cap = cv2.VideoCapture(str(video_path))
    src_width, src_height, fps, total_frames = _capture_video_info(cap, video_path, "[Face Crop]")

    # Размер окна кропа в исходных пикселях
    target_aspect = width / height           # 9/16
    crop_w = int(src_height * target_aspect) # ширина окна
    crop_h = src_height                      # начальная высота = вся высота источника

    if crop_w > src_width:
        # Источник уже вертикальный — сводим к центру
        cap.release()
        logger.info("[Face Crop] Источник уже вертикальный, используем center crop.")
        return crop_center(video_path, output_path, width, height)

    # Для стандартного 16:9 crop_h == src_height → нет вертикального сдвига окна.
    # Уменьшаем crop_h, чтобы окно могло ездить по вертикали; слишком большой запас
    # (раньше 0.30) даёт маленькое окно и сильный upscale в 1080×1920 → «зерно».
    _pan = _clamp_vertical_pan_fraction(min_vertical_pan_fraction)
    min_v_range = int(src_height * _pan)
    if src_height - crop_h < min_v_range:
        crop_h = src_height - min_v_range
        crop_w = int(crop_h * target_aspect)

    # --- MediaPipe Face Detection ---
    try:
        mp_face_detection = mp.solutions.face_detection
        face_detection = mp_face_detection.FaceDetection(
            model_selection=1,
            min_detection_confidence=0.5
        )
        # Pre-warm: прогреваем модель первым РЕАЛЬНЫМ кадром видео.
        # TFLite/XNNPACK компилирует граф отдельно под каждый размер входа,
        # поэтому 64x64 заглушка не помогает — нужен кадр оригинального разрешения.
        ret_w, warm_frame = cap.read()
        if ret_w:
            face_detection.process(cv2.cvtColor(warm_frame, cv2.COLOR_BGR2RGB))
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # вернуться к началу
    except AttributeError:
        logger.warning("[Face Crop] mediapipe.solutions недоступен. Использую center crop.")
        cap.release()
        return crop_center(video_path, output_path, width, height)

    # =========================================================
    # ПРОХОД 1: собираем центры лица для каждого кадра
    # =========================================================
    raw_cx: list[float] = []  # x-координата центра лица
    raw_cy: list[float] = []  # y-координата центра лица
    face_found: list[bool] = []

    logger.info(f"[Face Crop] Проход 1: детекция лица в {total_frames} кадрах...")
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = face_detection.process(rgb)

        if results.detections:
            det  = results.detections[0]
            bbox = det.location_data.relative_bounding_box
            cx = (bbox.xmin + bbox.width  / 2) * src_width
            cy = (bbox.ymin + bbox.height / 2) * src_height
            raw_cx.append(cx)
            raw_cy.append(cy)
            face_found.append(True)
        else:
            # Плейсхолдер — заполним интерполяцией после
            raw_cx.append(None)
            raw_cy.append(None)
            face_found.append(False)

        frame_idx += 1
        if frame_idx % 100 == 0:
            logger.info(f"[Face Crop]   Кадр {frame_idx}/{total_frames}")

    cap.release()
    face_detection.close()
    face_rate = sum(face_found) / max(1, len(face_found))
    logger.info(f"[Face Crop] Лицо найдено в {sum(face_found)}/{total_frames} кадрах "
                f"({face_rate:.0%}).")

    # Если лица практически не нашли — кадрирование по лицу бесполезно,
    # лучше обычный центровой кроп чем случайное смещение.
    if face_rate < 0.15:
        logger.warning(f"[Face Crop] Детекция лица < 15% кадров — fallback → center crop.")
        return crop_center(video_path, output_path, width, height)

    # --- Интерполяция пропусков (нет лица) ---
    # Заполняем None через линейную интерполяцию соседних найденных значений.
    # Дефолт когда лицо не найдено нигде: cx=центр ширины, cy=позиция при которой
    # crop_y окажется посередине вертикального диапазона (не src_height/2 — иначе
    # при уменьшенном crop_h головы вылетают за верхний край кадра).
    default_cy = crop_h * face_vertical_bias + (src_height - crop_h) / 2
    for axis_list in (raw_cx, raw_cy):
        last_valid = None
        # Прямой проход
        for i in range(len(axis_list)):
            if axis_list[i] is not None:
                last_valid = axis_list[i]
            elif last_valid is not None:
                axis_list[i] = last_valid
        # Обратный проход (для начальных None)
        last_valid = None
        for i in range(len(axis_list) - 1, -1, -1):
            if axis_list[i] is not None:
                last_valid = axis_list[i]
            elif last_valid is not None:
                axis_list[i] = last_valid
        # Если вообще не нашли ни одного лица — центрированная позиция
        for i in range(len(axis_list)):
            if axis_list[i] is None:
                axis_list[i] = src_width / 2 if axis_list is raw_cx else default_cy

    # =========================================================
    # ПРОХОД СГЛАЖИВАНИЯ: Gaussian smoothing траектории
    # =========================================================
    smooth_cx = _gaussian_smooth(raw_cx, sigma=smoothing_sigma)
    smooth_cy = _gaussian_smooth(raw_cy, sigma=smoothing_sigma)

    # По горизонтали: лицо в центре окна; по вертикали: face_vertical_bias от верха
    crop_xs = [_clamp(cx - crop_w * 0.5, 0, src_width - crop_w) for cx in smooth_cx]
    crop_ys = [_clamp(cy - crop_h * face_vertical_bias, 0, src_height - crop_h) for cy in smooth_cy]

    # =========================================================
    # ПРОХОД 2: покадровая запись через FFmpeg pipe
    # =========================================================
    logger.info("[Face Crop] Проход 2: покадровый кроп и кодирование...")

    def _process_face(frame: np.ndarray, frame_idx: int) -> np.ndarray:
        cx_ = crop_xs[frame_idx] if frame_idx < len(crop_xs) else crop_xs[-1]
        cy_ = crop_ys[frame_idx] if frame_idx < len(crop_ys) else crop_ys[-1]
        cropped = frame[cy_:cy_ + crop_h, cx_:cx_ + crop_w]
        return _resize_bgr(cropped, (width, height))

    _render_via_pipe(video_path, output_path, width, height, fps, total_frames,
                     _process_face, log_prefix="[Face Crop]")
    logger.info(f"[Face Crop] Готово: {output_path}")
    return output_path


def _filter_complex_stretch_bg_blur(w: int, h: int, sigma: float) -> str:
    """
    FFmpeg: split → фон (cover + gblur) + передний план (ширина w, центр по вертикали).
    crop_fg обрезает по высоте, если после scale по ширине кадр выше h.
    """
    crop_fg = (
        f"crop={w}:if(gt(ih\\,{h})\\,{h}\\,ih):0:"
        f"if(gt(ih\\,{h})\\,(ih-{h})/2\\,0)"
    )
    return (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={w}:{h}:flags=lanczos:force_original_aspect_ratio=increase,"
        f"crop={w}:{h}:(iw-{w})/2:(ih-{h})/2,"
        f"gblur=sigma={sigma}[bgf];"
        f"[fg]scale={w}:-2:flags=lanczos,{crop_fg}[fgf];"
        f"[bgf][fgf]overlay=(W-w)/2:(H-h)/2[ov];"
        f"[ov]setpts=PTS-STARTPTS,format=yuv420p[vout]"
    )


def crop_stretch_bg(video_path: Path, output_path: Path,
                    width: int = 1080, height: int = 1920,
                    blur_sigma: float = 26.0) -> Path:
    """
    Вертикаль 9:16: размытый фон + чёткий центр (как в Reels/Shorts).

    - Фон: то же видео в режиме «cover» на весь кадр, затем Gaussian blur
      (sigma задаётся параметром blur_sigma).
    - Поверх: то же видео без размытия, ширина = ширина кадра, по вертикали
      по центру; сверху и снизу виден мягкий размытый фон.
    - Очень высокий исходник: верхний слой после подгонки по ширине обрезается
      по центру по высоте (как раньше).
    """
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    w, h = width, height
    sigma = max(1.0, min(float(blur_sigma), 80.0))
    logger.info(
        f"[Stretch BG] {video_path.name} → {w}x{h} "
        f"(размытый фон σ={sigma:.1f} + чёткий слой по ширине)"
    )
    vf = _filter_complex_stretch_bg_blur(w, h, sigma)
    cmd = [
        "ffmpeg",
        "-i", str(video_path),
        "-filter_complex", vf,
        "-map", "[vout]",
        "-map", "0:a?",
        *_crop_encode_args(),
        *_ACOPY_FASTSTART,
        "-shortest",
        "-y",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8")
        logger.info(f"[Stretch BG] Готово: {output_path}")
        return output_path
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка FFmpeg [Stretch BG]: {e.stderr}")
        raise RuntimeError(f"Не удалось собрать кадр stretch_bg: {e.stderr}")


def crop_template(video_path: Path, output_path: Path,
                  bg_color: str = "#1a1a2e",
                  width: int = 1080, height: int = 1920) -> Path:
    """
    Шаблонный кроп: видео сверху (с сохранением пропорций) + цветной фон снизу.

    Args:
        video_path: Путь к исходному видео.
        output_path: Путь для сохранения.
        bg_color: Цвет фона (hex).
        width: Ширина выхода.
        height: Высота выхода.

    Returns:
        Path к обрезанному видео.
    """
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    logger.info(f"Шаблонный кроп {video_path.name} в {width}x{height}")
    
    # Конвертировать hex цвет в FFmpeg формат (0xRRGGBB)
    bg_color_ffmpeg = bg_color.replace("#", "0x")
    
    # FFmpeg: масштабировать видео по ширине, добавить padding снизу
    cmd = [
        "ffmpeg",
        "-i", str(video_path),
        "-vf", (
            f"scale={width}:-1:flags=lanczos,"
            f"pad={width}:{height}:0:(oh-ih)/2:color={bg_color_ffmpeg},"
            f"setpts=PTS-STARTPTS"
        ),
        *_crop_encode_args(),
        *_ACOPY_FASTSTART,
        "-y",
        str(output_path)
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, encoding='utf-8')
        logger.info(f"Видео обрезано (шаблон): {output_path}")
        return output_path
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка FFmpeg: {e.stderr}")
        raise RuntimeError(f"Не удалось обрезать видео: {e.stderr}")


def _letterbox_frame(frame: np.ndarray, width: int, height: int,
                     bg: tuple = (15, 15, 20)) -> np.ndarray:
    """Вписать кадр в width×height с тёмными полосами (letterbox)."""
    h, w = frame.shape[:2]
    scale = min(width / w, height / h)
    nw, nh = int(w * scale), int(h * scale)
    resized = _resize_bgr(frame, (nw, nh))
    canvas = np.full((height, width, 3), bg, dtype=np.uint8)
    y0 = (height - nh) // 2
    x0 = (width  - nw) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = resized
    return canvas


def crop_smart(video_path: Path, output_path: Path,
               width: int = 1080, height: int = 1920,
               smoothing_sigma: float = 30.0,
               face_vertical_bias: float = 0.35,
               spread_threshold: float = 0.30,
               mode_hold_frames: int = 25,
               analysis_scale: float = 0.25,
               frame_step: int = 2,
               min_vertical_pan_fraction: float = 0.10) -> Path:
    """
    Умный кроп: автоматически выбирает между тремя режимами покадрово:
      - "face"      — лицо найдено → следим за лицом
      - "crop"      — движение сконцентрировано → следим за центром движения
      - "letterbox" — движение размазано по всему кадру (эксперименты, два объекта)
                      → показываем полный кадр с тёмными полосами

    spread_threshold: доля ширины кадра; если std(x движения) > порога → letterbox.
    mode_hold_frames: минимум кадров в режиме перед переключением (против мерцания).
    min_vertical_pan_fraction: см. crop_face — влияет на размер окна до upscale в Shorts.
    """
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"[Smart Crop] Анализ: {video_path.name} → {width}x{height}")

    cap = cv2.VideoCapture(str(video_path))
    src_width, src_height, fps, total_frames = _capture_video_info(cap, video_path, "[Smart Crop]")

    target_aspect = width / height
    crop_w = int(src_height * target_aspect)
    crop_h = src_height

    if crop_w > src_width:
        cap.release()
        logger.info("[Smart Crop] Источник уже вертикальный, используем center crop.")
        return crop_center(video_path, output_path, width, height)

    _pan = _clamp_vertical_pan_fraction(min_vertical_pan_fraction)
    min_v_range = int(src_height * _pan)
    if src_height - crop_h < min_v_range:
        crop_h = src_height - min_v_range
        crop_w = int(crop_h * target_aspect)

    # --- MediaPipe Face Detection ---
    try:
        mp_face_detection = mp.solutions.face_detection
        face_detection = mp_face_detection.FaceDetection(
            model_selection=1, min_detection_confidence=0.5
        )
        ret_w, warm_frame = cap.read()
        if ret_w:
            face_detection.process(cv2.cvtColor(warm_frame, cv2.COLOR_BGR2RGB))
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        has_mediapipe = True
    except AttributeError:
        has_mediapipe = False
        logger.warning("[Smart Crop] mediapipe недоступен, только motion tracking.")

    # =========================================================
    # ПРОХОД 1: детекция лица + оптический поток + spread анализ
    # Оптимизация: анализ при уменьшенном разрешении (analysis_scale)
    # и через каждые frame_step кадров для ускорения на CPU.
    # =========================================================
    raw_cx: list[float] = []
    raw_cy: list[float] = []
    # "face" | "crop" | "letterbox" | "none"
    focus_source: list[str] = []

    # Размеры уменьшенного кадра для анализа движения
    _aw = max(16, int(src_width * analysis_scale))
    _ah = max(9, int(src_height * analysis_scale))
    _sx = src_width / _aw   # коэффициент масштабирования обратно
    _sy = src_height / _ah

    prev_gray_small = None
    _last_cx = float(src_width / 2)
    # Дефолтная cy: позиция при которой crop_y окажется посередине вертикального диапазона.
    # src_height/2 неверно при уменьшенном crop_h — смещает кроп вниз, срезая головы.
    _last_cy = float(crop_h * face_vertical_bias + (src_height - crop_h) / 2)
    _last_src = "none"
    frame_idx = 0
    logger.info(f"[Smart Crop] Проход 1: {total_frames} кадров "
                f"(масштаб анализа={analysis_scale}, шаг={frame_step})...")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Уменьшенный серый кадр вычисляем всегда (быстро)
        small = cv2.resize(frame, (_aw, _ah), interpolation=cv2.INTER_AREA)
        gray_small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        if frame_idx % frame_step == 0:
            # Полный анализ на ключевых кадрах
            cx, cy, source = None, None, "none"

            # 1. Ищем лицо
            if has_mediapipe:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                results = face_detection.process(rgb)
                if results.detections:
                    det  = results.detections[0]
                    bbox = det.location_data.relative_bounding_box
                    cx = (bbox.xmin + bbox.width  / 2) * src_width
                    cy = (bbox.ymin + bbox.height / 2) * src_height
                    source = "face"

            # 2. Если лица нет — оптический поток на уменьшенном кадре
            if cx is None and prev_gray_small is not None:
                flow = cv2.calcOpticalFlowFarneback(
                    prev_gray_small, gray_small, None,
                    pyr_scale=0.5, levels=3, winsize=15,
                    iterations=3, poly_n=5, poly_sigma=1.2, flags=0
                )
                mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
                threshold = np.percentile(mag, 90)
                motion_mask = mag > threshold

                if motion_mask.any():
                    ys, xs = np.where(motion_mask)
                    weights = mag[ys, xs]
                    # Масштабируем координаты обратно к оригинальному разрешению
                    cx = float(np.average(xs, weights=weights)) * _sx
                    cy = float(np.average(ys, weights=weights)) * _sy
                    x_std = float(np.std(xs)) * _sx
                    if x_std > spread_threshold * src_width:
                        source = "letterbox"
                    else:
                        source = "crop"

            _last_cx = cx if cx is not None else src_width / 2
            _last_cy = cy if cy is not None else src_height / 2
            _last_src = source if cx is not None else "none"

        # Для пропущенных кадров используем результат предыдущего анализа
        raw_cx.append(_last_cx)
        raw_cy.append(_last_cy)
        focus_source.append(_last_src)

        prev_gray_small = gray_small
        frame_idx += 1
        if frame_idx % 100 == 0:
            lb = focus_source.count("letterbox")
            logger.info(f"[Smart Crop]   Кадр {frame_idx}/{total_frames} "
                        f"(лиц: {focus_source.count('face')}, "
                        f"кроп: {focus_source.count('crop')}, "
                        f"полный: {lb})")

    cap.release()
    if has_mediapipe:
        face_detection.close()

    logger.info(f"[Smart Crop] Итог: лиц={focus_source.count('face')}, "
                f"кроп={focus_source.count('crop')}, "
                f"полный={focus_source.count('letterbox')}, "
                f"пусто={focus_source.count('none')}/{total_frames}")

    # --- Сглаживание режима: убираем мерцание (hysteresis) ---
    # Если режим держится меньше mode_hold_frames — оставляем предыдущий
    smoothed_source = list(focus_source)
    current_mode, hold_count = focus_source[0] if focus_source else "none", 0
    for i in range(len(smoothed_source)):
        if smoothed_source[i] == current_mode:
            hold_count += 1
        else:
            if hold_count >= mode_hold_frames:
                current_mode = smoothed_source[i]
                hold_count = 1
            else:
                smoothed_source[i] = current_mode
                hold_count += 1
    focus_source = smoothed_source

    # --- Gaussian smoothing траектории ---
    smooth_cx = _gaussian_smooth(raw_cx, sigma=smoothing_sigma)
    smooth_cy = _gaussian_smooth(raw_cy, sigma=smoothing_sigma)

    # Вертикальный bias сглаживается отдельно — иначе при переключении face→crop
    # множитель скачет с 0.35 на 0.5 мгновенно (~162px для 1080p), что выглядит
    # как резкий "срез" по кадру. Smoothing даёт плавный переход ~2 сек.
    raw_bias = [face_vertical_bias if s == "face" else 0.5 for s in focus_source]
    smooth_bias = _gaussian_smooth(raw_bias, sigma=smoothing_sigma)

    crop_xs = [_clamp(cx - crop_w * 0.5, 0, src_width - crop_w) for cx in smooth_cx]
    crop_ys = [
        _clamp(smooth_cy[i] - crop_h * smooth_bias[i], 0, src_height - crop_h)
        for i in range(len(smooth_cy))
    ]

    # =========================================================
    # ПРОХОД 2: покадровый рендеринг через FFmpeg pipe
    # =========================================================
    logger.info(f"[Smart Crop] Проход 2: кодирование {total_frames} кадров...")

    def _process_smart(frame: np.ndarray, frame_idx: int) -> np.ndarray:
        src = focus_source[frame_idx] if frame_idx < len(focus_source) else focus_source[-1]
        if src == "letterbox":
            return _letterbox_frame(frame, width, height)
        cx_ = crop_xs[frame_idx] if frame_idx < len(crop_xs) else crop_xs[-1]
        cy_ = crop_ys[frame_idx] if frame_idx < len(crop_ys) else crop_ys[-1]
        cropped = frame[cy_:cy_ + crop_h, cx_:cx_ + crop_w]
        return _resize_bgr(cropped, (width, height))

    _render_via_pipe(video_path, output_path, width, height, fps, total_frames,
                     _process_smart, log_prefix="[Smart Crop]")
    logger.info(f"[Smart Crop] Готово: {output_path}")
    return output_path


def crop_dual(video_path: Path, output_path: Path,
              width: int = 1080, height: int = 1920,
              smoothing_sigma: float = 25.0,
              analysis_scale: float = 0.5,
              frame_step: int = 2) -> Path:
    """
    Dual-split кроп (стиль Opus Clip):
      Верхняя панель = левый спикер / левая часть кадра.
      Нижняя  панель = правый спикер / правая часть кадра.

    Алгоритм:
      Проход 1: MediaPipe детектирует все лица в ключевых кадрах.
                Лицо с меньшим x → верхняя панель; с большим x → нижняя.
                Если лицо одно — верхняя/нижняя определяется по стороне кадра;
                вторая панель фиксируется на противоположной половине.
                Без лиц — стандартное деление (левая / правая четверти).
      Проход 2: покадровый рендеринг через FFmpeg pipe (два вертикально склеенных кропа).
    """
    video_path  = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"[Dual Crop] Анализ: {video_path.name} → {width}x{height}")

    panel_w = width
    panel_h = height // 2          # 960
    panel_aspect = panel_w / panel_h  # 9/8 = 1.125

    cap = cv2.VideoCapture(str(video_path))
    src_width, src_height, fps, total_frames = _capture_video_info(cap, video_path, "[Dual Crop]")

    # Размер кропа для одной панели: полная высота источника, ширина из соотношения сторон
    crop_h = src_height
    crop_w = int(src_height * panel_aspect)
    if crop_w > src_width:
        crop_w = src_width
        crop_h = int(crop_w / panel_aspect)

    if crop_w * 2 > src_width + 1:
        # Источник недостаточно широкий для dual (почти квадрат/вертикаль) — fallback
        cap.release()
        logger.info("[Dual Crop] Источник слишком узкий для dual, fallback → face crop.")
        return crop_face(video_path, output_path, width, height)

    # MediaPipe
    try:
        mp_face_detection = mp.solutions.face_detection
        face_detection = mp_face_detection.FaceDetection(
            model_selection=1, min_detection_confidence=0.5
        )
        ret_w, warm_frame = cap.read()
        if ret_w:
            face_detection.process(cv2.cvtColor(warm_frame, cv2.COLOR_BGR2RGB))
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        has_mediapipe = True
    except AttributeError:
        has_mediapipe = False
        logger.warning("[Dual Crop] mediapipe недоступен, используем деление пополам.")

    # =========================================================
    # ПРОХОД 1: определяем x-позиции для каждой панели
    # =========================================================
    raw_top_cx: list[float] = []
    raw_bot_cx: list[float] = []

    _last_top_cx = float(src_width * 0.25)   # дефолт: левая четверть
    _last_bot_cx = float(src_width * 0.75)   # дефолт: правая четверть

    logger.info(f"[Dual Crop] Проход 1: {total_frames} кадров (шаг={frame_step})...")
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % frame_step == 0 and has_mediapipe:
            rgb     = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            results = face_detection.process(rgb)
            if results.detections:
                faces = sorted(
                    [(det.location_data.relative_bounding_box.xmin +
                      det.location_data.relative_bounding_box.width / 2) * src_width
                     for det in results.detections]
                )
                if len(faces) >= 2:
                    _last_top_cx = faces[0]           # левое лицо → верхняя панель
                    _last_bot_cx = faces[-1]          # правое лицо → нижняя панель
                else:
                    fx = faces[0]
                    if fx < src_width * 0.5:
                        _last_top_cx = fx
                        # нижняя панель остаётся на правой половине
                    else:
                        _last_bot_cx = fx
                        # верхняя панель остаётся на левой половине

        raw_top_cx.append(_last_top_cx)
        raw_bot_cx.append(_last_bot_cx)
        frame_idx += 1
        if frame_idx % 200 == 0:
            logger.info(f"[Dual Crop]   Кадр {frame_idx}/{total_frames}")

    cap.release()
    if has_mediapipe:
        face_detection.close()

    # =========================================================
    # СГЛАЖИВАНИЕ + расчёт координат кропа
    # =========================================================
    smooth_top = _gaussian_smooth(raw_top_cx, sigma=smoothing_sigma)
    smooth_bot = _gaussian_smooth(raw_bot_cx, sigma=smoothing_sigma)

    top_xs = [_clamp(cx - crop_w * 0.5, 0, src_width - crop_w) for cx in smooth_top]
    bot_xs = [_clamp(cx - crop_w * 0.5, 0, src_width - crop_w) for cx in smooth_bot]

    # =========================================================
    # ПРОХОД 2: покадровый рендеринг (две панели вертикально)
    # =========================================================
    logger.info(f"[Dual Crop] Проход 2: кодирование {total_frames} кадров...")

    def _process_dual(frame: np.ndarray, fi: int) -> np.ndarray:
        tx = top_xs[fi] if fi < len(top_xs) else top_xs[-1]
        bx = bot_xs[fi] if fi < len(bot_xs) else bot_xs[-1]
        top_panel = _resize_bgr(
            frame[0:crop_h, tx:tx + crop_w],
            (panel_w, panel_h),
        )
        bot_panel = _resize_bgr(
            frame[0:crop_h, bx:bx + crop_w],
            (panel_w, panel_h),
        )
        return np.vstack([top_panel, bot_panel])

    _render_via_pipe(video_path, output_path, width, height, fps, total_frames,
                     _process_dual, log_prefix="[Dual Crop]")
    logger.info(f"[Dual Crop] Готово: {output_path}")
    return output_path


def crop_video(video_path: Path, output_path: Path,
               mode: str = "center", **kwargs) -> Path:
    """
    Кадрирование видео (диспетчер по режиму).

    Args:
        video_path: Путь к исходному видео.
        output_path: Путь для сохранения.
        mode: Режим кропа: "center", "face", "smart", "template", "dual", "stretch_bg".

    Returns:
        Path к обрезанному видео.
    """
    mode = (mode or "center").strip().lower().replace("-", "_")
    min_vertical_pan_fraction = float(kwargs.pop("min_vertical_pan_fraction", 0.10) or 0.10)

    if mode == "center":
        return crop_center(video_path, output_path, **kwargs)
    elif mode == "face":
        return crop_face(
            video_path, output_path,
            min_vertical_pan_fraction=min_vertical_pan_fraction,
            **kwargs,
        )
    elif mode == "smart":
        return crop_smart(
            video_path, output_path,
            min_vertical_pan_fraction=min_vertical_pan_fraction,
            **kwargs,
        )
    elif mode == "template":
        return crop_template(video_path, output_path, **kwargs)
    elif mode == "dual":
        return crop_dual(video_path, output_path, **kwargs)
    elif mode == "stretch_bg":
        return crop_stretch_bg(video_path, output_path, **kwargs)
    else:
        raise ValueError(f"Неизвестный режим кропа: {mode}")
