from django.conf import settings
from django.db import models


class Course(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="courses"
    )
    course_code = models.CharField(max_length=30)
    course_name = models.CharField(max_length=200)
    teacher_name = models.CharField(max_length=150, blank=True)
    department = models.CharField(max_length=150, blank=True)
    semester = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.course_code} — {self.course_name}"
