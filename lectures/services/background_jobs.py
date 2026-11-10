"""Durable job submission and fenced, renewable worker claims."""
from contextlib import contextmanager
from datetime import timedelta
import logging
from threading import Event, Thread
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

from lectures.models import BackgroundJob, Lecture

logger = logging.getLogger(__name__)
ACTIVE = [BackgroundJob.Status.QUEUED, BackgroundJob.Status.RUNNING]
CORE = [BackgroundJob.Kind.LECTURE, BackgroundJob.Kind.NOTES]


class JobError(Exception):
    pass


class LostClaim(JobError):
    pass


def enqueue(lecture, *, user, kind="lecture", payload=None):
    if lecture.owner_id != user.pk:
        raise PermissionDenied
    if kind not in BackgroundJob.Kind.values:
        raise JobError("Unknown background action.")
    if kind != BackgroundJob.Kind.LECTURE and not lecture.transcript_segments.exists():
        raise JobError("Transcribe the lecture before requesting this action.")
    try:
        with transaction.atomic():
            if kind in CORE and lecture.jobs.filter(kind__in=CORE, status__in=ACTIVE).exists():
                raise JobError("This lecture already has a queued or running processing job.")
            job = BackgroundJob.objects.create(lecture=lecture, user=user, kind=kind, payload=payload or {})
            if kind in CORE:
                Lecture.objects.filter(pk=lecture.pk).update(
                    status=Lecture.Status.UPLOADED if kind == "lecture" else Lecture.Status.GENERATING_NOTES,
                    progress=10 if kind == "lecture" else 80,
                    processing_message="Queued. Waiting for a local worker...", processing_error="", updated_at=timezone.now(),
                )
            transaction.on_commit(lambda: publish(job.pk))
    except IntegrityError as exc:
        raise JobError("This action already has a queued or running job.") from exc
    return job


def publish(job_id):
    from lectures.tasks import process_lecture, run_job
    job = BackgroundJob.objects.get(pk=job_id)
    try:
        task = process_lecture if job.kind == "lecture" else run_job
        args = [job.lecture_id] if job.kind == "lecture" else [str(job.pk)]
        task.apply_async(args=args, task_id=str(job.pk), retry=False)
    except Exception:
        logger.exception("Could not publish job %s to the local worker.", job.pk)
        message = "Could not queue work. Start local Redis and the Celery worker, then retry."
        with transaction.atomic():
            changed = BackgroundJob.objects.filter(pk=job.pk, status="queued").update(
                status="failed", error=message, message=message, updated_at=timezone.now())
            if changed and job.kind in CORE:
                Lecture.objects.filter(pk=job.lecture_id).update(
                    status="failed", processing_error=message, processing_message=message, updated_at=timezone.now())


class JobClaim:
    def __init__(self, job):
        self.job = job
        self.token = uuid4()

    def matching(self):
        return BackgroundJob.objects.filter(pk=self.job.pk, run_token=self.token, status="running",
                                            lecture__owner_id=self.job.user_id)

    def acquire(self):
        now = timezone.now()
        return BackgroundJob.objects.filter(pk=self.job.pk).filter(
            models_claimable(now)
        ).update(status="running", run_token=self.token, lease_until=now + timedelta(minutes=3), updated_at=now)

    def check(self):
        # UPDATE takes a write lock even on SQLite; retain it within the caller's atomic save.
        if not self.matching().update(lease_until=timezone.now() + timedelta(minutes=3)):
            raise LostClaim("This worker no longer owns the processing job.")

    def checkpoint(self, **values):
        self.check()
        self.job.refresh_from_db(fields=["result"])
        self.job.result.update(values)
        self.matching().update(result=self.job.result, updated_at=timezone.now())

    def stage(self, status, progress, message):
        with transaction.atomic():
            self.check()
            self.matching().update(progress=progress, message=message, updated_at=timezone.now())
            if self.job.kind in CORE:
                Lecture.objects.filter(pk=self.job.lecture_id).update(
                    status=status, progress=progress, processing_message=message, processing_error="", updated_at=timezone.now())

    def finish(self):
        with transaction.atomic():
            self.check()
            self.matching().update(status="completed", progress=100, message="Completed.", lease_until=None, updated_at=timezone.now())
            if self.job.kind in CORE:
                Lecture.objects.filter(pk=self.job.lecture_id).update(
                    status="completed", progress=100, processing_message="Lecture ready.", processing_error="", updated_at=timezone.now())

    def fail(self, exc):
        message = str(exc)[:2000] or "Processing failed. Check worker logs and retry."
        with transaction.atomic():
            changed = self.matching().update(status="failed", error=message, message=message[:255], lease_until=None, updated_at=timezone.now())
            if changed and self.job.kind in CORE:
                Lecture.objects.filter(pk=self.job.lecture_id).update(
                    status="failed", processing_error=message, processing_message=message[:255], updated_at=timezone.now())

    @contextmanager
    def heartbeat(self, *, eager=False):
        stopped = Event()
        def renew():
            while not stopped.wait(30):
                close_old_connections()
                try:
                    self.check()
                except LostClaim:
                    return
                except Exception:
                    logger.exception("Could not renew job %s lease.", self.job.pk)
                finally:
                    close_old_connections()
        thread = None if eager else Thread(target=renew, daemon=True)
        if thread:
            thread.start()
        try:
            yield
        finally:
            stopped.set()
            if thread:
                thread.join(timeout=2)


def models_claimable(now):
    from django.db.models import Q
    return Q(status="queued") | Q(status="running", lease_until__lt=now)
