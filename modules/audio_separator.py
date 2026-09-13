"""
Модуль 3: Разделение и замена фонового аудио.

Demucs (разделение) -> MusicGen (генерация нового фона) -> Ducking -> Микширование.
"""

import logging
import subprocess
import sys
import tempfile
import os
from pathlib import Path

import torch
import torchaudio
from audiocraft.models import MusicGen
from pydub import AudioSegment

logger = logging.getLogger(__name__)


def _resolve_device(device: str) -> str:
    """Вернуть 'cpu' если cuda запрошена, но недоступна."""
    if device == "cuda" and not torch.cuda.is_available():
        logger.warning("CUDA недоступна, переключаюсь на CPU")
        return "cpu"
    return device


def separate_audio(audio_path: Path, output_dir: Path, 
                   model: str = "htdemucs", device: str = "cuda") -> dict:
    """
    Разделить аудио на stems с помощью Demucs (htdemucs).

    Args:
        audio_path: Путь к аудиофайлу клипа.
        output_dir: Папка для сохранения stems.
        model: Модель Demucs (htdemucs, htdemucs_ft).
        device: "cuda" или "cpu".

    Returns:
        dict с полями:
            - vocals: Path к vocals.wav
            - drums: Path к drums.wav
            - bass: Path к bass.wav
            - other: Path к other.wav
    """
    audio_path = Path(audio_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    device = _resolve_device(device)
    
    logger.info(f"Разделение аудио {audio_path.name} через Demucs ({model}, {device})")
    
    # Demucs создаёт структуру: output_dir/htdemucs/audio_name/{stems}
    # Используем subprocess для вызова demucs CLI
    cmd = [
        sys.executable, "-m", "demucs",
        "--two-stems", "vocals",  # Быстрее: только vocals + остальное
        "-n", model,
        "--device", device,
        "-o", str(output_dir),
        str(audio_path)
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
        logger.info(f"Demucs завершён")
        
        # Найти результаты
        # Структура: output_dir/htdemucs/audio_name/vocals.wav и no_vocals.wav
        audio_name = audio_path.stem
        stems_dir = output_dir / model / audio_name
        
        vocals_path = stems_dir / "vocals.wav"
        no_vocals_path = stems_dir / "no_vocals.wav"
        
        if not vocals_path.exists() or not no_vocals_path.exists():
            raise FileNotFoundError(f"Demucs stems не найдены в {stems_dir}")
        
        logger.info(f"Stems сохранены: vocals={vocals_path.name}, фон={no_vocals_path.name}")
        
        return {
            "vocals": vocals_path,
            "background": no_vocals_path,  # drums + bass + other
            "stems_dir": stems_dir
        }
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка Demucs: {e.stderr}")
        raise RuntimeError(f"Не удалось разделить аудио: {e.stderr}")


def generate_background(original_bg_path: Path, duration: float,
                        prompt: str = "dark ambient documentary texture, "
                                      "slow evolving drone, no melody, no drums, "
                                      "unsettling atmosphere",
                        model_name: str = "facebook/musicgen-melody",
                        device: str = "cuda",
                        output_path: Path = None) -> Path:
    """
    Сгенерировать новый фоновый трек с помощью MusicGen (audio conditioning).

    Args:
        original_bg_path: Путь к оригинальному фоновому аудио (для conditioning).
        duration: Длительность в секундах.
        prompt: Текстовое описание желаемого стиля.
        model_name: Модель MusicGen.
        device: "cuda" или "cpu".
        output_path: Путь для сохранения (если None — temp).

    Returns:
        Path к сгенерированному WAV-файлу.
    """
    original_bg_path = Path(original_bg_path)

    device = _resolve_device(device)
    
    logger.info(f"Генерация нового фона через MusicGen (melody conditioning)")
    logger.info(f"  Промпт: {prompt}")
    logger.info(f"  Длительность: {duration:.1f}s")
    
    # Загрузка модели MusicGen
    model = MusicGen.get_pretrained(model_name, device=device)
    model.set_generation_params(duration=min(duration, 30))  # MusicGen макс 30 сек за раз
    
    # Загрузка оригинального фона для conditioning
    melody_waveform, melody_sr = torchaudio.load(str(original_bg_path))
    
    # Если стерео → моно
    if melody_waveform.shape[0] > 1:
        melody_waveform = melody_waveform.mean(dim=0, keepdim=True)
    
    # Ресемплинг если нужно (MusicGen работает с 32kHz)
    if melody_sr != 32000:
        resampler = torchaudio.transforms.Resample(melody_sr, 32000)
        melody_waveform = resampler(melody_waveform)
        melody_sr = 32000
    
    # Если длительность > 30 сек, генерируем по частям
    if duration > 30:
        logger.info(f"  Длительность {duration}s > 30s, генерация по сегментам")
        segments = []
        current_time = 0
        segment_duration = 30
        
        while current_time < duration:
            segment_len = min(segment_duration, duration - current_time)
            model.set_generation_params(duration=segment_len)
            
            # Вырезать соответствующий кусок conditioning
            start_sample = int(current_time * melody_sr)
            end_sample = int((current_time + segment_len) * melody_sr)
            melody_segment = melody_waveform[:, start_sample:end_sample]
            
            # Генерация
            with torch.no_grad():
                wav = model.generate_with_chroma(
                    descriptions=[prompt],
                    melody_wavs=melody_segment.to(device),
                    melody_sample_rate=melody_sr,
                    progress=False
                )
            
            segments.append(wav[0].cpu())
            current_time += segment_len
        
        # Склеиваем сегменты
        generated_waveform = torch.cat(segments, dim=1)
        
    else:
        # Генерация одним куском
        with torch.no_grad():
            wav = model.generate_with_chroma(
                descriptions=[prompt],
                melody_wavs=melody_waveform.to(device),
                melody_sample_rate=melody_sr,
                progress=False
            )
        generated_waveform = wav[0].cpu()
    
    # Сохранение
    if output_path is None:
        output_path = Path(tempfile.mktemp(suffix=".wav", prefix="musicgen_bg_"))
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    
    torchaudio.save(
        str(output_path),
        generated_waveform,
        sample_rate=32000
    )
    
    logger.info(f"Новый фон сгенерирован: {output_path}")
    return output_path


def apply_ducking(vocals_path: Path, background_path: Path,
                  duck_db: float = -15.0, boost_db: float = 6.0,
                  threshold_db: float = -35.0, chunk_ms: int = 500,
                  output_path: Path = None) -> Path:
    """
    Применить ducking: приглушить фон когда звучит голос, усилить в паузах.

    Args:
        vocals_path: Путь к вокальной дорожке.
        background_path: Путь к фоновому аудио.
        duck_db: На сколько dB приглушать фон при голосе.
        boost_db: На сколько dB усиливать фон в паузах.
        threshold_db: Порог dBFS ниже которого считается тишина (пауза).
        chunk_ms: Размер анализируемого фрагмента (мс).
        output_path: Путь для сохранения (если None — temp).

    Returns:
        Path к результирующему аудио с ducking.
    """
    logger.info(f"Применение ducking к фоновому аудио")
    
    # Загрузка аудио
    vocals = AudioSegment.from_file(str(vocals_path))
    background = AudioSegment.from_file(str(background_path))
    
    # Подогнать длительность фона под вокал
    if len(background) < len(vocals):
        # Зациклить фон
        repeats = (len(vocals) // len(background)) + 1
        background = background * repeats
    background = background[:len(vocals)]
    
    # Приглушить фон по умолчанию на duck_db
    background = background + duck_db
    
    # Анализ вокала по chunk'ам и регулировка фона
    result_chunks = []
    for i in range(0, len(vocals), chunk_ms):
        vocal_chunk = vocals[i:i + chunk_ms]
        bg_chunk = background[i:i + chunk_ms]
        
        # Проверка: тишина или речь
        if vocal_chunk.dBFS < threshold_db:
            # Тишина (пауза) — усилить фон
            bg_chunk = bg_chunk + boost_db
        # Иначе оставляем приглушённым
        
        result_chunks.append(bg_chunk)
    
    # Склеиваем
    ducked_background = sum(result_chunks)
    
    # Сохранение
    if output_path is None:
        output_path = Path(tempfile.mktemp(suffix=".wav", prefix="ducked_bg_"))
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    
    ducked_background.export(str(output_path), format="wav")
    
    logger.info(f"Ducking применён: {output_path}")
    return output_path


def mix_audio(vocals_path: Path, background_path: Path,
              output_path: Path, vocals_gain_db: float = 0,
              background_gain_db: float = 0) -> Path:
    """
    Финальное микширование: vocals + фон.

    Args:
        vocals_path: Вокальная дорожка.
        background_path: Фоновая дорожка (желательно с ducking).
        output_path: Путь для сохранения результата.
        vocals_gain_db: Регулировка громкости вокала (dB).
        background_gain_db: Регулировка громкости фона (dB).

    Returns:
        Path к финальному аудиофайлу.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    logger.info(f"Финальное микширование аудио")
    
    vocals = AudioSegment.from_file(str(vocals_path))
    background = AudioSegment.from_file(str(background_path))
    
    # Применить gain
    if vocals_gain_db != 0:
        vocals = vocals + vocals_gain_db
    if background_gain_db != 0:
        background = background + background_gain_db
    
    # Подогнать длину
    if len(background) < len(vocals):
        background = background * ((len(vocals) // len(background)) + 1)
    background = background[:len(vocals)]
    
    # Наложение
    mixed = vocals.overlay(background)
    
    # Экспорт
    mixed.export(str(output_path), format="wav")
    
    logger.info(f"Аудио смикшировано: {output_path}")
    return output_path
