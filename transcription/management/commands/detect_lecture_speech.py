import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from lectures.models import Lecture
from transcription.services.vad_service import VADError, get_speech_segments


class Command(BaseCommand):
    help = "Print local Silero speech timestamps for a lecture's processed WAV."

    def add_arguments(self, parser):
        parser.add_argument("lecture_id", type=int)

    def handle(self, *args, **options):
        try:
            lecture = Lecture.objects.get(pk=options["lecture_id"])
        except Lecture.DoesNotExist as exc:
            raise CommandError("Lecture not found.") from exc
        path = (
            Path(settings.MEDIA_ROOT) / "lectures" / str(lecture.owner_id)
            / str(lecture.pk) / "processed" / "audio.wav"
        )
        try:
            segments = get_speech_segments(path)
        except VADError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(segments, indent=2))
