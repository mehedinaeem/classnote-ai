from django.urls import path
from . import views

app_name = "ai_chat"
urlpatterns = [path("lectures/<int:pk>/ask/", views.ask, name="ask")]
