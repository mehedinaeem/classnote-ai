from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from .forms import CourseForm
from .models import Course

User = get_user_model()


class CourseModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner")

    def test_fields_defaults_and_timestamps(self):
        course = Course.objects.create(
            owner=self.owner, course_code="CSE101", course_name="Introduction to Computing"
        )
        course.full_clean()
        self.assertIsNotNone(course.pk)
        self.assertEqual(str(course), "CSE101 — Introduction to Computing")
        for field in ("teacher_name", "department", "semester", "description"):
            self.assertEqual(getattr(course, field), "")
        created = course.created_at
        updated = course.updated_at
        course.course_name = "Computing"
        course.save()
        course.refresh_from_db()
        self.assertEqual(course.created_at, created)
        self.assertGreater(course.updated_at, updated)

    def test_required_fields_and_lengths(self):
        course = Course(owner=self.owner, course_code="", course_name="")
        with self.assertRaises(ValidationError) as error:
            course.full_clean()
        self.assertIn("course_code", error.exception.message_dict)
        self.assertIn("course_name", error.exception.message_dict)
        course.course_code = "X" * 31
        course.course_name = "X" * 201
        with self.assertRaises(ValidationError):
            course.full_clean()

    def test_owner_cascade(self):
        course = Course.objects.create(owner=self.owner, course_code="CSE", course_name="Test")
        self.owner.delete()
        self.assertFalse(Course.objects.filter(pk=course.pk).exists())

    def test_form_excludes_owner_and_validates_whitespace(self):
        self.assertNotIn("owner", CourseForm().fields)
        form = CourseForm(data={"course_code": "   ", "course_name": " "})
        self.assertFalse(form.is_valid())
        self.assertIn("course_code", form.errors)
        self.assertIn("course_name", form.errors)

    def test_form_trims_values_and_accepts_optional_fields(self):
        form = CourseForm(data={
            "course_code": " CSE101 ", "course_name": " Computing ",
            "teacher_name": "Dr. Teacher", "department": "CSE",
            "semester": "Fall 2026", "description": "Course overview",
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["course_code"], "CSE101")
        self.assertEqual(form.cleaned_data["course_name"], "Computing")


class CourseViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner")
        cls.other = User.objects.create_user(username="other")
        cls.course = Course.objects.create(
            owner=cls.owner, course_code="OWN101", course_name="My subject"
        )
        cls.other_course = Course.objects.create(
            owner=cls.other, course_code="SECRET101", course_name="Private subject"
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def url(self, name, course=None):
        return reverse(f"courses:{name}", kwargs={"pk": (course or self.course).pk})

    def test_list_contains_only_owned_courses(self):
        response = self.client.get(reverse("courses:list"))
        self.assertContains(response, "OWN101")
        self.assertNotContains(response, "SECRET101")
        self.assertEqual(list(response.context["page_obj"]), [self.course])

    def test_empty_list(self):
        Course.objects.filter(owner=self.owner).delete()
        self.assertContains(self.client.get(reverse("courses:list")), "No courses yet")

    def test_list_pagination(self):
        Course.objects.bulk_create([
            Course(owner=self.owner, course_code=f"C{i}", course_name=f"Subject {i}")
            for i in range(13)
        ])
        response = self.client.get(reverse("courses:list"))
        self.assertEqual(len(response.context["page_obj"]), 12)
        response = self.client.get(reverse("courses:list"), {"page": 2})
        self.assertEqual(len(response.context["page_obj"]), 2)

    def test_create_get_and_post_ignores_forged_owner(self):
        self.assertEqual(self.client.get(reverse("courses:create")).status_code, 200)
        response = self.client.post(reverse("courses:create"), {
            "course_code": "NEW101", "course_name": "New course", "owner": self.other.pk,
        })
        course = Course.objects.get(course_code="NEW101")
        self.assertEqual(course.owner, self.owner)
        self.assertRedirects(response, self.url("detail", course))

    def test_invalid_create_does_not_save(self):
        response = self.client.post(reverse("courses:create"), {"course_code": "Missing name"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("course_name", response.context["form"].errors)
        self.assertEqual(Course.objects.count(), 2)

    def test_detail(self):
        response = self.client.get(self.url("detail"))
        self.assertContains(response, self.course.course_name)
        self.assertContains(response, "No lectures yet")
        self.assertNotContains(response, self.other_course.course_name)

    def test_edit_and_ownership_remains_unchanged(self):
        self.assertEqual(self.client.get(self.url("edit")).status_code, 200)
        response = self.client.post(self.url("edit"), {
            "course_code": "EDIT101", "course_name": "Updated subject",
            "teacher_name": "A Teacher", "owner": self.other.pk,
        })
        self.assertRedirects(response, self.url("detail"))
        self.course.refresh_from_db()
        self.assertEqual(self.course.course_name, "Updated subject")
        self.assertEqual(self.course.teacher_name, "A Teacher")
        self.assertEqual(self.course.owner, self.owner)

    def test_invalid_edit_keeps_original(self):
        response = self.client.post(self.url("edit"), {"course_name": ""})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.course.refresh_from_db()
        self.assertEqual(self.course.course_name, "My subject")

    def test_delete_confirmation_does_not_delete_and_post_deletes(self):
        self.assertContains(self.client.get(self.url("delete")), "Delete course?")
        self.assertTrue(Course.objects.filter(pk=self.course.pk).exists())
        self.assertRedirects(self.client.post(self.url("delete")), reverse("courses:list"))
        self.assertFalse(Course.objects.filter(pk=self.course.pk).exists())
        self.assertTrue(Course.objects.filter(pk=self.other_course.pk).exists())

    def test_other_users_course_is_inaccessible(self):
        for name in ("detail", "edit", "delete"):
            for method in ("get", "post"):
                with self.subTest(page=name, method=method):
                    response = getattr(self.client, method)(self.url(name, self.other_course), {
                        "course_code": "HACK", "course_name": "Changed",
                    })
                    self.assertEqual(response.status_code, 405 if name == "detail" and method == "post" else 404)
        self.other_course.refresh_from_db()
        self.assertEqual(self.other_course.course_code, "SECRET101")

    def test_missing_course_returns_404(self):
        for name in ("detail", "edit", "delete"):
            with self.subTest(page=name):
                url = reverse(f"courses:{name}", kwargs={"pk": 999999})
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_all_routes_require_login(self):
        self.client.logout()
        urls = [reverse("courses:list"), reverse("courses:create")]
        urls += [self.url(name) for name in ("detail", "edit", "delete")]
        for url in urls:
            for method in ("get", "post"):
                with self.subTest(url=url, method=method):
                    self.assertRedirects(
                        getattr(self.client, method)(url), f"/login/?next={url}"
                    )

    def test_write_operations_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        for url in (reverse("courses:create"), self.url("edit"), self.url("delete")):
            with self.subTest(url=url):
                self.assertEqual(client.post(url, {
                    "course_code": "HACK", "course_name": "Changed",
                }).status_code, 403)
        self.assertTrue(Course.objects.filter(pk=self.course.pk).exists())
        # Exercise the rendered confirmation form with a valid token.
        client.get(self.url("delete"))
        self.assertRedirects(client.post(self.url("delete"), {
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        }), reverse("courses:list"))

    def test_dashboard_recent_courses_are_owned_and_limited(self):
        courses = [
            Course.objects.create(owner=self.owner, course_code=f"R{i}", course_name=f"Recent {i}")
            for i in range(6)
        ]
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(list(response.context["recent_courses"]), list(reversed(courses))[:5])
        self.assertNotContains(response, "SECRET101")
        self.assertContains(response, "Recent 5")

    def test_course_text_is_escaped(self):
        self.course.course_name = "<script>alert(1)</script>"
        self.course.save()
        response = self.client.get(self.url("detail"))
        self.assertNotContains(response, self.course.course_name)
        self.assertContains(response, "&lt;script&gt;")
