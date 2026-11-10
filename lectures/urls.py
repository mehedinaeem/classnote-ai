from django.urls import path
from . import views

app_name = "lectures"
urlpatterns = [
    path("", views.lecture_list, name="list"),
    path("record/", views.lecture_record, name="record"),
    path("record/upload/", views.recording_upload, name="recording_upload"),
    path("new/", views.lecture_create, name="create"),
    path("<int:pk>/", views.lecture_detail, name="detail"),
    path("<int:pk>/status/", views.lecture_status, name="status"),
    path("<int:pk>/jobs/<uuid:job_id>/status/", views.job_status, name="job_status"),
    path("<int:pk>/process/", views.lecture_process, name="process"),
    path("<int:pk>/export/pdf/", views.lecture_notes_pdf, name="export_pdf"),
    path("<int:pk>/audio/", views.lecture_audio, name="audio"),
]
