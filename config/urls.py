from django.contrib import admin
from django.urls import include, path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("", include("accounts.urls")),
    path("lectures/", include("lectures.urls")),
    path("chat/", include("ai_chat.urls")),
    path("notes/", include("notes.urls")),
    path("courses/", include("courses.urls")),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("health/", views.health, name="health"),
    path("admin/", admin.site.urls),
]
