"""Celery entry points. Services own all inference and persistence."""
from celery import shared_task
from django.db import IntegrityError, transaction

from lectures.models import BackgroundJob, Lecture
from lectures.services.background_jobs import JobClaim, LostClaim


def execute(job, task):
    if job.user_id != job.lecture.owner_id:
        BackgroundJob.objects.filter(pk=job.pk).update(status="failed", error="Lecture ownership changed.")
        return
    claim = JobClaim(job)
    if not claim.acquire():
        job.refresh_from_db()
        if job.status == "running":
            raise task.retry(countdown=30, max_retries=None)
        return job.result
    try:
        with claim.heartbeat(eager=task.request.is_eager):
            lecture = job.lecture
            job.refresh_from_db()
            if job.kind == "lecture":
                from transcription.services.pipeline import process_lecture as transcribe
                from notes.services.note_generator import generate_lecture_notes
                if not job.result.get("transcript_id"):
                    transcribe(lecture, _job_guard=claim, finish=False)
                lecture.refresh_from_db()
                if not job.result.get("note_id"):
                    generate_lecture_notes(lecture, _job_guard=claim)
            elif job.kind == "notes":
                from notes.services.note_generator import generate_lecture_notes
                if not job.result.get("note_id"):
                    generate_lecture_notes(lecture, _job_guard=claim)
            elif job.kind == "study":
                from notes.services.study_generator import generate_study_materials
                if not job.result.get("study_id"):
                    claim.stage(lecture.status, 20, "Generating quiz and flashcards...")
                    generate_study_materials(lecture, user=job.user, _job_guard=claim, **job.payload)
            elif job.kind == "chat":
                from ai_chat.services.rag_service import ask_lecture
                if not job.result.get("message_id"):
                    claim.stage(lecture.status, 20, "Searching the lecture and preparing an answer...")
                    ask_lecture(lecture, user=job.user, question=job.payload["question"], _job_guard=claim)
            claim.finish()
            job.refresh_from_db()
            return job.result
    except LostClaim:
        return None
    except Exception as exc:
        claim.fail(exc)
        raise


@shared_task(bind=True, acks_late=True, reject_on_worker_lost=True)
def process_lecture(self, lecture_id):
    """Process an owned lecture; repeated deliveries never duplicate saved output."""
    lecture = Lecture.objects.filter(pk=lecture_id).first()
    if lecture is None:
        return None
    job = BackgroundJob.objects.filter(pk=self.request.id, lecture=lecture, kind="lecture").first() if self.request.id else None
    if job is None:
        if lecture.status == "completed" and hasattr(lecture, "transcript") and hasattr(lecture, "note"):
            return None
        try:
            with transaction.atomic():
                job = BackgroundJob.objects.create(**({"id": self.request.id} if self.request.id else {}), lecture=lecture, user=lecture.owner, kind="lecture")
        except IntegrityError:
            return None
    return execute(job, self)


@shared_task(bind=True, acks_late=True, reject_on_worker_lost=True)
def run_job(self, job_id):
    job = BackgroundJob.objects.select_related("lecture", "user").filter(pk=job_id).first()
    return execute(job, self) if job else None
