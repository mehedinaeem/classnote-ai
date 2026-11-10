import io
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch
import subprocess

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from courses.models import Course
from .forms import LectureForm
from .models import Lecture
from .services import create_uploaded_lecture, inspect_audio


def audio(name="lecture.wav"):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 800)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="audio/wav")


class LectureTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="student")
        cls.other = get_user_model().objects.create_user(username="other")
        cls.course = Course.objects.create(owner=cls.user, course_code="CSE101", course_name="Computing")
        cls.other_course = Course.objects.create(owner=cls.other, course_code="SECRET", course_name="Private")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.directory.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.client.force_login(self.user)

    def data(self, **kwargs):
        return {
            "course": self.course.pk, "title": "Intro lecture",
            "lecture_date": "2026-11-09", "description": "An introduction",
            "audio_file": audio(), **kwargs,
        }

    def upload(self, owner=None, course=None, title="Intro lecture"):
        form = LectureForm(data={
            "course": (course or self.course).pk, "title": title, "lecture_date": "2026-11-09",
        }, files={"audio_file": audio()}, user=owner or self.user)
        self.assertTrue(form.is_valid(), form.errors)
        return create_uploaded_lecture(form=form, owner=owner or self.user)

    def test_model_defaults_and_validation(self):
        lecture = Lecture(owner=self.user, course=self.course, title="Draft")
        lecture.full_clean()
        lecture.save()
        self.assertEqual(lecture.status, Lecture.Status.DRAFT)
        self.assertIsNone(lecture.duration_seconds)
        self.assertEqual(str(lecture), "Draft")
        self.assertIsNotNone(lecture.created_at)
        lecture.course = self.other_course
        with self.assertRaises(ValidationError):
            lecture.full_clean()

    def test_upload_persists_metadata_path_and_audio(self):
        response = self.client.post(reverse("lectures:create"), self.data(
            owner=self.other.pk, status="completed", original_filename="forged"
        ))
        lecture = Lecture.objects.get()
        self.assertRedirects(response, reverse("lectures:detail", args=[lecture.pk]))
        self.assertEqual(lecture.owner, self.user)
        self.assertEqual(lecture.course, self.course)
        self.assertEqual(lecture.status, "uploaded")
        self.assertEqual(lecture.original_filename, "lecture.wav")
        self.assertTrue(lecture.audio_file.name.startswith(f"lectures/{self.user.pk}/{lecture.pk}/"))
        self.assertTrue(Path(lecture.audio_file.path).exists())
        self.assertIsNone(lecture.duration_seconds)

    def test_audio_playback_is_private(self):
        lecture = self.upload()
        url = reverse("lectures:audio", args=[lecture.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertTrue(b"".join(response.streaming_content).startswith(b"RIFF"))
        self.assertEqual(self.client.get(lecture.audio_file.url).status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.logout()
        self.assertRedirects(self.client.get(url), f"/login/?next={url}")

    def test_missing_audio_returns_404(self):
        lecture = self.upload()
        Path(lecture.audio_file.path).unlink()
        self.assertEqual(self.client.get(reverse("lectures:audio", args=[lecture.pk])).status_code, 404)

    def test_invalid_uploads_do_not_persist(self):
        for upload in (
            SimpleUploadedFile("bad.exe", b"not audio"),
            SimpleUploadedFile("fake.mp3", b"not audio"),
            SimpleUploadedFile("empty.wav", b""),
            audio("wrong.mp3"),
        ):
            with self.subTest(filename=upload.name):
                response = self.client.post(reverse("lectures:create"), self.data(audio_file=upload))
                self.assertEqual(response.status_code, 200)
                self.assertIn("audio_file", response.context["form"].errors)
                self.assertFalse(Lecture.objects.exists())
        self.assertEqual(list(Path(self.directory.name).rglob("*")), [])

    @override_settings(MAX_AUDIO_UPLOAD_BYTES=10)
    def test_size_limit(self):
        response = self.client.post(reverse("lectures:create"), self.data())
        self.assertIn("audio_file", response.context["form"].errors)
        self.assertFalse(Lecture.objects.exists())

    def test_missing_required_fields(self):
        response = self.client.post(reverse("lectures:create"), {})
        self.assertEqual(response.status_code, 200)
        for field in ("title", "course", "audio_file", "lecture_date"):
            self.assertIn(field, response.context["form"].errors)
        self.assertFalse(Lecture.objects.exists())

    def test_course_choices_and_forged_course(self):
        response = self.client.get(reverse("lectures:create"))
        self.assertEqual(list(response.context["form"].fields["course"].queryset), [self.course])
        response = self.client.post(reverse("lectures:create"), self.data(course=self.other_course.pk))
        self.assertIn("course", response.context["form"].errors)
        self.assertFalse(Lecture.objects.exists())

    def test_no_course_guidance(self):
        user = get_user_model().objects.create_user(username="no-courses")
        self.client.force_login(user)
        self.assertContains(
            self.client.get(reverse("lectures:create")), "Create a course before uploading"
        )

    def test_detail_and_owner_scoped_lists(self):
        lecture = self.upload()
        other = self.upload(owner=self.other, course=self.other_course, title="Secret lecture")
        response = self.client.get(reverse("lectures:detail", args=[lecture.pk]))
        for text in ("Intro lecture", "CSE101", "Uploaded", "Process lecture", "<audio"):
            self.assertContains(response, text)
        for url in (reverse("lectures:list"), reverse("dashboard"), reverse("courses:detail", args=[self.course.pk])):
            response = self.client.get(url)
            self.assertContains(response, lecture.title)
            self.assertNotContains(response, other.title)
        self.assertEqual(self.client.get(reverse("lectures:detail", args=[other.pk])).status_code, 404)

    def test_pages_require_login(self):
        lecture = self.upload()
        self.client.logout()
        for url in (reverse("lectures:list"), reverse("lectures:create"), reverse("lectures:detail", args=[lecture.pk])):
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), f"/login/?next={url}")
        self.assertEqual(self.client.post(reverse("lectures:create"), {}).status_code, 302)

    def test_upload_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(reverse("lectures:create"), self.data()).status_code, 403)
        self.assertFalse(Lecture.objects.exists())

    def test_list_pagination_and_empty_state(self):
        self.assertContains(self.client.get(reverse("lectures:list")), "No lectures yet.")
        Lecture.objects.bulk_create([
            Lecture(owner=self.user, course=self.course, title=f"Lecture {i}") for i in range(13)
        ])
        response = self.client.get(reverse("lectures:list"))
        self.assertEqual(len(response.context["page_obj"]), 12)
        response = self.client.get(reverse("lectures:list"), {"page": 2})
        self.assertEqual(len(response.context["page_obj"]), 1)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(len(response.context["recent_lectures"]), 5)

    def test_readonly_routes_and_missing_lecture(self):
        lecture = self.upload()
        for name in ("list", "detail", "audio"):
            url = reverse(f"lectures:{name}", args=[] if name == "list" else [lecture.pk])
            self.assertEqual(self.client.post(url).status_code, 405)
        self.assertEqual(self.client.get(reverse("lectures:detail", args=[99999])).status_code, 404)

    def test_probe_failures_are_validation_errors_and_rewind_file(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("ffprobe", 20)):
            upload = audio()
            with patch("lectures.services.subprocess.run", side_effect=error):
                with self.assertRaises(ValidationError):
                    inspect_audio(upload)
            self.assertEqual(upload.tell(), 0)

    def test_video_without_audio_is_rejected(self):
        with patch("lectures.services.subprocess.run") as probe:
            probe.return_value.stdout = '{"format":{"format_name":"mov,mp4,m4a"},"streams":[{"codec_type":"video"}]}'
            with self.assertRaises(ValidationError):
                inspect_audio(SimpleUploadedFile("silent.mp4", b"video"))

    def test_storage_failure_rolls_back_lecture(self):
        form = LectureForm(data={
            "course": self.course.pk, "title": "Failed", "lecture_date": "2026-11-09",
        }, files={"audio_file": audio()}, user=self.user)
        self.assertTrue(form.is_valid())
        with patch("django.core.files.storage.FileSystemStorage._save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                create_uploaded_lecture(form=form, owner=self.user)
        self.assertFalse(Lecture.objects.exists())

    def test_supported_extensions(self):
        from .validators import validate_audio_upload
        for extension in ("mp3", "wav", "m4a", "mp4", "webm", "ogg", "WAV"):
            validate_audio_upload(SimpleUploadedFile(f"lecture.{extension}", b"data"))

    def test_real_supported_containers(self):
        # Real FFmpeg fixtures verify the container probe for every supported type.
        for extension in ("mp3", "wav", "m4a", "mp4", "webm", "ogg"):
            with self.subTest(extension=extension):
                path = Path(self.directory.name) / f"sample.{extension}"
                subprocess.run([
                    "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=16000:cl=mono", "-t", "0.1", "-y", str(path),
                ], check=True, timeout=20, capture_output=True)
                inspect_audio(SimpleUploadedFile(path.name, path.read_bytes()))


class RecordingEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="recorder")
        cls.other = get_user_model().objects.create_user(username="someone-else")
        cls.course = Course.objects.create(owner=cls.user, course_code="REC", course_name="Recording course")
        cls.other_course = Course.objects.create(owner=cls.other, course_code="PRIVATE", course_name="Hidden course")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = override_settings(MEDIA_ROOT=directory.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.client.force_login(self.user)
        self.url = reverse("lectures:recording_upload")

    def payload(self, **kwargs):
        return {"course": self.course.pk, "title": "Recorded lecture", "audio_file": audio(), **kwargs}

    def test_record_page_only_offers_owned_courses(self):
        response = self.client.get(reverse("lectures:record"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Start Recording")
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertContains(response, "js/recorder.js")
        self.assertEqual(list(response.context["form"].fields["course"].queryset), [self.course])
        self.assertNotContains(response, "Hidden course")

    def test_record_page_requires_a_course(self):
        self.client.force_login(self.other)
        self.other_course.delete()
        response = self.client.get(reverse("lectures:record"))
        self.assertContains(response, "Create a course first")
        self.assertNotContains(response, 'id="record-start"')

    def test_saves_with_server_controlled_owner_status_and_date(self):
        response = self.client.post(self.url, self.payload(
            owner=self.other.pk, status="completed", lecture_date="2000-01-01",
        ))
        self.assertEqual(response.status_code, 201)
        lecture = Lecture.objects.get()
        self.assertEqual(response.json()["url"], reverse("lectures:detail", args=[lecture.pk]))
        self.assertEqual(response.json()["id"], lecture.pk)
        self.assertEqual(lecture.owner, self.user)
        self.assertEqual(lecture.status, "uploaded")
        from django.utils import timezone
        self.assertEqual(lecture.lecture_date, timezone.localdate())
        self.assertTrue(Path(lecture.audio_file.path).exists())

    def test_authenticated_csrf_protected_fetch_upload(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.url, self.payload()).status_code, 403)
        client.get(reverse("lectures:record"))
        response = client.post(
            self.url, self.payload(), HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Lecture.objects.count(), 1)

    def test_anonymous_cannot_record_or_upload(self):
        self.client.logout()
        for url in (self.url, reverse("lectures:record")):
            response = self.client.post(url, self.payload()) if url == self.url else self.client.get(url)
            self.assertRedirects(response, f"/login/?next={url}")
        self.assertFalse(Lecture.objects.exists())

    def test_upload_endpoint_requires_post(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_forged_course_rejected(self):
        response = self.client.post(self.url, self.payload(course=self.other_course.pk))
        self.assertEqual(response.status_code, 400)
        self.assertIn("course", response.json()["errors"])
        self.assertFalse(Lecture.objects.exists())

    def test_missing_fields_return_json_errors(self):
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, 400)
        for field in ("title", "course", "audio_file"):
            self.assertIn(field, response.json()["errors"])
        self.assertFalse(Lecture.objects.exists())

    def test_invalid_recordings_are_rejected(self):
        for upload in (
            SimpleUploadedFile("lecture.webm", b"invalid audio", content_type="audio/webm"),
            SimpleUploadedFile("lecture.exe", b"data"),
            SimpleUploadedFile("lecture.webm", b""),
        ):
            with self.subTest(upload=upload.name):
                response = self.client.post(self.url, self.payload(audio_file=upload))
                self.assertEqual(response.status_code, 400)
                self.assertIn("audio_file", response.json()["errors"])
        self.assertFalse(Lecture.objects.exists())

    @override_settings(MAX_AUDIO_UPLOAD_BYTES=10)
    def test_oversized_recording(self):
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 400)
        self.assertIn("audio_file", response.json()["errors"])
        self.assertFalse(Lecture.objects.exists())

    def test_real_browser_container_formats(self):
        for extension in ("webm", "ogg", "mp4"):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / f"recording.{extension}"
                subprocess.run([
                    "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=16000:cl=mono", "-t", "0.1", "-y", str(path),
                ], capture_output=True, check=True, timeout=20)
                response = self.client.post(self.url, self.payload(
                    audio_file=SimpleUploadedFile(path.name, path.read_bytes(), content_type=f"audio/{extension}")
                ))
                self.assertEqual(response.status_code, 201)
                lecture = Lecture.objects.get(pk=response.json()["id"])
                self.assertEqual(lecture.original_filename, path.name)
                self.client.force_login(self.other)
                self.assertEqual(self.client.get(response.json()["url"]).status_code, 404)
                self.client.force_login(self.user)

    def test_storage_failure_returns_retryable_json(self):
        with patch("lectures.views.create_uploaded_lecture", side_effect=OSError("disk full")):
            with self.assertLogs("lectures.views", level="ERROR"):
                response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 503)
        self.assertIn("error", response.json())
        self.assertFalse(Lecture.objects.exists())


class AudioSeekingTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = override_settings(MEDIA_ROOT=directory.name)
        config.enable()
        self.addCleanup(config.disable)
        owner = get_user_model().objects.create_user(username="audio-seeker")
        course = Course.objects.create(owner=owner, course_code="SEEK", course_name="Audio seeking")
        lecture = Lecture.objects.create(owner=owner, course=course, title="Seeking")
        lecture.audio_file.save("lecture.wav", audio())
        self.client.force_login(owner)
        self.url = reverse("lectures:audio", args=[lecture.pk])
        self.original = Path(lecture.audio_file.path).read_bytes()

    def test_single_byte_range_for_browser_seeking(self):
        response = self.client.get(self.url, HTTP_RANGE="bytes=8-31")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertEqual(response["Content-Range"], f"bytes 8-31/{len(self.original)}")
        self.assertEqual(response["Content-Length"], "24")
        self.assertEqual(b"".join(response.streaming_content), self.original[8:32])

    def test_open_ended_and_suffix_ranges(self):
        for header, expected in (
            ("bytes=20-", self.original[20:]), ("bytes=-10", self.original[-10:]),
        ):
            with self.subTest(header=header):
                response = self.client.get(self.url, HTTP_RANGE=header)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(b"".join(response.streaming_content), expected)

    def test_invalid_ranges_are_rejected(self):
        for header in ("bytes=999999-", "bytes=5-2", "bytes=-0", "bytes=-", "bytes=1-2,5-6"):
            with self.subTest(header=header):
                response = self.client.get(self.url, HTTP_RANGE=header)
                self.assertEqual(response.status_code, 416)

    def test_if_range_falls_back_to_full_file(self):
        response = self.client.get(self.url, HTTP_RANGE="bytes=1-10", HTTP_IF_RANGE='"unknown"')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), self.original)

    def test_ranges_do_not_bypass_ownership(self):
        other = get_user_model().objects.create_user(username="other-seeker")
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.url, HTTP_RANGE="bytes=0-10").status_code, 404)
