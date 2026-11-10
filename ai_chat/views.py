from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from lectures.models import Lecture
from .forms import QuestionForm
from lectures.services.background_jobs import JobError, enqueue


@login_required
@require_POST
def ask(request, pk):
    lecture = get_object_or_404(Lecture, pk=pk, owner=request.user)
    wants_json = "application/json" in request.headers.get("Accept", "")
    form = QuestionForm(request.POST)
    if not form.is_valid():
        if wants_json:
            return JsonResponse({"error": form.errors["question"][0]}, status=400)
        messages.error(request, form.errors["question"][0])
    else:
        try:
            job = enqueue(lecture, user=request.user, kind="chat", payload=form.cleaned_data)
        except JobError as exc:
            if wants_json:
                return JsonResponse({"error": str(exc)}, status=503)
            messages.error(request, str(exc))
        else:
            if wants_json:
                return JsonResponse({
                    "job_id": str(job.pk), "question": form.cleaned_data["question"],
                    "status_url": reverse("lectures:job_status", args=[lecture.pk, job.pk]),
                }, status=202)
            messages.success(request, "Your question is queued for a local answer.")
    return redirect(reverse("lectures:detail", args=[lecture.pk]) + "#ask-ai")
