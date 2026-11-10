from django.urls import path
from . import views

app_name = "notes"
urlpatterns = [
    path("lectures/<int:pk>/study/generate/", views.generate_study, name="generate_study"),
    path("lectures/<int:pk>/generate/", views.generate_notes, name="generate"),
    path("diagnostics/ollama/", views.ollama_diagnostic, name="ollama_diagnostic"),
]
