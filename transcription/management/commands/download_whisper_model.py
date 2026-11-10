from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Explicitly download Whisper weights for subsequent offline inference."

    def handle(self, *args, **options):
        try:
            from faster_whisper.utils import download_model
            from pathlib import Path
            if Path(settings.WHISPER_MODEL).is_dir():
                self.stdout.write("WHISPER_MODEL points to an existing local directory; no download performed.")
                return
            path = download_model(
                settings.WHISPER_MODEL, cache_dir=str(settings.WHISPER_DOWNLOAD_ROOT)
            )
        except Exception as exc:
            raise CommandError("Model download failed. Check dependencies, model name, disk space and connectivity.") from exc
        self.stdout.write(self.style.SUCCESS(f"Model available locally at {path}"))
