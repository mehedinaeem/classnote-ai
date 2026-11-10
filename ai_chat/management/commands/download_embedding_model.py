from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Explicitly provision the local multilingual sentence-transformer."

    def handle(self, *args, **options):
        try:
            from huggingface_hub import snapshot_download
            path = snapshot_download(
                repo_id=settings.EMBEDDING_MODEL, revision=settings.EMBEDDING_REVISION,
                local_dir=str(settings.EMBEDDING_MODEL_PATH),
                allow_patterns=["*.json", "*.txt", "*.safetensors", "*.model"],
            )
            if not (Path(path) / "modules.json").is_file():
                raise ValueError("Missing sentence-transformer configuration")
        except Exception as exc:
            raise CommandError("Embedding model setup failed. Check dependencies, model name, connectivity and disk space.") from exc
        self.stdout.write(self.style.SUCCESS(f"Embedding model available locally: {path}"))
