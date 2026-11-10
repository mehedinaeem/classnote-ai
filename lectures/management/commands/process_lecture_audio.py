from django.core.management.base import BaseCommand, CommandError

from lectures.models import Lecture
from lectures.services.audio_processor import AudioProcessingError, process_lecture_audio


class Command(BaseCommand):
    help = "Preprocess one saved lecture locally; no transcription is performed."

    def add_arguments(self, parser):
        parser.add_argument("lecture_id", type=int)

    def handle(self, *args, **options):
        try:
            lecture = Lecture.objects.get(pk=options["lecture_id"])
        except Lecture.DoesNotExist as exc:
            raise CommandError("Lecture not found.") from exc
        try:
            output = process_lecture_audio(lecture)
        except AudioProcessingError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"Processed audio: {output} ({lecture.duration_seconds} seconds)"
        ))
