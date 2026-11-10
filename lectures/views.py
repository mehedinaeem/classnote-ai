import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, JsonResponse
from io import BytesIO
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_safe, require_POST

from ai_chat.forms import QuestionForm
from ai_chat.models import ChatMessage
from ai_chat.services.vector_store import source_fingerprint, transcript_snapshot
from notes.services.pdf_export import PDFExportError, render_notes_pdf
from notes.models import ImportanceCandidate, LectureNote, StudyMaterialSet
from notes.forms import StudyGenerationForm
from notes.services.study_generator import source_snapshot, source_hash
from notes.services.chunking import fingerprint, transcript_source
from transcription.models import Transcript
from .services.background_jobs import ACTIVE, CORE, JobError, enqueue

from .forms import LectureForm, RecordingForm
from .models import BackgroundJob, Lecture
from .services import create_uploaded_lecture
from .services.audio_response import audio_response


@login_required
@require_safe
def lecture_list(request):
    lectures = Lecture.objects.filter(owner=request.user).select_related("course")
    page = Paginator(lectures, 12).get_page(request.GET.get("page"))
    return render(request, "lectures/lecture_list.html", {"page_obj": page})


@login_required
@require_http_methods(["GET", "POST"])
def lecture_create(request):
    form = LectureForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        user=request.user,
    )
    if request.method == "POST" and form.is_valid():
        lecture = create_uploaded_lecture(form=form, owner=request.user)
        messages.success(request, "Lecture uploaded. Select Process Lecture to transcribe it locally.")
        return redirect("lectures:detail", pk=lecture.pk)
    return render(request, "lectures/lecture_form.html", {
        "form": form,
        "has_courses": form.fields["course"].queryset.exists(),
        "max_upload_mb": settings.MAX_AUDIO_UPLOAD_BYTES // (1024 * 1024),
    })


@login_required
@require_safe
def lecture_detail(request, pk):
    lecture = get_object_or_404(
        Lecture.objects.select_related("course"), pk=pk, owner=request.user
    )
    segments = list(lecture.transcript_segments.all())
    note = LectureNote.objects.filter(lecture=lecture).first()
    data = note.data if note else {}
    chat_history = list(ChatMessage.objects.filter(
        lecture=lecture, user=request.user,
    ).order_by("-created_at", "-pk")[:20])
    current_source = source_fingerprint(transcript_snapshot(lecture, segments))
    for message in chat_history:
        message.is_stale = message.transcript_fingerprint != current_source
    chat_history.reverse()
    study = StudyMaterialSet.objects.filter(lecture=lecture).first()
    quiz = list(lecture.quiz_questions.values(
        "question", "option_a", "option_b", "option_c", "option_d",
        "correct_option", "explanation", "source_timestamp", "source_excerpt",
    ))
    cards = list(lecture.flashcards.values("front", "back", "source_timestamp", "source_excerpt"))
    candidates = list(ImportanceCandidate.objects.filter(
        transcript_segment__lecture=lecture).select_related("transcript_segment"))
    return render(request, "lectures/lecture_detail.html", {
        "lecture": lecture,
        "study_materials": study,
        "study_form": StudyGenerationForm(initial={
            "question_count": study.quiz_count if study and study.quiz_count else settings.QUIZ_QUESTION_COUNT,
            "flashcard_count": study.flashcard_count if study and study.flashcard_count else settings.FLASHCARD_COUNT,
        }),
        "quiz_data": quiz,
        "flashcard_data": cards,
        "study_stale": bool(study and study.source_fingerprint and study.source_fingerprint != source_hash(source_snapshot(lecture))),
        "note": note,
        "chat_form": QuestionForm(),
        "chat_history": chat_history,
        "note_data": data,
        "importance_candidates": candidates,
        "exam_candidates": [item for item in candidates if item.final_category == "exam_important"],
        "note_stale": bool(note and note.transcript_fingerprint != fingerprint(transcript_source(segments))),
        "transcript": Transcript.objects.filter(lecture=lecture).first(),
        "segments": segments,
        "active_job": lecture.jobs.filter(kind__in=CORE, status__in=ACTIVE).first(),
        "study_job": lecture.jobs.filter(kind="study", status__in=ACTIVE).first(),
        "pending_chat_jobs": [{"url": reverse("lectures:job_status", args=[lecture.pk, job.pk])} for job in lecture.jobs.filter(kind="chat", user=request.user, status__in=ACTIVE)],
        "is_processing": lecture.jobs.filter(kind__in=CORE, status__in=ACTIVE).exists() or lecture.status in (
            Lecture.Status.PROCESSING_AUDIO, Lecture.Status.TRANSCRIBING, Lecture.Status.GENERATING_NOTES,
        ),
    })


