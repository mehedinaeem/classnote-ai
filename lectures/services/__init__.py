"""Local file inspection and upload persistence; no transcription."""
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from ..models import Lecture

logger = logging.getLogger(__name__)


def inspect_audio(upload):
    """Inspect actual container/audio streams, ignoring client MIME claims."""
    containers = {
        ".mp3": {"mp3"}, ".wav": {"wav"},
        ".m4a": {"mov", "mp4", "m4a"}, ".mp4": {"mov", "mp4", "m4a"},
        ".webm": {"matroska", "webm"}, ".ogg": {"ogg"},
    }
    try:
        with tempfile.NamedTemporaryFile(suffix=Path(upload.name).suffix.lower()) as temporary:
            for chunk in upload.chunks():
                temporary.write(chunk)
            temporary.flush()
            result = subprocess.run(
                [settings.FFPROBE_BINARY, "-v", "error", "-show_entries",
                 "format=format_name:stream=codec_type", "-of", "json", temporary.name],
                capture_output=True, text=True, timeout=20, check=True,
            )
        data = json.loads(result.stdout)
        formats = set(data.get("format", {}).get("format_name", "").split(","))
        has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
        if not has_audio or not formats.intersection(containers[Path(upload.name).suffix.lower()]):
            raise ValidationError("The file must contain audio and match its extension.")
    except FileNotFoundError as exc:
        raise ValidationError("Audio validation is unavailable. Install FFmpeg with ffprobe.") from exc
    except (subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise ValidationError("The audio file could not be read. Choose a valid supported file.") from exc
    finally:
        upload.seek(0)


def create_uploaded_lecture(*, form, owner):
    lecture = form.save(commit=False)
    upload = form.cleaned_data["audio_file"]
    lecture.owner = owner
    lecture.original_filename = Path(upload.name).name
    lecture.audio_file = ""
    lecture.status = Lecture.Status.UPLOADED
    lecture.progress = 10
    lecture.processing_message = "Audio uploaded. Ready to process."
    lecture.full_clean()
    try:
        with transaction.atomic():
            lecture.save()
            lecture.audio_file.save(upload.name, upload, save=False)
            lecture.save(update_fields=["audio_file", "updated_at"])
    except Exception:
        # Storage is not transactional: remove a newly written file on DB failure.
        if lecture.audio_file.name:
            try:
                lecture.audio_file.delete(save=False)
            except OSError:
                logger.exception("Could not clean up failed lecture upload.")
        raise
    return lecture
