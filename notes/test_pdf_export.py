from datetime import date
from pathlib import Path
import tempfile
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from courses.models import Course
from lectures.models import Lecture
from transcription.models import TranscriptSegment
from .models import LectureNote
from .services.chunking import fingerprint, transcript_source
from .services.schema import empty_note_data
from .services.pdf_export import PDFExportError, restricted_font_fetcher, select_font

BENGALI_FONT = Path("/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf")


class PDFFontTests(SimpleTestCase):
    @override_settings(PDF_FONT_PATH="/missing/pdf-font.ttf")
    def test_missing_configured_font_has_helpful_error(self):
        with self.assertRaisesMessage(PDFExportError, "PDF_FONT_PATH"):
            select_font("Lecture notes")

    def test_configured_font_is_used_and_coverage_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "user-font.ttf"
            path.touch()
            with override_settings(PDF_FONT_PATH=str(path)):
                with patch("notes.services.pdf_export._font_supports", return_value=True) as supports:
                    self.assertEqual(select_font("বাংলা"), path.resolve())
                    supports.assert_called_once()
                with patch("notes.services.pdf_export._font_supports", return_value=False):
                    with self.assertRaises(PDFExportError):
                        select_font("বাংলা")

    @override_settings(PDF_FONT_PATH="")
    def test_missing_system_bangla_font_fails_instead_of_squares(self):
        with patch("notes.services.pdf_export.Path.is_file", return_value=False):
            with patch("notes.services.pdf_export.shutil.which", return_value=None):
                with self.assertRaisesMessage(PDFExportError, "Noto Sans Bengali"):
                    select_font("শিক্ষার্থী")

    @skipUnless(BENGALI_FONT.is_file(), "System Noto Bengali font not installed")
    @override_settings(PDF_FONT_PATH=str(BENGALI_FONT))
    def test_real_system_font_covers_bangla(self):
        self.assertEqual(select_font("শিক্ষার্থী গুরুত্বপূর্ণ প্রযুক্তি পরীক্ষা"), BENGALI_FONT.resolve())

    def test_only_configured_font_urls_can_be_fetched(self):
        default = Mock(return_value={"string": b"font"})
        fetch = restricted_font_fetcher({"file:///fonts/user.ttf"}, default)
        fetch("file:///fonts/user.ttf")
        default.assert_called_once_with("file:///fonts/user.ttf")
        for url in ("https://example.com/audio.mp3", "file:///media/lecture.wav", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(PDFExportError):
                fetch(url)
        self.assertEqual(default.call_count, 1)


class NotesPDFExportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="pdf-owner")
        cls.other = get_user_model().objects.create_user(username="pdf-other")
        cls.course = Course.objects.create(owner=cls.owner, course_code="CSE321", course_name="Machine Learning")
        cls.lecture = Lecture.objects.create(
            owner=cls.owner, course=cls.course, title="Support Vector Machine",
            lecture_date=date(2026, 11, 10), audio_file="lectures/private-recording.wav",
        )
        TranscriptSegment.objects.create(lecture=cls.lecture, sequence=1, start_time=4, end_time=30, text="SVM is important.")
        data = empty_note_data()
        data.update({
            "summary": "A lecture about support vectors.",
            "topics": [{"title": "Support vectors", "explanation": "Closest points to the boundary.", "start_time": 4}],
            "key_points": ["The margin is maximized."],
            "definitions": [{"term": "Support vector", "definition": "A closest point."}],
            "examples": ["Two groups of points."],
            "formulas": ["w · x + b = 0"],
            "exam_important": [{"text": "SVM", "reason": "SVM is important.", "start_time": 4}],
            "homework": ["Review chapter 2."],
        })
        cls.note = LectureNote.objects.create(
            lecture=cls.lecture, data=data,
            transcript_fingerprint=fingerprint(transcript_source(cls.lecture.transcript_segments.all())),
            model_name="qwen3:4b", processing_time=1,
        )

    def setUp(self):
        self.url = reverse("lectures:export_pdf", args=[self.lecture.pk])
        self.client.force_login(self.owner)

    def test_owner_receives_real_pdf_attachment_without_audio(self):
        with patch("django.db.models.fields.files.FieldFile.open") as audio:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn(f"lecture-{self.lecture.pk}-notes.pdf", response["Content-Disposition"])
        self.assertEqual(response["Cache-Control"], "private, no-store")
        pdf = b"".join(response.streaming_content)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertGreater(len(pdf), 2000)
        self.assertNotIn(b"private-recording.wav", pdf)
        audio.assert_not_called()

    def test_login_and_ownership_are_required_before_rendering(self):
        with patch("lectures.views.render_notes_pdf") as renderer:
            self.client.force_login(self.other)
            self.assertEqual(self.client.get(self.url).status_code, 404)
            self.client.logout()
            self.assertRedirects(self.client.get(self.url), f"/login/?next={self.url}")
        renderer.assert_not_called()

    def test_missing_notes_missing_lecture_and_write_methods(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)
        missing = reverse("lectures:export_pdf", args=[999999])
        self.assertEqual(self.client.get(missing).status_code, 404)
        self.lecture.note.delete()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_document_contains_sections_metadata_and_timestamps(self):
        html = render_to_string("notes/pdf/lecture_notes.html", {
            "lecture": self.lecture, "data": self.note.data, "font_uri": "", "stale": False,
        })
        for text in ("ClassNote AI", "CSE321", "Machine Learning", "Support Vector Machine",
                     "2026-11-10", "Summary", "Topics", "Key Points", "Definitions", "Examples",
                     "Formulas", "Exam Important Topics", "Homework", "[00:00:04]"):
            with self.subTest(text=text):
                self.assertIn(text, html)
        self.assertNotIn("<audio", html)
        self.assertNotIn("private-recording.wav", html)

    def test_source_text_is_escaped_not_executed_or_fetched(self):
        data = {**self.note.data, "summary": '<img src="https://example.com/secret"> & <script>bad()</script>'}
        html = render_to_string("notes/pdf/lecture_notes.html", {"lecture": self.lecture, "data": data})
        self.assertNotIn('<img src=', html)
        self.assertIn("&lt;img", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    @skipUnless(BENGALI_FONT.is_file(), "System Noto Bengali font not installed")
    @override_settings(PDF_FONT_PATH=str(BENGALI_FONT))
    def test_bangla_notes_export_successfully_with_system_font(self):
        self.note.data["summary"] = "শিক্ষার্থী গুরুত্বপূর্ণ প্রযুক্তি ও Support Vector Machine নিয়ে আলোচনা।"
        self.note.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF-"))

    @override_settings(PDF_FONT_PATH="/missing/font.ttf")
    def test_unavailable_font_is_a_useful_error_not_broken_pdf(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, "PDF_FONT_PATH", status_code=503)
        self.assertNotIn("Content-Disposition", response)

    def test_rendering_failure_is_safe_and_does_not_change_notes(self):
        with patch("lectures.views.render_notes_pdf", side_effect=PDFExportError("Renderer unavailable.")):
            response = self.client.get(self.url)
        self.assertContains(response, "Renderer unavailable.", status_code=503)
        self.note.refresh_from_db()
        self.assertEqual(self.note.data["summary"], "A lecture about support vectors.")

    def test_download_link_is_available_on_lecture(self):
        response = self.client.get(reverse("lectures:detail", args=[self.lecture.pk]))
        self.assertContains(response, self.url)
        self.assertContains(response, "Download PDF")
