from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_safe

from courses.models import Course
from lectures.models import Lecture

@require_safe
def home(request):
    return render(request, "home.html")

@require_safe
def health(request):
    return JsonResponse({"status": "ok"})


@login_required
@require_safe
def dashboard(request):
    return render(request, "dashboard.html", {
        "recent_courses": Course.objects.filter(owner=request.user)[:5],
        "recent_lectures": Lecture.objects.filter(owner=request.user).select_related("course")[:5],
    })
