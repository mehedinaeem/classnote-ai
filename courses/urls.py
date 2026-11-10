from django.urls import path
from . import views

app_name = "courses"
urlpatterns = [
    path("", views.course_list, name="list"),
    path("new/", views.course_create, name="create"),
    path("<int:pk>/", views.course_detail, name="detail"),
    path("<int:pk>/edit/", views.course_edit, name="edit"),
    path("<int:pk>/delete/", views.course_delete, name="delete"),
]
