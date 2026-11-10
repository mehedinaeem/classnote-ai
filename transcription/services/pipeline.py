"""Local transcription service used by the background worker."""
from decimal import Decimal
import logging

from django.db import transaction
from django.utils import timezone

from lectures.models import Lecture
from lectures.services.audio_processor import AudioProcessingError, process_lecture_audio
from transcription.models import Transcript, TranscriptSegment
from .vad_service import VADError, get_speech_segments
from .whisper_service import WhisperError, transcribe_audio

logger = logging.getLogger(__name__)


class LectureProcessingError(Exception):
    pass


class ProcessingInProgress(LectureProcessingError):
    pass


def process_lecture(lecture, *, _job_guard=None, finish=True):
    """Caller must first authorize ownership. Claim work with one atomic update."""
    claimed = True if _job_guard else Lecture.objects.filter(
        pk=lecture.pk, owner_id=lecture.owner_id,
        status__in=[Lecture.Status.DRAFT, Lecture.Status.UPLOADED, Lecture.Status.FAILED, Lecture.Status.COMPLETED],
    ).update(status=Lecture.Status.PROCESSING_AUDIO, processing_error="", updated_at=timezone.now())
    if not claimed:
        raise ProcessingInProgress("This lecture is already being processed.")
    try:
        if _job_guard:
            _job_guard.stage("processing_audio", 15, "Processing audio...")
        processed = process_lecture_audio(lecture)
        if _job_guard:
            _job_guard.stage("processing_audio", 40, "Audio processed. Detecting speech...")
        speech = get_speech_segments(processed)
        if not _job_guard:
            Lecture.objects.filter(pk=lecture.pk).update(status=Lecture.Status.TRANSCRIBING, updated_at=timezone.now())
        if _job_guard:
            _job_guard.stage("transcribing", 55, "Transcribing lecture...")
        result = transcribe_audio(processed, speech_segments=speech)
        segments = []
        for index, item in enumerate(result.segments, start=1):
            segment = TranscriptSegment(
                lecture=lecture, sequence=index,
                start_time=Decimal(str(item["start"])).quantize(Decimal("0.001")),
                end_time=Decimal(str(item["end"])).quantize(Decimal("0.001")),
                text=item["text"], detected_language=item.get("detected_language", ""),
            )
            segment.full_clean(validate_unique=False, validate_constraints=False)
            segments.append(segment)
        with transaction.atomic():
            if _job_guard:
                _job_guard.check()
            # Retain the previous successful transcript until the replacement is ready.
            TranscriptSegment.objects.filter(lecture=lecture).delete()
            TranscriptSegment.objects.bulk_create(segments)
            transcript, _ = Transcript.objects.update_or_create(
                lecture=lecture, defaults={
                    "full_text": result.full_text, "detected_language": result.detected_language,
                    "processing_time": result.processing_time,
                },
            )
            if _job_guard:
                _job_guard.checkpoint(transcript_id=transcript.pk)
                _job_guard.stage("completed" if finish else "generating_notes", 100 if finish else 80,
                                 "Transcript ready." if finish else "Generating academic notes...")
            else:
                Lecture.objects.filter(pk=lecture.pk).update(
                    status=Lecture.Status.COMPLETED, progress=100, processing_error="", updated_at=timezone.now())
    except Exception as exc:
        if _job_guard:
            _job_guard.check()
        logger.exception("Processing failed for lecture %s.", lecture.pk)
        message = str(exc) if isinstance(exc, (AudioProcessingError, VADError, WhisperError)) else (
            "An unexpected processing error occurred. Check the server logs and retry."
        )
        if not _job_guard:
            Lecture.objects.filter(pk=lecture.pk).update(
                status=Lecture.Status.FAILED, processing_error=message[:2000], updated_at=timezone.now())
        raise LectureProcessingError(message) from exc
    lecture.refresh_from_db()
    return transcript