@login_required
@require_safe
def lecture_audio(request, pk):
    lecture = get_object_or_404(Lecture, pk=pk, owner=request.user)
    if not lecture.audio_file:
        raise Http404("No audio is available.")
    try:
        return audio_response(request, lecture.audio_file)
    except FileNotFoundError as exc:
        raise Http404("Audio file not found.") from exc



@login_required
@require_safe
def lecture_record(request):
    form = RecordingForm(user=request.user)
    return render(request, "lectures/lecture_record.html", {
        "form": form,
        "has_courses": form.fields["course"].queryset.exists(),
        "max_upload_bytes": settings.MAX_AUDIO_UPLOAD_BYTES,
        "max_upload_mb": settings.MAX_AUDIO_UPLOAD_BYTES // (1024 * 1024),
    })


@login_required
@require_POST
def recording_upload(request):
    form = RecordingForm(request.POST, request.FILES, user=request.user)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
    try:
        lecture = create_uploaded_lecture(form=form, owner=request.user)
    except OSError:
        logging.getLogger(__name__).exception("Recording storage failed.")
        return JsonResponse(
            {"error": "Unable to save the recording. Please try again."}, status=503
        )
    return JsonResponse({
        "id": lecture.pk,
        "url": reverse("lectures:detail", args=[lecture.pk]),
    }, status=201)


@login_required
@require_POST
def lecture_process(request, pk):
    lecture = get_object_or_404(Lecture, pk=pk, owner=request.user)
    try:
        enqueue(lecture, user=request.user)
        messages.success(request, "Lecture queued for local processing.")
    except JobError as exc:
        messages.error(request, str(exc))
    return redirect("lectures:detail", pk=lecture.pk)


@login_required
@require_safe
def lecture_notes_pdf(request, pk):
    lecture = get_object_or_404(Lecture.objects.select_related("course"), pk=pk, owner=request.user)
    note = get_object_or_404(LectureNote, lecture=lecture)
    try:
        pdf = render_notes_pdf(lecture, note)
    except PDFExportError as exc:
        return render(request, "notes/pdf/export_error.html", {"lecture": lecture, "error": str(exc)}, status=503)
    response = FileResponse(
        BytesIO(pdf), content_type="application/pdf", as_attachment=True,
        filename=f"classnote-ai-lecture-{lecture.pk}-notes.pdf",
    )
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_safe
def lecture_status(request, pk):
    lecture = get_object_or_404(Lecture, pk=pk, owner=request.user)
    response = JsonResponse({"status": lecture.status, "progress": lecture.progress,
                             "message": lecture.processing_message or lecture.get_status_display()})
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_safe
def job_status(request, pk, job_id):
    lecture = get_object_or_404(Lecture, pk=pk, owner=request.user)
    job = get_object_or_404(BackgroundJob, pk=job_id, lecture=lecture, user=request.user)
    result = job.result
    if job.kind == "chat" and job.status == "completed":
        turn = get_object_or_404(ChatMessage, pk=result.get("message_id"), lecture=lecture, user=request.user)
        result = {"id": turn.pk, "question": turn.question, "answer": turn.answer,
                  "sources": turn.sources, "created_at": turn.created_at.isoformat()}
    response = JsonResponse({"status": job.status, "progress": job.progress, "message": job.message,
                             "error": job.error, "result": result})
    response["Cache-Control"] = "private, no-store"
    return response
