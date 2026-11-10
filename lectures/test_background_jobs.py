"""Worker pipeline tests never download models or contact Redis/Ollama."""
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from celery.exceptions import Retry
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from config.test_utils import enable_eager
from courses.models import Course
from lectures.models import BackgroundJob, Lecture
from lectures.services.background_jobs import JobClaim, JobError, LostClaim, enqueue, publish
from lectures.tasks import process_lecture, run_job
from notes.models import LectureNote
from notes.services.interfaces import LLMServiceError
from notes.services.schema import empty_note_data
from transcription.models import Transcript, TranscriptSegment
from transcription.services.whisper_service import TranscriptionResult


class BackgroundProcessingTests(TestCase):
    def setUp(self):
        enable_eager(self)
        self.owner = get_user_model().objects.create_user(username="worker-owner")
        self.other = get_user_model().objects.create_user(username="worker-other")
        course = Course.objects.create(owner=self.owner, course_code="ML", course_name="Machine learning")
        self.lecture = Lecture.objects.create(owner=self.owner, course=course, title="Kernel methods", status="uploaded", progress=10)
        self.client.force_login(self.owner)
        self.url = reverse("lectures:process", args=[self.lecture.pk])
        self.status_url = reverse("lectures:status", args=[self.lecture.pk])
        self.stages = []
        self.result = TranscriptionResult(
            segments=[{"start": 4, "end": 12, "text": "আজকে kernel trick নিয়ে আলোচনা।", "detected_language": "bn"}],
            full_text="আজকে kernel trick নিয়ে আলোচনা।", detected_language="bn", processing_time=1.0)
        def record(value):
            self.lecture.refresh_from_db()
            self.stages.append((self.lecture.status, self.lecture.progress))
            return value
        self.mocks = {}
        for name, target, effect in [
            ("audio", "transcription.services.pipeline.process_lecture_audio", lambda lecture: record(Path("/tmp/audio.wav"))),
            ("vad", "transcription.services.pipeline.get_speech_segments", lambda path: record([{"start": 4, "end": 12}])),
            ("whisper", "transcription.services.pipeline.transcribe_audio", lambda *args, **kwargs: record(self.result)),
            ("notes", "notes.services.note_generator.generate_structured_notes", lambda *args, **kwargs: record(empty_note_data())),
        ]:
            patcher = patch(target, side_effect=effect)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def submit(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        return BackgroundJob.objects.latest("created_at")

    def test_eager_pipeline_stages_progress_and_atomic_output(self):
        job = self.submit()
        self.lecture.refresh_from_db()
        self.assertEqual(self.stages, [("processing_audio", 15), ("processing_audio", 40), ("transcribing", 55), ("generating_notes", 80)])
        self.assertEqual((job.status, job.progress), ("completed", 100))
        self.assertEqual((self.lecture.status, self.lecture.progress), ("completed", 100))
        self.assertEqual(Transcript.objects.get().full_text, self.result.full_text)
        self.assertEqual(TranscriptSegment.objects.get().start_time, Decimal("4"))
        self.assertEqual(LectureNote.objects.count(), 1)
        self.assertEqual(self.client.get(self.status_url).json(), {"status": "completed", "progress": 100, "message": "Lecture ready."})

    def test_repeated_delivery_and_explicit_regeneration_do_not_duplicate(self):
        job = self.submit()
        process_lecture.apply(args=[self.lecture.pk], task_id=str(job.pk))
        process_lecture.apply(args=[self.lecture.pk])
        self.mocks["whisper"].assert_called_once()
        self.submit()  # User explicitly requests a new generation.
        self.assertEqual(Transcript.objects.count(), 1)
        self.assertEqual(TranscriptSegment.objects.count(), 1)
        self.assertEqual(LectureNote.objects.count(), 1)
        self.assertEqual(self.mocks["whisper"].call_count, 2)

    def test_no_model_inference_in_web_request_and_publish_after_commit(self):
        # A normal Django client does not execute on_commit while TestCase holds its transaction.
        client = Client()
        client.force_login(self.owner)
        with patch("lectures.tasks.process_lecture.apply_async") as send:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                response = client.post(self.url)
                self.assertEqual(response.status_code, 302)
                send.assert_not_called()
                for mocked in self.mocks.values():
                    mocked.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            job = BackgroundJob.objects.get()
            send.assert_called_once_with(args=[self.lecture.pk], task_id=str(job.pk), retry=False)
            self.assertEqual(job.status, "queued")
            self.assertContains(client.get(reverse("lectures:detail", args=[self.lecture.pk])), 'data-active="true"')

    def test_whisper_failure_retains_previous_transcript_and_marks_failed(self):
        old = Transcript.objects.create(lecture=self.lecture, full_text="Previous lecture", processing_time=1)
        self.mocks["whisper"].side_effect = RuntimeError("broken model")
        with self.assertLogs("transcription.services.pipeline", level="ERROR"):
            job = self.submit()
        old.refresh_from_db()
        self.lecture.refresh_from_db()
        self.assertEqual(old.full_text, "Previous lecture")
        self.assertEqual((job.status, self.lecture.status), ("failed", "failed"))
        self.assertEqual(self.lecture.progress, 55)
        self.assertTrue(self.lecture.processing_error)
        self.mocks["notes"].assert_not_called()

    def test_note_failure_keeps_transcript_and_old_note_and_supports_retry(self):
        old = LectureNote.objects.create(lecture=self.lecture, data=empty_note_data(), transcript_fingerprint="a" * 64, model_name="old", processing_time=1)
        self.mocks["notes"].side_effect = LLMServiceError("Local Ollama not connected")
        with self.assertLogs("notes.services.note_generator", level="ERROR"):
            job = self.submit()
        self.lecture.refresh_from_db()
        self.assertEqual((job.status, self.lecture.status, self.lecture.progress), ("failed", "failed", 80))
        self.assertIn("not connected", self.lecture.processing_error)
        old.refresh_from_db()
        self.assertEqual(old.model_name, "old")
        self.assertEqual(Transcript.objects.count(), 1)
        self.mocks["notes"].side_effect = lambda *a, **kw: empty_note_data()
        self.submit()
        self.assertEqual(LectureNote.objects.count(), 1)
        self.assertEqual(TranscriptSegment.objects.count(), 1)

    def test_expired_worker_resumes_from_saved_transcript_checkpoint(self):
        job = BackgroundJob.objects.create(lecture=self.lecture, user=self.owner, kind="lecture")
        claim = JobClaim(job)
        self.assertTrue(claim.acquire())
        from transcription.services.pipeline import process_lecture as transcribe
        transcribe(self.lecture, _job_guard=claim, finish=False)
        BackgroundJob.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        process_lecture.apply(args=[self.lecture.pk], task_id=str(job.pk))
        self.mocks["whisper"].assert_called_once()
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        with self.assertRaises(LostClaim):
            claim.check()

    def test_live_duplicate_task_cannot_take_claim(self):
        job = BackgroundJob.objects.create(lecture=self.lecture, user=self.owner, kind="lecture")
        claim = JobClaim(job)
        self.assertTrue(claim.acquire())
        with patch.object(process_lecture, "retry", side_effect=Retry) as retry:
            process_lecture.apply(args=[self.lecture.pk], task_id=str(job.pk))
        retry.assert_called_once()
        self.mocks["audio"].assert_not_called()
        self.assertFalse(JobClaim(job).acquire())

    def test_duplicate_submission_and_ownership_are_checked_before_enqueue(self):
        with self.assertRaises(PermissionDenied):
            enqueue(self.lecture, user=self.other)
        with patch("lectures.services.background_jobs.publish"):
            with self.captureOnCommitCallbacks(execute=True):
                enqueue(self.lecture, user=self.owner)
            with self.assertRaises(JobError):
                enqueue(self.lecture, user=self.owner)
        self.assertEqual(BackgroundJob.objects.count(), 1)

    def test_broker_unavailable_marks_failure_without_inference(self):
        with patch("lectures.tasks.process_lecture.apply_async", side_effect=ConnectionError("Redis offline")):
            with self.assertLogs("lectures.services.background_jobs", level="ERROR"):
                job = self.submit()
        self.lecture.refresh_from_db()
        self.assertEqual((job.status, self.lecture.status), ("failed", "failed"))
        self.assertIn("Redis", self.lecture.processing_error)
        self.mocks["audio"].assert_not_called()

    def test_status_endpoints_are_private_owner_only_and_get_only(self):
        job = self.submit()
        url = reverse("lectures:job_status", args=[self.lecture.pk, job.pk])
        self.assertEqual(self.client.get(url)["Cache-Control"], "private, no-store")
        self.assertEqual(self.client.post(self.status_url).status_code, 405)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.status_url).status_code, 404)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.logout()
        self.assertRedirects(self.client.get(self.status_url), "/login/?next=" + self.status_url)
        self.assertRedirects(self.client.get(url), "/login/?next=" + url)

    def test_silent_lecture_produces_empty_valid_notes_without_llm(self):
        self.result = TranscriptionResult(segments=[], full_text="", detected_language="", processing_time=1)
        # Empty speech should complete without hallucinating content.
        job = self.submit()
        self.assertEqual(job.status, "completed")
        self.assertEqual(LectureNote.objects.get().data, empty_note_data())
        self.mocks["notes"].assert_not_called()

    def test_progress_limits_enforced_by_database(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Lecture.objects.filter(pk=self.lecture.pk).update(progress=101)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            BackgroundJob.objects.create(lecture=self.lecture, user=self.owner, kind="chat", progress=101)
