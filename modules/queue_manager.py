"""
Управление очередью загрузки на YouTube.

Копирование готовых клипов в очередь каналов.
"""

import logging
import json
import shutil
from pathlib import Path

from modules.language_guard import assert_clip_language_matches_channel
from modules.manual_metadata import write_manual_upload_text, write_plain_upload_text

logger = logging.getLogger(__name__)


def add_to_queue(video_path: Path, metadata_path: Path, 
                 channel_name: str, queue_base_dir: Path,
                 channel_config: dict = None) -> Path:
    """
    Добавить видео в очередь загрузки для канала.

    Args:
        video_path: Путь к готовому видео.
        metadata_path: Путь к JSON с метаданными.
        channel_name: Имя канала из конфига.
        queue_base_dir: Базовая папка очередей (queue/).

    Returns:
        Path к файлу в очереди.

    Имена в очереди всегда с префиксом project_id (папка output), иначе clip_001.mp4 из
    разных проектов перезаписывают друг друга в одной папке канала.
    """
    video_path = Path(video_path)
    metadata_path = Path(metadata_path)
    queue_dir = Path(queue_base_dir) / channel_name
    queue_dir.mkdir(parents=True, exist_ok=True)

    metadata = None
    if metadata_path.exists():
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
    if channel_config is not None and metadata is not None:
        assert_clip_language_matches_channel(metadata, channel_name, channel_config, video_path.name)

    if video_path.parent.name == "clips_final":
        project_id = video_path.parent.parent.name
    else:
        project_id = video_path.stem

    unique_stem = f"{project_id}_{video_path.stem}"
    queue_video_path = queue_dir / f"{unique_stem}.mp4"
    shutil.copy2(video_path, queue_video_path)

    if metadata_path.exists():
        if metadata_path.name.endswith("_meta.json"):
            queue_meta_path = queue_dir / f"{unique_stem}_meta.json"
        else:
            queue_meta_path = queue_dir / f"{unique_stem}.json"
        shutil.copy2(metadata_path, queue_meta_path)
        if metadata is not None:
            write_manual_upload_text(queue_video_path, metadata)
            write_plain_upload_text(queue_video_path, metadata)

    logger.info(f"Добавлено в очередь {channel_name}: {queue_video_path.name}")
    return queue_video_path


def distribute_clips(clips_dir: Path, channels_config: dict,
                     queue_base_dir: Path, language: str = "en") -> dict:
    """
    Распределить клипы по очередям каналов согласно языку.

    Args:
        clips_dir: Папка с готовыми клипами.
        channels_config: Конфигурация каналов из config.yaml.
        queue_base_dir: Базовая папка очередей.
        language: Язык клипов.

    Returns:
        dict с количеством добавленных клипов по каналам.
    """
    clips_dir = Path(clips_dir)
    if not clips_dir.exists():
        logger.warning(f"Папка клипов не найдена: {clips_dir}")
        return {}
    
    # Найти канал для этого языка
    target_channel = None
    for channel_name, channel_config in channels_config.items():
        if channel_config.get("language") == language:
            target_channel = channel_name
            break
    
    if not target_channel:
        logger.warning(f"Не найден канал для языка {language}")
        return {}
    
    # Добавить все клипы в очередь этого канала
    video_files = sorted(clips_dir.glob("*.mp4"))
    added = 0
    
    for video_path in video_files:
        meta_path = video_path.with_suffix('.json')
        if not meta_path.exists():
            # Попробовать найти _meta.json
            meta_path = video_path.parent / f"{video_path.stem}_meta.json"
        
        if meta_path.exists():
            add_to_queue(
                video_path,
                meta_path,
                target_channel,
                queue_base_dir,
                channels_config.get(target_channel, {}),
            )
            added += 1
        else:
            logger.warning(f"Метаданные не найдены для {video_path.name}, пропускаем")
    
    logger.info(f"Добавлено {added} клипов в очередь {target_channel}")
    return {target_channel: added}


def get_queue_status(queue_base_dir: Path, channels_config: dict) -> dict:
    """
    Получить статус очередей всех каналов.

    Args:
        queue_base_dir: Базовая папка очередей.
        channels_config: Конфигурация каналов.

    Returns:
        dict: channel_name -> количество видео в очереди.
    """
    queue_base_dir = Path(queue_base_dir)
    status = {}
    
    for channel_name in channels_config.keys():
        queue_dir = queue_base_dir / channel_name
        if queue_dir.exists():
            video_count = len(list(queue_dir.glob("*.mp4")))
        else:
            video_count = 0
        status[channel_name] = video_count
    
    return status


def clear_queue(channel_name: str, queue_base_dir: Path):
    """
    Очистить очередь канала.

    Args:
        channel_name: Имя канала.
        queue_base_dir: Базовая папка очередей.
    """
    queue_dir = Path(queue_base_dir) / channel_name
    if not queue_dir.exists():
        logger.info(f"Очередь {channel_name} уже пуста")
        return
    
    files = list(queue_dir.glob("*"))
    for file in files:
        file.unlink()
    
    logger.info(f"Очередь {channel_name} очищена ({len(files)} файлов удалено)")
