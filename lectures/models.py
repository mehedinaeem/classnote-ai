from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db import models
from django.utils import timezone


def lecture_audio_path(instance, filename):
    if not instance.pk:
        raise ValueError("Save the lecture before storing its audio.")
    return f"lectures/{instance.owner_id}/{instance.pk}/{uuid4().hex}{Path(filename).suffix.lower()}"


class Lecture(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        UPLOADED = "uploaded", "Uploaded"
        PROCESSING_AUDIO = "processing_audio", "Processing audio"
        TRANSCRIBING = "transcribing", "Transcribing"
        GENERATING_NOTES = "generating_notes", "Generating notes"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="lectures")
    course = models.ForeignKey("courses.Course", on_delete=models.CASCADE, related_name="lectures")
    title = models.CharField(max_length=200)
    lecture_date = models.DateField(default=timezone.localdate)
    description = models.TextField(blank=True)
    audio_file = models.FileField(upload_to=lecture_audio_path, max_length=255, blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    progress = models.PositiveSmallIntegerField(default=0, validators=[MaxValueValidator(100)])
    processing_message = models.CharField(max_length=255, blank=True)
    processing_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.CheckConstraint(condition=models.Q(progress__lte=100), name="lecture_progress_range")]

    def clean(self):
        super().clean()
        if self.course_id and self.owner_id and self.course.owner_id != self.owner_id:
            raise ValidationError({"course": "Choose one of your own courses."})

    def __str__(self):
        return self.title


class BackgroundJob(models.Model):
    """Private, durable work state. Celery carries only the job/lecture identifiers."""
    class Kind(models.TextChoices):
        LECTURE = "lecture", "Process lecture"
        NOTES = "notes", "Generate notes"
        STUDY = "study", "Generate study materials"
        CHAT = "chat", "Answer question"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    lecture = models.ForeignKey(Lecture, on_delete=models.CASCADE, related_name="jobs")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    progress = models.PositiveSmallIntegerField(default=0, validators=[MaxValueValidator(100)])
    message = models.CharField(max_length=255, default="Waiting for a local worker...")
    payload = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    run_token = models.UUIDField(null=True, editable=False)
    lease_until = models.DateTimeField(null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["lecture"], condition=models.Q(
                kind__in=["lecture", "notes"], status__in=["queued", "running"]), name="one_active_lecture_pipeline"),
            models.UniqueConstraint(fields=["lecture", "kind"], condition=models.Q(
                kind="study", status__in=["queued", "running"]), name="one_active_study_job"),
            models.CheckConstraint(condition=models.Q(progress__lte=100), name="job_progress_range"),
        ]
