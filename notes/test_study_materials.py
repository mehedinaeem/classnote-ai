from config.test_utils import EagerClient, enable_eager
import copy
import json
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from courses.models import Course
from lectures.models import Lecture
from transcription.models import TranscriptSegment
from .models import Flashcard, LectureNote, QuizQuestion, StudyMaterialSet
from .services.chunking import fingerprint, serialize, transcript_source
from .services.interfaces import LLMServiceError
from .services.schema import empty_note_data
from .services import study_generator as study

TEXT = " ".join(f"Concept{i} is Definition{i}." for i in range(1, 21))
CONTEXT = [{"start_time": 4.0, "end_time": 200.0, "text": TEXT}]
SNAPSHOT = {"transcript": CONTEXT, "notes": None}


def question(index=1):
    wrong = [f"Concept{i}" for i in range(1, 21) if i != index][:3]
    return {
        "question": f"Which concept has Definition{index}?",
        "option_a": f"Concept{index}", "option_b": wrong[0], "option_c": wrong[1], "option_d": wrong[2],
        "correct_option": "A", "explanation": f"The lecture states that Concept{index} is Definition{index}.",
        "source_timestamp": 4, "source_excerpt": f"Concept{index} is Definition{index}.",
    }


def card(index=1):
    return {
        "front": f"What is Concept{index}?", "back": f"Definition{index}",
        "source_timestamp": 4, "source_excerpt": f"Concept{index} is Definition{index}.",
    }


def batch(q=1, f=1):
    return {"questions": [question(i + 1) for i in range(q)], "flashcards": [card(i + 1) for i in range(f)]}


