from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import views
from .forms import LoginForm

urlpatterns = [
    path("login/", LoginView.as_view(
        template_name="accounts/login.html",
        authentication_form=LoginForm,
        redirect_authenticated_user=True,
    ), name="login"),
    path("register/", views.register, name="register"),
    path("logout/", LogoutView.as_view(), name="logout"),
]
