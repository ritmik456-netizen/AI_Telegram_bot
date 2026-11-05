import asyncio
import logging
from typing import BinaryIO

from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)


class SpeechRecognizer:
    """Распознаёт речь локально через Whisper: бесплатно и без лимитов, но нагружает процессор."""

    def __init__(self, model_size: str) -> None:
        self._model_size = model_size
        self._model: WhisperModel | None = None
        self._lock = asyncio.Lock()

    def _load(self) -> WhisperModel:
        if self._model is None:
            logger.info("Загружаю модель распознавания речи Whisper %s...", self._model_size)
            self._model = WhisperModel(self._model_size, device="cpu", compute_type="int8")
            logger.info("Модель Whisper %s готова", self._model_size)
        return self._model

    def _transcribe(self, audio: BinaryIO) -> str:
        segments, _ = self._load().transcribe(audio, beam_size=1, vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def preload(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._load)

    async def transcribe(self, audio: BinaryIO) -> str:
        # Одновременно распознаём только одно сообщение, иначе процессор не справится.
        async with self._lock:
            return await asyncio.to_thread(self._transcribe, audio)