def mock_client():
    client = Mock(model="qwen3:4b")
    counters = [0, 0]
    def respond(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        q, f = payload["question_count"], payload["flashcard_count"]
        values = {
            "questions": [question(counters[0] + i + 1) for i in range(q)],
            "flashcards": [card(counters[1] + i + 1) for i in range(f)],
        }
        counters[0] += q
        counters[1] += f
        return serialize(values)
    client.chat.side_effect = respond
    return client


class StudyValidationTests(SimpleTestCase):
    def test_valid_json_and_null_notes_only_timestamp(self):
        data = batch()
        self.assertEqual(study.validate_response(serialize(data), CONTEXT, 1, 1), data)
        for item in data["questions"] + data["flashcards"]:
            item["source_timestamp"] = None
        context = [{**CONTEXT[0], "start_time": None}]
        study.validate_response(serialize(data), context, 1, 1)

    def test_invalid_counts_fields_options_and_correct_answer(self):
        cases = []
        data = batch()
        data["questions"] = []
        cases.append(data)
        data = batch()
        data["questions"][0]["correct_option"] = "E"
        cases.append(data)
        data = batch()
        data["questions"][0]["option_b"] = data["questions"][0]["option_a"]
        cases.append(data)
        data = batch()
        data["questions"][0]["option_a"] = "An invented correct answer"
        cases.append(data)
        data = batch()
        data["flashcards"][0]["front"] = ""
        cases.append(data)
        data = batch()
        data["flashcards"][0]["extra"] = "unexpected"
        cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(study.StudyGenerationError):
                study.validate_response(serialize(data), CONTEXT, 1, 1)

    def test_unproven_evidence_and_invented_timestamps_are_rejected(self):
        for key in ("questions", "flashcards"):
            for change in ({"source_timestamp": 5}, {"source_timestamp": True}, {"source_excerpt": "Never stated"}):
                data = batch()
                data[key][0].update(change)
                with self.subTest(key=key, change=change), self.assertRaises(study.StudyGenerationError):
                    study.validate_response(serialize(data), CONTEXT, 1, 1)

    def test_malformed_duplicate_and_oversized_json(self):
        for raw in ("not json", '{"questions":[],"questions":[],"flashcards":[]}', '{"questions":NaN,"flashcards":[]}'):
            with self.subTest(raw=raw), self.assertRaises(study.StudyGenerationError):
                study.validate_response(raw, CONTEXT, 1, 1)
        with override_settings(STUDY_MAX_RESPONSE_CHARS=10):
            with self.assertRaises(study.StudyGenerationError):
                study.validate_response(serialize(batch()), CONTEXT, 1, 1)


class StudyBatchTests(SimpleTestCase):
    def test_defaults_make_ten_each_in_bounded_batches(self):
        client = mock_client()
        questions, cards = study.generate_items(SNAPSHOT, client=client, question_count=10, flashcard_count=10)
        self.assertEqual(len(questions), 10)
        self.assertEqual(len(cards), 10)
        self.assertEqual(client.chat.call_count, 5)
        for call in client.chat.call_args_list:
            payload = json.loads(call.args[0][1]["content"])
            self.assertLessEqual(len(serialize(payload["source"])), 4000 + 1)
            self.assertIn("ONLY", call.args[0][0]["content"])
            self.assertIn("response_schema", call.kwargs)

    def test_unequal_counts_and_configurable_batch_size(self):
        client = mock_client()
        with override_settings(STUDY_GENERATION_BATCH_SIZE=1):
            questions, cards = study.generate_items(SNAPSHOT, client=client, question_count=2, flashcard_count=3)
        self.assertEqual((len(questions), len(cards)), (2, 3))
        self.assertEqual(client.chat.call_count, 3)

    def test_invalid_response_gets_one_repair(self):
        client = Mock()
        client.chat.side_effect = ["invalid", serialize(batch())]
        with self.assertLogs(study.logger, level="WARNING"):
            questions, cards = study.generate_items(SNAPSHOT, client=client, question_count=1, flashcard_count=1)
        self.assertEqual(len(questions), 1)
        self.assertEqual(client.chat.call_count, 2)

    def test_second_invalid_response_fails_without_partial_result(self):
        client = Mock()
        client.chat.return_value = "{}"
        with self.assertLogs(study.logger, level="WARNING"):
            with self.assertRaises(study.StudyGenerationError):
                study.generate_items(SNAPSHOT, client=client, question_count=1, flashcard_count=1)
        self.assertEqual(client.chat.call_count, 2)

    def test_duplicate_items_are_rejected(self):
        client = Mock()
        data = {"questions": [question(), question()], "flashcards": [card(), card()]}
        client.chat.return_value = serialize(data)
        with self.assertLogs(study.logger, level="WARNING"):
            with self.assertRaisesMessage(study.StudyGenerationError, "distinct"):
                study.generate_items(SNAPSHOT, client=client, question_count=2, flashcard_count=2)

    def test_long_lecture_context_is_bounded_and_distributed(self):
        source = [{
            "start_time": i * 100, "end_time": i * 100 + 99, "text": TEXT,
        } for i in range(40)]
        client = mock_client()
        # Validation for these test batches uses actual start_time from selected context.
        original_response = client.chat.side_effect
        def respond(messages, **kwargs):
            result = json.loads(original_response(messages, **kwargs))
            time = json.loads(messages[1]["content"])["source"][0]["start_time"]
            for item in result["questions"] + result["flashcards"]:
                item["source_timestamp"] = time
            return serialize(result)
        client.chat.side_effect = respond
        study.generate_items({"transcript": source, "notes": None}, client=client, question_count=4, flashcard_count=4)
        contexts = [json.loads(call.args[0][1]["content"])["source"] for call in client.chat.call_args_list]
        self.assertEqual(contexts[0][0]["start_time"], 0)
        self.assertEqual(contexts[-1][-1]["start_time"], 3900)
        self.assertTrue(all(len(serialize(context)) <= 4001 for context in contexts))


class StudyPersistenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="quiz-owner")
        cls.other = get_user_model().objects.create_user(username="quiz-other")
        cls.course = Course.objects.create(owner=cls.owner, course_code="ML", course_name="Machine learning")
        cls.lecture = Lecture.objects.create(owner=cls.owner, course=cls.course, title="Concepts", status="completed")
        TranscriptSegment.objects.create(lecture=cls.lecture, sequence=1, start_time=4, end_time=200, text=TEXT)

    def setUp(self):
        enable_eager(self)
        self.client.force_login(self.owner)
        self.llm = mock_client()
        self.patcher = patch.object(study, "get_llm_client", return_value=self.llm)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.url = reverse("notes:generate_study", args=[self.lecture.pk])

    def generate(self, count=2):
        return study.generate_study_materials(
            self.lecture, user=self.owner, question_count=count, flashcard_count=count,
        )

    def test_defaults_save_ten_mcqs_and_ten_flashcards(self):
        material = study.generate_study_materials(self.lecture, user=self.owner)
        self.assertEqual(material.status, "ready")
        self.assertEqual(material.quiz_count, 10)
        self.assertEqual(material.flashcard_count, 10)
        self.assertEqual(QuizQuestion.objects.count(), 10)
        self.assertEqual(Flashcard.objects.count(), 10)
        self.assertEqual(QuizQuestion.objects.first().correct_option, "A")
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.status, "completed")

    def test_environment_defaults_are_configurable(self):
        with override_settings(QUIZ_QUESTION_COUNT=3, FLASHCARD_COUNT=4):
            result = study.generate_study_materials(self.lecture, user=self.owner)
        self.assertEqual((result.quiz_count, result.flashcard_count), (3, 4))

    def test_generation_route_uses_selected_counts_and_redirects_to_quiz(self):
        response = self.client.post(self.url, {"question_count": 1, "flashcard_count": 2})
        self.assertEqual(response["Location"], reverse("lectures:detail", args=[self.lecture.pk]) + "#quiz")
        self.assertEqual(QuizQuestion.objects.count(), 1)
        self.assertEqual(Flashcard.objects.count(), 2)

    def test_regeneration_replaces_both_sets_atomically(self):
        self.generate()
        self.llm.chat.side_effect = None
        self.llm.chat.return_value = serialize(batch(1, 1))
        self.generate(count=1)
        self.assertEqual(QuizQuestion.objects.count(), 1)
        self.assertEqual(Flashcard.objects.count(), 1)
        self.assertEqual(StudyMaterialSet.objects.count(), 1)

    def test_failure_preserves_previous_material(self):
        self.generate()
        previous_ids = list(QuizQuestion.objects.values_list("pk", flat=True))
        self.llm.chat.side_effect = None
        self.llm.chat.return_value = "invalid"
        with self.assertLogs(study.logger, level="WARNING"):
            with self.assertRaises(study.StudyGenerationError):
                self.generate()
        self.assertEqual(list(QuizQuestion.objects.values_list("pk", flat=True)), previous_ids)
        self.assertEqual(Flashcard.objects.count(), 2)
        self.assertEqual(StudyMaterialSet.objects.get().status, "failed")

    def test_database_failure_rolls_back_both_replacements(self):
        self.generate()
        previous_ids = list(QuizQuestion.objects.values_list("pk", flat=True))
        self.llm.chat.side_effect = mock_client().chat.side_effect
        with patch.object(Flashcard.objects, "bulk_create", side_effect=RuntimeError("db failure")):
            with self.assertLogs(study.logger, level="ERROR"):
                with self.assertRaises(study.StudyGenerationError):
                    self.generate()
        self.assertEqual(list(QuizQuestion.objects.values_list("pk", flat=True)), previous_ids)
        self.assertEqual(Flashcard.objects.count(), 2)

    def test_missing_ollama_and_duplicate_submission(self):
        self.llm.chat.side_effect = LLMServiceError("Ollama unavailable")
        with self.assertLogs(study.logger, level="ERROR"):
            with self.assertRaisesMessage(study.StudyGenerationError, "Ollama unavailable"):
                self.generate()
        self.assertEqual(StudyMaterialSet.objects.get().status, "failed")
        StudyMaterialSet.objects.update(status="generating")
        calls = self.llm.chat.call_count
        with self.assertRaisesMessage(study.StudyGenerationError, "already"):
            self.generate()
        self.assertEqual(self.llm.chat.call_count, calls)

    def test_source_change_rejects_stale_generated_result(self):
        original = self.llm.chat.side_effect
        def change(*args, **kwargs):
            result = original(*args, **kwargs)
            TranscriptSegment.objects.filter(lecture=self.lecture).update(text=TEXT + " Updated.")
            return result
        self.llm.chat.side_effect = change
        with self.assertLogs(study.logger, level="ERROR"):
            with self.assertRaisesMessage(study.StudyGenerationError, "changed"):
                self.generate()
        self.assertFalse(QuizQuestion.objects.exists())
        self.assertFalse(Flashcard.objects.exists())

    def test_only_fresh_notes_are_used_as_source(self):
        snapshot = study.source_snapshot(self.lecture)
        data = empty_note_data()
        data["summary"] = "Fresh supplementary concept."
        note = LectureNote.objects.create(
            lecture=self.lecture, data=data, transcript_fingerprint=fingerprint(snapshot["transcript"]),
            model_name="qwen3:4b", processing_time=1,
        )
        self.assertEqual(study.source_snapshot(self.lecture)["notes"], data)
        note.transcript_fingerprint = "0" * 64
        note.save()
        self.assertIsNone(study.source_snapshot(self.lecture)["notes"])

    def test_authorization_login_post_and_csrf(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(self.url, {"question_count": 2, "flashcard_count": 2}).status_code, 404)
        with self.assertRaises(PermissionDenied):
            study.generate_study_materials(self.lecture, user=self.other)
        self.llm.chat.assert_not_called()
        self.client.logout()
        self.assertRedirects(self.client.post(self.url), f"/login/?next={self.url}")
        client = EagerClient(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(self.url, {"question_count": 1, "flashcard_count": 1}).status_code, 403)
        client.get(reverse("lectures:detail", args=[self.lecture.pk]))
        response = client.post(self.url, {"question_count": 1, "flashcard_count": 1},
                               HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(QuizQuestion.objects.count(), 1)

    def test_invalid_counts_and_empty_transcript_do_not_call_model(self):
        for count in (0, 21, True):
            with self.assertRaises(study.StudyGenerationError):
                study.generate_study_materials(self.lecture, user=self.owner, question_count=count)
        self.lecture.transcript_segments.all().delete()
        with self.assertRaises(study.StudyGenerationError):
            self.generate()
        self.llm.chat.assert_not_called()

    def test_models_validate_choices_timestamps_and_cascade(self):
        self.generate()
        quiz = QuizQuestion.objects.first()
        quiz.correct_option = "E"
        with self.assertRaises(ValidationError):
            quiz.full_clean()
        flashcard = Flashcard.objects.first()
        flashcard.source_timestamp = -1
        with self.assertRaises(ValidationError):
            flashcard.full_clean()
        self.lecture.delete()
        self.assertFalse(QuizQuestion.objects.exists())
        self.assertFalse(Flashcard.objects.exists())
        self.assertFalse(StudyMaterialSet.objects.exists())

    def test_rendering_serializes_safely_and_marks_stale_material(self):
        self.generate()
        question_row = QuizQuestion.objects.first()
        question_row.question = "</script><img src=x onerror=alert(1)>"
        question_row.save()
        TranscriptSegment.objects.filter(lecture=self.lecture).update(text=TEXT + " Changed.")
        response = self.client.get(reverse("lectures:detail", args=[self.lecture.pk]))
        self.assertContains(response, 'id="quiz-data"')
        self.assertContains(response, 'id="flashcard-flip"')
        self.assertContains(response, "Regenerate quiz")
        self.assertContains(response, "Regenerate to update")
        self.assertNotContains(response, "</script><img")
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("lectures:detail", args=[self.lecture.pk])).status_code, 404)
