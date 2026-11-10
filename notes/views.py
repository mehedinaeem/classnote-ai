from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_safe, require_POST

from lectures.models import Lecture
from lectures.services.background_jobs import JobError, enqueue
from .services.note_generator import NoteGenerationError, generate_lecture_notes

from .forms import StudyGenerationForm
from .services.study_generator import StudyGenerationError, generate_study_materials
from .services.interfaces import LLMHealth, LLMServiceError
from .services.llm import get_llm_client


@login_required
@require_safe
def ollama_diagnostic(request):
    try:
        health = get_llm_client().health_check()
    except LLMServiceError as exc:
        health = LLMHealth(connected=False, model=settings.OLLAMA_MODEL, message=str(exc))
    return render(request, "notes/ollama_diagnostic.html", {"health": health})


@login_required
@require_POST
def generate_notes(request, pk):
    lecture = get_object_or_404(Lecture, pk=pk, owner=request.user)
    try:
        enqueue(lecture, user=request.user, kind="notes")
        messages.success(request, "Academic notes queued for generation.")
    except JobError as exc:
        messages.error(request, str(exc))
    return redirect(reverse("lectures:detail", args=[lecture.pk]) + "#notes")


@login_required
@require_POST
def generate_study(request, pk):
    lecture = get_object_or_404(Lecture, pk=pk, owner=request.user)
    form = StudyGenerationForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose between 1 and 20 MCQs and flashcards.")
    else:
        try:
            enqueue(lecture, user=request.user, kind="study", payload=form.cleaned_data)
            messages.success(request, "Quiz and flashcards queued for generation.")
        except JobError as exc:
            messages.error(request, str(exc))
    return redirect(reverse("lectures:detail", args=[lecture.pk]) + "#quiz")
