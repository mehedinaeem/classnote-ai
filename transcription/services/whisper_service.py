"""Lazy faster-whisper inference using predownloaded local model files."""
from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import threading
import time
import wave

from django.conf import settings

logger = logging.getLogger(__name__)
_model = None
_model_pid = None
_model_config = None
_model_lock = threading.RLock()


class WhisperError(Exception):
    """Base local transcription error."""


class WhisperDependencyError(WhisperError):
    pass


class WhisperModelError(WhisperError):
    pass


class WhisperInferenceError(WhisperError):
    pass


@dataclass
class TranscriptionResult:
    segments: list
    full_text: str
    detected_language: str
    processing_time: float


def _after_fork():
    global _model, _model_pid, _model_config, _model_lock
    _model = None
    _model_pid = None
    _model_config = None
    _model_lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _get_model():
    global _model, _model_pid, _model_config
    config = (settings.WHISPER_MODEL, settings.WHISPER_DEVICE,
              settings.WHISPER_COMPUTE_TYPE, str(settings.WHISPER_DOWNLOAD_ROOT))
    with _model_lock:
        if _model is not None and _model_pid == os.getpid():
            if _model_config != config:
                raise WhisperModelError("Whisper configuration changed. Restart the process to load it.")
            return _model
        try:
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model
        except (ImportError, OSError) as exc:
            raise WhisperDependencyError("Install local transcription dependencies from requirements-transcription.txt.") from exc
        try:
            model_path = Path(settings.WHISPER_MODEL)
            if not model_path.is_dir():
                model_path = Path(download_model(
                    settings.WHISPER_MODEL, cache_dir=str(settings.WHISPER_DOWNLOAD_ROOT),
                    local_files_only=True,
                ))
            # Avoid tokenizers' remote fallback for incomplete local directories.
            for name in ("model.bin", "config.json", "tokenizer.json"):
                if not (model_path / name).is_file():
                    raise WhisperModelError(f"Local Whisper model is incomplete: missing {name}. Run download_whisper_model.")
            model = WhisperModel(
                str(model_path), device=settings.WHISPER_DEVICE,
                compute_type=settings.WHISPER_COMPUTE_TYPE,
                download_root=str(settings.WHISPER_DOWNLOAD_ROOT), local_files_only=True,
            )
            if not model.model.is_multilingual:
                raise WhisperModelError("Choose a multilingual Whisper model; English-only models cannot transcribe Bangla.")
        except WhisperModelError:
            raise
        except Exception as exc:
            logger.exception("Local Whisper model initialization failed.")
            raise WhisperModelError(
                "Unable to load the local Whisper model. Run 'python manage.py download_whisper_model' "
                "first, and check WHISPER_MODEL, WHISPER_DEVICE and WHISPER_COMPUTE_TYPE."
            ) from exc
        _model = model
        _model_pid = os.getpid()
        _model_config = config
        logger.info("Loaded local Whisper model in process %s.", _model_pid)
        return _model



def _read_waveform(path):
    """Read only the preprocessor's PCM WAV, avoiding optional codec decoding."""
    try:
        import numpy as np
    except ImportError as exc:
        raise WhisperDependencyError("Install requirements-transcription.txt, including NumPy.") from exc
    try:
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth(), audio.getcomptype()) != (1, 16000, 2, "NONE"):
                raise WhisperInferenceError("Whisper requires a processed mono 16 kHz PCM16 WAV.")
            count = audio.getnframes()
            pcm = audio.readframes(count)
            if count == 0 or len(pcm) != count * 2:
                raise WhisperInferenceError("Processed WAV is empty or truncated.")
        return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    except (OSError, wave.Error, EOFError) as exc:
        raise WhisperInferenceError("Processed WAV could not be read.") from exc

def transcribe_audio(path, *, speech_segments=None):
    """Transcribe multilingual audio without translation; retain original timestamps.

    speech_segments contains absolute seconds from Silero. clip_timestamps
    selects ranges on the original timeline without writing or concatenating files.
    Passing [] returns an empty result without loading Whisper.
    """
    path = Path(path)
    if not path.is_file():
        raise WhisperInferenceError("Processed audio file is missing.")
    started = time.monotonic()
    if speech_segments == []:
        return TranscriptionResult([], "", "", time.monotonic() - started)
    clips = None
    if speech_segments is not None:
        try:
            clips = []
            previous_end = 0
            for segment in speech_segments:
                start, end = float(segment["start"]), float(segment["end"])
                if not math.isfinite(start) or not math.isfinite(end) or start < previous_end or end <= start:
                    raise ValueError("Invalid VAD interval")
                clips.extend([start, end])
                previous_end = end
        except (KeyError, TypeError, ValueError) as exc:
            raise WhisperInferenceError("Invalid speech intervals supplied to Whisper.") from exc
    waveform = _read_waveform(path)
    with _model_lock:
        model = _get_model()
        try:
            options = {
                "task": "transcribe", "language": None, "multilingual": True,
                "vad_filter": False, "condition_on_previous_text": False,
            }
            if clips is not None:
                options["clip_timestamps"] = clips
            generated, info = model.transcribe(waveform, **options)
            segments = []
            previous_start = 0.0
            # Consume the generator inside the error boundary and model lock.
            for segment in generated:
                start, end = float(segment.start), float(segment.end)
                if not math.isfinite(start) or not math.isfinite(end) or start < previous_start or end < start:
                    raise ValueError("Whisper returned invalid timestamps")
                previous_start = start
                text = segment.text.strip()
                if not text:
                    continue
                segments.append({
                    "start": start, "end": end, "text": text,
                    # This backend may only expose file-level language, not per segment.
                    "detected_language": getattr(segment, "language", None) or "",
                })
            language = (info.language or "") if segments else ""
        except Exception as exc:
            logger.exception("Local Whisper inference failed.")
            raise WhisperInferenceError(
                "Local speech recognition failed. Check the audio and available memory; see server logs."
            ) from exc
    result = TranscriptionResult(
        segments, '\n'.join(item["text"] for item in segments),
        language, time.monotonic() - started,
    )
    logger.info("Local transcription completed: %d segments in %.3f seconds.", len(segments), result.processing_time)
    return result
