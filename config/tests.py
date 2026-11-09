import os
from unittest.mock import patch
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from .settings import env_bool

class PublicPageTests(SimpleTestCase):
    def test_home(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home.html")
        self.assertContains(response, "ClassNote")
        self.assertContains(response, "Bangla")

    def test_health(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertJSONEqual(response.content, {"status": "ok"})

    def test_post_is_rejected(self):
        for name in ("home", "health"):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse(name)).status_code, 405)

    def test_head_is_supported(self):
        self.assertEqual(self.client.head(reverse("health")).status_code, 200)

    def test_local_assets(self):
        for asset in ("css/site.css", "vendor/bootstrap/bootstrap.min.css"):
            with self.subTest(asset=asset):
                self.assertIsNotNone(finders.find(asset))

    def test_apps_installed(self):
        for name in ("accounts", "courses", "lectures", "transcription", "notes", "ai_chat"):
            with self.subTest(app=name):
                self.assertTrue(apps.is_installed(name))

class EnvironmentTests(SimpleTestCase):
    def test_boolean_values(self):
        for value, expected in (("true", True), ("1", True), ("off", False), ("False", False)):
            with self.subTest(value=value), patch.dict(os.environ, {"TEST_BOOL": value}):
                self.assertEqual(env_bool("TEST_BOOL"), expected)

    def test_invalid_boolean(self):
        with patch.dict(os.environ, {"TEST_BOOL": "maybe"}):
            with self.assertRaises(ImproperlyConfigured):
                env_bool("TEST_BOOL")

class AuthenticationTests(TestCase):
    def test_builtin_authentication(self):
        user = get_user_model().objects.create_user(username="student", password="test-password")
        self.assertTrue(self.client.login(username="student", password="test-password"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_admin_requires_login(self):
        response = self.client.get(reverse("admin:index"))
        self.assertRedirects(response, reverse("admin:login") + "?next=/admin/")
