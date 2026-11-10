"""Local, bounded FFmpeg preprocessing. Originals are never modified."""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave

from django.conf import settings


class AudioProcessingError(Exception):
    """Base error for local audio processing."""


class AudioDependencyError(AudioProcessingError):
    """A required executable is missing or cannot be started."""


class AudioFileNotFoundError(AudioProcessingError):
    """The input file is absent or is not a regular file."""


class InvalidAudioError(AudioProcessingError):
    """The input or output does not contain readable audio."""


class AudioConversionError(AudioProcessingError):
    """FFmpeg conversion or output storage failed."""


class AudioProcessingTimeout(AudioProcessingError):
    """A local audio command exceeded its time limit."""


def _binary(name):
    configured = getattr(settings, f"{name.upper()}_BINARY", name)
    executable = shutil.which(configured)
    if not executable:
        raise AudioDependencyError(f"{name} is unavailable. Install FFmpeg or configure {name.upper()}_BINARY.")
    return executable


def _input_file(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise AudioFileNotFoundError(f"Audio file does not exist: {path}")
    return path


def _run(arguments, *, timeout, failure_type):
    try:
        return subprocess.run(
            arguments, check=True, capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioProcessingTimeout("Audio processing exceeded its time limit.") from exc
    except (FileNotFoundError, PermissionError) as exc:
        raise AudioDependencyError("The audio executable could not be started.") from exc
    except subprocess.CalledProcessError as exc:
        raise failure_type("The audio command failed. The file may be corrupt or unsupported.") from exc
    except OSError as exc:
        raise AudioConversionError("Unable to execute the audio command.") from exc


def get_audio_duration(path):
    """Return finite, positive audio duration in seconds using local ffprobe."""
    path = _input_file(path)
    result = _run([
        _binary("ffprobe"), "-v", "error", "-protocol_whitelist", "file,pipe",
        "-select_streams", "a:0", "-show_entries",
        "format=duration:stream=codec_type,duration", "-of", "json", str(path),
    ], timeout=settings.AUDIO_PROBE_TIMEOUT_SECONDS, failure_type=InvalidAudioError)
    try:
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams or streams[0].get("codec_type") != "audio":
            raise ValueError("No audio stream")
        raw_duration = streams[0].get("duration")
        if raw_duration in (None, "N/A"):
            raw_duration = data.get("format", {}).get("duration")
        duration = float(raw_duration)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Invalid duration")
    except (ValueError, TypeError, AttributeError, IndexError) as exc:
        raise InvalidAudioError("Could not determine a valid audio duration.") from exc
    return duration


def process_audio(input_path, output_path):
    """Create normalized mono 16 kHz PCM WAV atomically; return its Path.

    Existing output is replaced only after conversion and WAV validation succeed.
    Each run uses a separate temporary output in the destination directory.
    """
    source = _input_file(input_path)
    destination = Path(output_path).resolve()
    if source == destination:
        raise AudioConversionError("Processed output must be separate from the original.")
    ffmpeg = _binary("ffmpeg")
    temporary = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".audio-", suffix=".wav", dir=destination.parent, delete=False
        ) as output:
            temporary = Path(output.name)
        _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-xerror", "-protocol_whitelist", "file,pipe", "-i", str(source),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
            "-af", "aformat=channel_layouts=mono,loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            "-f", "wav", str(temporary),
        ], timeout=settings.AUDIO_PROCESS_TIMEOUT_SECONDS, failure_type=AudioConversionError)
        try:
            with wave.open(str(temporary), "rb") as audio:
                if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) != (1, 16000, 2):
                    raise InvalidAudioError("Processed audio has an unexpected WAV format.")
                if audio.getnframes() <= 0:
                    raise InvalidAudioError("Processed audio is empty.")
        except (wave.Error, EOFError) as exc:
            raise InvalidAudioError("FFmpeg did not produce a valid WAV file.") from exc
        os.replace(temporary, destination)
    except OSError as exc:
        raise AudioConversionError("Unable to read audio or save the processed file.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def process_lecture_audio(lecture):
    """Process a saved lecture and update duration only after success.

    The caller is responsible for authorization before passing a lecture.
    This service supports the project's local filesystem media storage.
    """
    if not lecture.pk or not lecture.owner_id or not lecture.audio_file:
        raise AudioFileNotFoundError("A saved lecture with uploaded audio is required.")
    try:
        source = lecture.audio_file.path
    except NotImplementedError as exc:
        raise AudioConversionError("Audio preprocessing requires local file storage.") from exc
    output = (
        Path(settings.MEDIA_ROOT) / "lectures" / str(lecture.owner_id)
        / str(lecture.pk) / "processed" / "audio.wav"
    )
    output = process_audio(source, output)
    duration = get_audio_duration(output)
    # The existing database field stores whole seconds; round up partial seconds.
    lecture.duration_seconds = math.ceil(duration)
    lecture.save(update_fields=["duration_seconds", "updated_at"])
    return output
