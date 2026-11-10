from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

User = get_user_model()


class RegistrationTests(TestCase):
    def setUp(self):
        self.data = {
            "username": "student",
            "password1": "Campus-Notebook-73!",
            "password2": "Campus-Notebook-73!",
        }

    def test_registration_page(self):
        self.assertContains(self.client.get(reverse("register")), "Create your account")

    def test_register_creates_user_with_hashed_password(self):
        response = self.client.post(reverse("register"), self.data)
        self.assertRedirects(response, reverse("login"))
        user = User.objects.get(username="student")
        self.assertTrue(user.check_password(self.data["password1"]))
        self.assertNotEqual(user.password, self.data["password1"])

    def test_invalid_passwords_do_not_create_users(self):
        for password1, password2 in (
            ("short", "short"),
            ("password", "password"),
            ("123456789012", "123456789012"),
            ("student", "student"),
            (self.data["password1"], "different"),
        ):
            with self.subTest(password=password1, confirmation=password2):
                response = self.client.post(reverse("register"), {
                    **self.data, "password1": password1, "password2": password2,
                })
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
                self.assertFalse(User.objects.exists())

    def test_duplicate_username(self):
        User.objects.create_user(username="student")
        response = self.client.post(reverse("register"), self.data)
        self.assertIn("username", response.context["form"].errors)
        self.assertEqual(User.objects.count(), 1)

    def test_registration_requires_csrf(self):
        response = Client(enforce_csrf_checks=True).post(reverse("register"), self.data)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.exists())


class LoginLogoutDashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "Campus-Notebook-73!"
        cls.user = User.objects.create_user(username="student", password=cls.password)
        cls.other = User.objects.create_user(username="other-student", password=cls.password)

    def test_login_page(self):
        self.assertContains(self.client.get(reverse("login")), "Welcome back")

    def test_login(self):
        response = self.client.post(reverse("login"), {
            "username": self.user.username, "password": self.password,
        })
        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_invalid_login(self):
        response = self.client.post(reverse("login"), {
            "username": self.user.username, "password": "wrong",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_inactive_user_cannot_login(self):
        self.user.is_active = False
        self.user.save()
        self.client.post(reverse("login"), {
            "username": self.user.username, "password": self.password,
        })
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_rejects_external_next_url(self):
        response = self.client.post(reverse("login"), {
            "username": self.user.username, "password": self.password,
            "next": "https://example.com/steal",
        })
        self.assertRedirects(response, reverse("dashboard"))

    def test_login_preserves_safe_next(self):
        response = self.client.post(reverse("login") + "?next=/dashboard/", {
            "username": self.user.username, "password": self.password,
            "next": "/dashboard/",
        })
        self.assertRedirects(response, reverse("dashboard"))

    def test_anonymous_dashboard_redirects(self):
        self.assertRedirects(
            self.client.get(reverse("dashboard")), "/login/?next=/dashboard/"
        )

    def test_dashboard_only_identifies_current_user(self):
        for user in (self.user, self.other):
            with self.subTest(username=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse("dashboard"))
                self.assertContains(response, f"Welcome, {user.username}!")
                other = self.other if user == self.user else self.user
                self.assertNotContains(response, f"Welcome, {other.username}!")
                self.assertContains(response, "No lectures yet.")
                self.assertContains(response, "No courses yet")
                self.assertContains(response, "Record New Lecture")
                self.assertContains(response, "Upload Lecture")
                self.assertQuerySetEqual(response.context["recent_courses"], [])
                self.assertQuerySetEqual(response.context["recent_lectures"], [])

    def test_authenticated_auth_pages_redirect(self):
        self.client.force_login(self.user)
        for name in ("login", "register"):
            with self.subTest(page=name):
                self.assertRedirects(self.client.get(reverse(name)), reverse("dashboard"))

    def test_logout_requires_post_and_clears_session(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        self.assertIn("_auth_user_id", self.client.session)
        self.assertRedirects(self.client.post(reverse("logout")), reverse("home"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertRedirects(self.client.get(reverse("dashboard")), "/login/?next=/dashboard/")

    def test_logout_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(reverse("logout")).status_code, 403)
        self.assertIn("_auth_user_id", client.session)
        page = client.get(reverse("dashboard"))
        self.assertContains(page, "csrfmiddlewaretoken")
        token = client.cookies["csrftoken"].value
        self.assertRedirects(
            client.post(reverse("logout"), {"csrfmiddlewaretoken": token}),
            reverse("home"),
        )
        self.assertNotIn("_auth_user_id", client.session)

    def test_login_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post(reverse("login"), {
            "username": self.user.username, "password": self.password,
        }).status_code, 403)
