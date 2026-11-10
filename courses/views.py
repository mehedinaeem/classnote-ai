from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_safe

from .forms import CourseForm
from .models import Course


@login_required
@require_safe
def course_list(request):
    courses = Course.objects.filter(owner=request.user)
    page = Paginator(courses, 12).get_page(request.GET.get("page"))
    return render(request, "courses/course_list.html", {"page_obj": page})


@login_required
@require_http_methods(["GET", "POST"])
def course_create(request):
    form = CourseForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        course = form.save(commit=False)
        course.owner = request.user
        course.save()
        messages.success(request, "Course created.")
        return redirect("courses:detail", pk=course.pk)
    return render(request, "courses/course_form.html", {"form": form})


@login_required
@require_safe
def course_detail(request, pk):
    course = get_object_or_404(Course, pk=pk, owner=request.user)
    return render(request, "courses/course_detail.html", {"course": course, "lectures": course.lectures.filter(owner=request.user)})


@login_required
@require_http_methods(["GET", "POST"])
def course_edit(request, pk):
    course = get_object_or_404(Course, pk=pk, owner=request.user)
    form = CourseForm(request.POST if request.method == "POST" else None, instance=course)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Course updated.")
        return redirect("courses:detail", pk=course.pk)
    return render(request, "courses/course_form.html", {"form": form, "course": course})


@login_required
@require_http_methods(["GET", "POST"])
def course_delete(request, pk):
    course = get_object_or_404(Course, pk=pk, owner=request.user)
    if request.method == "POST":
        course.delete()
        messages.success(request, "Course deleted.")
        return redirect("courses:list")
    return render(request, "courses/course_confirm_delete.html", {"course": course})
