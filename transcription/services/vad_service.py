"""Lazy, local Silero VAD for the preprocessor's mono 16 kHz PCM WAV files."""
import importlib
import logging
import math
import os
from pathlib import Path
import threading
import time
import wave

from django.conf import settings

logger = logging.getLogger(__name__)
SAMPLE_RATE = 16000

_runtime = None
_runtime_pid = None
_runtime_lock = threading.RLock()


class VADError(Exception):
    """Base VAD service error."""


class VADConfigurationError(VADError):
    """Invalid detection settings."""


class VADAudioError(VADError):
    """Missing, unreadable, or unsupported processed audio."""


class VADDependencyError(VADError):
    """Silero or PyTorch is missing or cannot be imported."""


class VADModelError(VADError):
    """The packaged model could not be loaded."""


class VADInferenceError(VADError):
    """Inference or timestamp validation failed."""


def _after_fork():
    # A fork must not reuse a parent's stateful model or an inherited held lock.
    global _runtime, _runtime_pid, _runtime_lock
    _runtime = None
    _runtime_pid = None
    _runtime_lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _get_runtime():
    global _runtime, _runtime_pid
    with _runtime_lock:
        if _runtime is not None and _runtime_pid == os.getpid():
            return _runtime
        try:
            torch = importlib.import_module("torch")
            silero = importlib.import_module("silero_vad")
        except (ImportError, OSError) as exc:
            logger.exception("Local VAD dependencies are unavailable.")
            raise VADDependencyError(
                "Install the VAD dependencies from requirements-vad.txt, including CPU PyTorch."
            ) from exc
        try:
            model = silero.load_silero_vad(onnx=False)
        except Exception as exc:
            logger.exception("Could not load the packaged Silero VAD model.")
            raise VADModelError("Unable to load the local Silero VAD model.") from exc
        _runtime = (torch, model, silero.get_speech_timestamps)
        _runtime_pid = os.getpid()
        logger.info("Loaded local Silero VAD model for process %s.", _runtime_pid)
        return _runtime


def _options():
    threshold = settings.VAD_THRESHOLD
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold < 1:
        raise VADConfigurationError("VAD_THRESHOLD must be greater than 0 and less than 1.")
    options = {"threshold": threshold}
    for setting, argument in (
        ("VAD_MIN_SPEECH_MS", "min_speech_duration_ms"),
        ("VAD_MIN_SILENCE_MS", "min_silence_duration_ms"),
        ("VAD_SPEECH_PAD_MS", "speech_pad_ms"),
    ):
        value = getattr(settings, setting)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise VADConfigurationError(f"{setting} must be a nonnegative integer.")
        options[argument] = value
    return options


def _read_pcm(path):
    try:
        with wave.open(str(Path(path)), "rb") as audio:
            if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth(), audio.getcomptype()) != (1, SAMPLE_RATE, 2, "NONE"):
                raise VADAudioError("VAD requires a mono 16 kHz 16-bit PCM WAV. Run audio preprocessing first.")
            count = audio.getnframes()
            pcm = audio.readframes(count)
            if count == 0 or len(pcm) != count * 2:
                raise VADAudioError("Processed audio is empty or truncated.")
    except (OSError, wave.Error, EOFError) as exc:
        logger.warning("VAD input could not be read: %s", type(exc).__name__)
        raise VADAudioError("Processed WAV is missing or unreadable.") from exc
    return pcm, count


def normalize_timestamps(timestamps, duration_seconds):
    """Validate seconds, clamp to the file, sort and merge overlapping ranges."""
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise VADInferenceError("Audio duration must be finite and positive.")
    segments = []
    try:
        for timestamp in timestamps:
            start, end = float(timestamp["start"]), float(timestamp["end"])
            if not math.isfinite(start) or not math.isfinite(end) or end < start:
                raise ValueError("Invalid speech range")
            start = max(0.0, min(start, duration_seconds))
            end = max(0.0, min(end, duration_seconds))
            if end > start:
                segments.append({"start": start, "end": end})
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        raise VADInferenceError("Silero returned invalid speech timestamps.") from exc
    merged = []
    for segment in sorted(segments, key=lambda item: (item["start"], item["end"])):
        if merged and segment["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], segment["end"])
        else:
            merged.append(segment)
    return merged


def get_speech_segments(path):
    """Return [{'start': seconds, 'end': seconds}, ...]; silence returns [].

    No files are cut or changed. Callers must authorize access to the input.
    Inference is serialized to protect the once-per-process model's state.
    """
    options = _options()
    pcm, count = _read_pcm(path)
    duration = count / SAMPLE_RATE
    started = time.monotonic()
    logger.info("Starting local VAD on %.3f seconds of processed audio.", duration)
    with _runtime_lock:
        torch, model, get_timestamps = _get_runtime()
        try:
            # Read PCM directly: no numpy/torchaudio/codec dependency or resampling.
            samples = torch.frombuffer(bytearray(pcm), dtype=torch.int16).to(dtype=torch.float32)
            samples.div_(32768.0)
            with torch.inference_mode():
                timestamps = get_timestamps(
                    samples, model, sampling_rate=SAMPLE_RATE,
                    return_seconds=True, time_resolution=3, **options,
                )
            segments = normalize_timestamps(timestamps, duration)
        except VADInferenceError:
            logger.exception("VAD timestamp validation failed.")
            raise
        except Exception as exc:
            logger.exception("Local Silero VAD inference failed.")
            raise VADInferenceError("Could not detect speech in the processed audio.") from exc
    logger.info(
        "Local VAD completed: %d segments, %.3f speech seconds, %.3f elapsed seconds.",
        len(segments), sum(item["end"] - item["start"] for item in segments),
        time.monotonic() - started,
    )
    return segments
