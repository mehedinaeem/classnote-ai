from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError

AUDIO_EXTENSIONS = ("mp3", "wav", "m4a", "mp4", "webm", "ogg")


def validate_audio_upload(upload):
    if Path(upload.name).suffix.lower().lstrip(".") not in AUDIO_EXTENSIONS:
        raise ValidationError("Upload an MP3, WAV, M4A, MP4, WebM, or OGG file.")
    if not upload.size:
        raise ValidationError("The audio file is empty.")
    if upload.size > settings.MAX_AUDIO_UPLOAD_BYTES:
        raise ValidationError(
            f"The file must be no larger than {settings.MAX_AUDIO_UPLOAD_BYTES // (1024 * 1024)} MB."
        )
