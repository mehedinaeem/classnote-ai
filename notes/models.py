import math

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from .services.schema import validate_note_data


class LectureNote(models.Model):
    lecture = models.OneToOneField("lectures.Lecture", on_delete=models.CASCADE, related_name="note")
    data = models.JSONField(validators=[validate_note_data])
    transcript_fingerprint = models.CharField(max_length=64)
    model_name = models.CharField(max_length=200)
    processing_time = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Notes for lecture {self.lecture_id}"


class StudyMaterialSet(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        GENERATING = "generating", "Generating"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    lecture = models.OneToOneField("lectures.Lecture", on_delete=models.CASCADE, related_name="study_materials")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    source_fingerprint = models.CharField(max_length=64, blank=True)
    model_name = models.CharField(max_length=200, blank=True)
    quiz_count = models.PositiveSmallIntegerField(default=0)
    flashcard_count = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class QuizQuestion(models.Model):
    class Option(models.TextChoices):
        A = "A", "A"
        B = "B", "B"
        C = "C", "C"
        D = "D", "D"

    lecture = models.ForeignKey("lectures.Lecture", on_delete=models.CASCADE, related_name="quiz_questions")
    sequence = models.PositiveIntegerField()
    question = models.TextField()
    option_a = models.CharField(max_length=500)
    option_b = models.CharField(max_length=500)
    option_c = models.CharField(max_length=500)
    option_d = models.CharField(max_length=500)
    correct_option = models.CharField(max_length=1, choices=Option.choices)
    explanation = models.TextField()
    source_timestamp = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    source_excerpt = models.TextField()

    class Meta:
        ordering = ["sequence", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["lecture", "sequence"], name="unique_lecture_quiz_sequence"),
            models.CheckConstraint(
                condition=models.Q(source_timestamp__isnull=True) | models.Q(source_timestamp__gte=0),
                name="quiz_source_time_nonnegative",
            ),
        ]

    def __str__(self):
        return self.question


class Flashcard(models.Model):
    lecture = models.ForeignKey("lectures.Lecture", on_delete=models.CASCADE, related_name="flashcards")
    sequence = models.PositiveIntegerField()
    front = models.TextField()
    back = models.TextField()
    source_timestamp = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    source_excerpt = models.TextField()

    class Meta:
        ordering = ["sequence", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["lecture", "sequence"], name="unique_lecture_flashcard_sequence"),
            models.CheckConstraint(
                condition=models.Q(source_timestamp__isnull=True) | models.Q(source_timestamp__gte=0),
                name="flashcard_source_time_nonnegative",
            ),
        ]

    def __str__(self):
        return self.front


class ImportanceCandidate(models.Model):
    class Category(models.TextChoices):
        EXAM = "exam_important", "Exam important"
        HOMEWORK = "homework", "Homework / assignment"
        DEFINITION = "definition_emphasis", "Definition emphasis"
        GENERAL = "general_emphasis", "General emphasis"
        NONE = "none", "Not important"

    transcript_segment = models.ForeignKey(
        "transcription.TranscriptSegment", on_delete=models.CASCADE, related_name="importance_candidates")
    position = models.PositiveIntegerField()
    text = models.TextField()
    context = models.TextField()
    matched_keywords = models.JSONField()
    keyword_score = models.PositiveSmallIntegerField()
    llm_classification = models.CharField(max_length=24, choices=Category.choices)
    confidence = models.FloatField(validators=[MinValueValidator(0), MaxValueValidator(1)])
    final_category = models.CharField(max_length=24, choices=Category.choices)
    title = models.CharField(max_length=200, blank=True)
    evidence_quote = models.TextField(blank=True)
    classification_reason = models.TextField()
    decision_reason = models.TextField()
    model_name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["transcript_segment__sequence", "position"]
        constraints = [
            models.UniqueConstraint(fields=["transcript_segment", "position"], name="unique_importance_candidate_position"),
            models.CheckConstraint(condition=models.Q(confidence__gte=0, confidence__lte=1), name="importance_confidence_range"),
        ]

    def clean(self):
        super().clean()
        if self.confidence is not None and (
            not isinstance(self.confidence, (float, int))
            or not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1
        ):
            raise ValidationError({"confidence": "Confidence must be between 0 and 1."})
        if self.final_category != self.Category.NONE:
            if not self.evidence_quote or self.evidence_quote not in self.text:
                raise ValidationError({"evidence_quote": "Evidence must quote the candidate text exactly."})
            if not self.title or self.title not in self.context:
                raise ValidationError({"title": "Title must quote the supplied source context."})

    @property
    def confidence_percent(self):
        return round(self.confidence * 100)
