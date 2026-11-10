from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models


class Transcript(models.Model):
    lecture = models.OneToOneField("lectures.Lecture", on_delete=models.CASCADE, related_name="transcript")
    full_text = models.TextField(blank=True)
    detected_language = models.CharField(max_length=20, blank=True)
    processing_time = models.FloatField(validators=[MinValueValidator(0)])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(processing_time__gte=0), name="transcript_processing_time_nonnegative"),
        ]

    def __str__(self):
        return f"Transcript for lecture {self.lecture_id}"


class TranscriptSegment(models.Model):
    lecture = models.ForeignKey("lectures.Lecture", on_delete=models.CASCADE, related_name="transcript_segments")
    sequence = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    start_time = models.DecimalField(max_digits=12, decimal_places=3, validators=[MinValueValidator(0)])
    end_time = models.DecimalField(max_digits=12, decimal_places=3, validators=[MinValueValidator(0)])
    text = models.TextField()
    detected_language = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["lecture", "sequence"], name="unique_lecture_segment_sequence"),
            models.CheckConstraint(condition=models.Q(sequence__gte=1), name="segment_sequence_positive"),
            models.CheckConstraint(condition=models.Q(start_time__gte=0), name="segment_start_nonnegative"),
            models.CheckConstraint(condition=models.Q(end_time__gte=models.F("start_time")), name="segment_end_after_start"),
        ]

    def clean(self):
        super().clean()
        if self.start_time is not None and self.end_time is not None and self.end_time < self.start_time:
            raise ValidationError({"end_time": "End time must be at or after start time."})

    def __str__(self):
        return f"Lecture {self.lecture_id}, segment {self.sequence}"
