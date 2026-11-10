from config.test_utils import EagerClient, enable_eager
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
from unittest import skipUnless
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from courses.models import Course
from lectures.models import Lecture
from transcription.models import TranscriptSegment
from notes.services.interfaces import LLMServiceError
from .models import ChatMessage
from .services import embedding_service as embeddings, vector_store, rag_service as rag

TEXT = "Support vectors are the closest points to the decision boundary and determine the separating hyperplane."
QUESTION = "Why are support vectors important?"
CHUNK = {"chunk_id": 0, "segment_id": 1, "start_time": 1338.0, "end_time": 1380.0, "text": TEXT}


def supported_answer():
    return json.dumps({
        "supported": True,
        "answer": "According to the lecture, support vectors determine the separating hyperplane.",
        "sources": [{"chunk_id": 0, "excerpt": TEXT}],
    })


class GroundingTests(SimpleTestCase):
    def test_grounded_answer_gets_server_timestamp_and_exact_quote(self):
        answer, sources = rag.validate_answer(supported_answer(), [CHUNK])
        self.assertIn("separating hyperplane", answer)
        self.assertEqual(sources, [{"timestamp": 1338.0, "excerpt": TEXT}])

    def test_unsupported_flag_uses_exact_fallback(self):
        raw = json.dumps({"supported": False, "answer": "General knowledge", "sources": []})
        self.assertEqual(rag.validate_answer(raw, [CHUNK]), (rag.NOT_DISCUSSED, []))

    def test_invalid_quotes_ids_missing_sources_and_json_cannot_leak_answer(self):
        cases = [
            "not JSON",
            json.dumps({"supported": True, "answer": "An invented fact.", "sources": []}),
            json.dumps({"supported": True, "answer": "An invented fact.", "sources": [{"chunk_id": 5, "excerpt": TEXT}]}),
            json.dumps({"supported": True, "answer": "An invented fact.", "sources": [{"chunk_id": 0, "excerpt": "Teacher never said this"}]}),
            json.dumps({"supported": True, "answer": "An invented fact.", "sources": [{"chunk_id": True, "excerpt": TEXT}]}),
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertLogs(rag.logger, level="WARNING"):
                self.assertEqual(rag.validate_answer(raw, [CHUNK]), (rag.NOT_DISCUSSED, []))


class EmbeddingTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        (self.path / "modules.json").touch()
        config = override_settings(EMBEDDING_MODEL_PATH=self.path)
        config.enable()
        self.addCleanup(config.disable)
        self.model = Mock(max_seq_length=512)
        self.model.tokenizer.side_effect = lambda text, **kwargs: {"input_ids": list(range(len(text)))}
        self.constructor = Mock(return_value=self.model)
        module = ModuleType("sentence_transformers")
        module.SentenceTransformer = self.constructor
        for patcher in (
            patch.dict(sys.modules, {"sentence_transformers": module}),
            patch.object(embeddings, "_model", None),
            patch.object(embeddings, "_model_key", None),
            patch.object(embeddings, "_model_pid", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_load_once_offline_under_concurrency(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            models = list(executor.map(lambda _: embeddings._get_model(), range(12)))
        self.assertTrue(all(model is self.model for model in models))
        self.constructor.assert_called_once()
        self.assertTrue(self.constructor.call_args.kwargs["local_files_only"])
        self.assertFalse(self.constructor.call_args.kwargs["trust_remote_code"])

    def test_query_passage_prefixes_and_normalization(self):
        embeddings.embed_documents(["বাংলা lecture"])
        self.assertEqual(self.model.encode.call_args.args[0], ["passage: বাংলা lecture"])
        self.assertTrue(self.model.encode.call_args.kwargs["normalize_embeddings"])
        embeddings.embed_question("What is a support vector?")
        self.assertEqual(self.model.encode.call_args.args[0], ["query: What is a support vector?"])

    def test_token_limits_and_missing_model(self):
        self.assertFalse(embeddings.passage_fits("a" * 600))
        with self.assertRaises(embeddings.EmbeddingError):
            embeddings.embed_question("a" * 600)
        embeddings._reset()
        (self.path / "modules.json").unlink()
        with self.assertRaisesMessage(embeddings.EmbeddingError, "download_embedding_model"):
            embeddings._get_model()

    def test_fork_reset_and_configuration_change(self):
        embeddings._get_model()
        with override_settings(EMBEDDING_MODEL="different"):
            with self.assertRaises(embeddings.EmbeddingError):
                embeddings._get_model()
        embeddings._reset()
        embeddings._get_model()
        self.assertEqual(self.constructor.call_count, 2)


class ChunkTests(SimpleTestCase):
    @override_settings(RAG_CHUNK_CHARS=80)
    def test_chunks_keep_text_and_absolute_source_times(self):
        source = [{**CHUNK, "text": "বাংলা technical term " * 50}]
        chunks = vector_store.chunk_segments(source, fits=lambda text: len(text) <= 60)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(item["text"]) <= 60 for item in chunks))
        self.assertTrue(all(item["start_time"] == 1338.0 for item in chunks))
        self.assertEqual(" ".join(item["text"] for item in chunks).split(), source[0]["text"].split())

    def test_empty_transcript_and_invalid_token_budget(self):
        self.assertEqual(vector_store.chunk_segments([]), [])
        with self.assertRaises(vector_store.VectorStoreError):
            vector_store.chunk_segments([CHUNK], fits=lambda _: False)


@skipUnless(importlib.util.find_spec("faiss") and importlib.util.find_spec("numpy"), "Optional FAISS dependencies not installed")
class FAISSStoreTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        config = override_settings(MEDIA_ROOT=self.root)
        config.enable()
        self.addCleanup(config.disable)
        self.owner = get_user_model().objects.create_user(username="faiss-owner")
        course = Course.objects.create(owner=self.owner, course_code="ML", course_name="Machine learning")
        self.lecture = Lecture.objects.create(owner=self.owner, course=course, title="Vectors")
        self.segment = TranscriptSegment.objects.create(lecture=self.lecture, sequence=1, start_time=1338, end_time=1380, text=TEXT)
        TranscriptSegment.objects.create(lecture=self.lecture, sequence=2, start_time=1500, end_time=1530, text="Homework is chapter 2.")
        patchers = [
            patch.object(vector_store, "passage_fits", return_value=True),
            patch.object(vector_store, "embed_documents", side_effect=lambda texts: [[1, 0] if "Support" in text else [0, 1] for text in texts]),
            patch.object(vector_store, "embed_question", return_value=[[1, 0]]),
        ]
        self.mocks = []
        for patcher in patchers:
            self.mocks.append(patcher.start())
            self.addCleanup(patcher.stop)

    def search(self):
        return vector_store.search_lecture(self.lecture, user=self.owner, question=QUESTION)

    def test_real_faiss_search_and_private_disk_cache_reuse(self):
        selected, original = self.search()
        self.assertEqual(selected[0]["text"], TEXT)
        self.assertEqual(len(selected), 1)
        self.assertEqual(original, vector_store.source_fingerprint(vector_store.transcript_snapshot(self.lecture)))
        files = list((self.root / "lectures" / str(self.owner.pk) / str(self.lecture.pk) / "search").glob("*/index.faiss"))
        self.assertEqual(len(files), 1)
        self.search()
        self.mocks[1].assert_called_once()

    def test_transcript_changes_rebuild_index(self):
        self.search()
        self.segment.text += " New definition."
        self.segment.save()
        self.search()
        self.assertEqual(self.mocks[1].call_count, 2)

    def test_corrupt_cache_rebuilds_without_returning_untrusted_metadata(self):
        self.search()
        index_file = next(self.root.rglob("index.faiss"))
        index_file.write_bytes(b"corrupt")
        with self.assertLogs(vector_store.logger, level="WARNING"):
            selected, _ = self.search()
        self.assertEqual(selected[0]["text"], TEXT)
        self.assertEqual(self.mocks[1].call_count, 2)

    def test_foreign_owner_is_rejected_before_embeddings_or_files(self):
        other = get_user_model().objects.create_user(username="not-owner")
        with self.assertRaises(PermissionDenied):
            vector_store.search_lecture(self.lecture, user=other, question=QUESTION)
        self.mocks[1].assert_not_called()
        self.assertFalse(list(self.root.rglob("index.faiss")))

    def test_dimension_or_zero_vector_errors_are_explicit(self):
        self.mocks[1].return_value = [[0, 0], [0, 0]]
        self.mocks[1].side_effect = None
        with self.assertRaises(vector_store.VectorStoreError):
            self.search()

    @override_settings(RAG_MIN_SIMILARITY=1.0)
    def test_relevance_threshold_can_return_no_context(self):
        self.mocks[2].return_value = [[0.5, 0.5]]
        selected, _ = self.search()
        self.assertEqual(selected, [])

    @override_settings(RAG_CONTEXT_CHARS=512)
    def test_context_budget_limits_selected_chunks(self):
        self.segment.text = "Support " + "a" * 290
        self.segment.save()
        TranscriptSegment.objects.filter(lecture=self.lecture, sequence=2).update(text="Support " + "b" * 290)
        selected, _ = self.search()
        from notes.services.chunking import serialize
        self.assertEqual(len(selected), 1)
        self.assertLessEqual(sum(len(serialize(chunk)) for chunk in selected), 512)

    def test_foreign_chunk_metadata_is_rebuilt_before_use(self):
        self.search()
        metadata_file = next(self.root.rglob("chunks.json"))
        data = json.loads(metadata_file.read_text())
        data["chunks"][0]["text"] = "PRIVATE CONTENT FROM ANOTHER LECTURE"
        metadata_file.write_text(json.dumps(data))
        with self.assertLogs(vector_store.logger, level="WARNING"):
            selected, _ = self.search()
        self.assertEqual(selected[0]["text"], TEXT)
        self.assertNotIn("PRIVATE CONTENT", str(selected))


class LectureChatTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="chat-owner")
        cls.other = get_user_model().objects.create_user(username="chat-other")
        cls.course = Course.objects.create(owner=cls.owner, course_code="ML", course_name="Machine learning")
        cls.lecture = Lecture.objects.create(owner=cls.owner, course=cls.course, title="SVM", status="completed")
        TranscriptSegment.objects.create(lecture=cls.lecture, sequence=1, start_time=1338, end_time=1380, text=TEXT)

    def setUp(self):
        enable_eager(self)
        self.client.force_login(self.owner)
        self.url = reverse("ai_chat:ask", args=[self.lecture.pk])
        self.fingerprint = vector_store.source_fingerprint(vector_store.transcript_snapshot(self.lecture))
        self.llm = Mock()
        self.llm.chat.return_value = supported_answer()
        patchers = [
            patch.object(rag, "search_lecture", return_value=([CHUNK], self.fingerprint)),
            patch.object(rag, "get_llm_client", return_value=self.llm),
        ]
        self.mocks = []
        for patcher in patchers:
            self.mocks.append(patcher.start())
            self.addCleanup(patcher.stop)

    def test_answer_persisted_with_sources_and_only_retrieved_context_sent(self):
        message = rag.ask_lecture(self.lecture, user=self.owner, question=QUESTION)
        self.assertEqual(message.user, self.owner)
        self.assertEqual(message.lecture, self.lecture)
        self.assertEqual(message.sources[0]["timestamp"], 1338.0)
        self.assertIsNotNone(message.created_at)
        prompt = self.llm.chat.call_args.args[0]
        self.assertIn("Never answer from general knowledge", prompt[0]["content"])
        payload = json.loads(prompt[1]["content"])
        self.assertEqual(payload["lecture_context"], [{"chunk_id": 0, "text": TEXT}])
        self.assertEqual(payload["question"], QUESTION)

    def test_no_relevant_context_never_calls_llm_and_saves_exact_fallback(self):
        self.mocks[0].return_value = ([], self.fingerprint)
        message = rag.ask_lecture(self.lecture, user=self.owner, question="Explain quantum gravity")
        self.assertEqual(message.answer, rag.NOT_DISCUSSED)
        self.assertEqual(message.sources, [])
        self.llm.chat.assert_not_called()

    def test_unanswerable_or_invalid_model_answer_does_not_save_general_knowledge(self):
        self.llm.chat.return_value = json.dumps({"supported": False, "answer": "I know this from elsewhere.", "sources": []})
        response = self.client.post(self.url, {"question": "Explain quantum gravity"}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.client.get(response.json()["status_url"]).json()["result"]["answer"], rag.NOT_DISCUSSED)
        self.assertEqual(self.client.get(response.json()["status_url"]).json()["result"]["sources"], [])

    def test_history_is_not_fed_back_as_context(self):
        ChatMessage.objects.create(user=self.owner, lecture=self.lecture, question="Old unrelated question",
                                   answer="UNRELATED OLD ANSWER", sources=[], transcript_fingerprint=self.fingerprint)
        rag.ask_lecture(self.lecture, user=self.owner, question=QUESTION)
        self.assertNotIn("UNRELATED OLD ANSWER", str(self.llm.chat.call_args))

    def test_json_and_non_js_submission(self):
        response = self.client.post(self.url, {"question": QUESTION}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.client.get(response.json()["status_url"]).json()["result"]["sources"], [{"timestamp": 1338.0, "excerpt": TEXT}])
        response = self.client.post(self.url, {"question": QUESTION})
        self.assertEqual(response["Location"], reverse("lectures:detail", args=[self.lecture.pk]) + "#ask-ai")
        self.assertEqual(ChatMessage.objects.count(), 2)

    def test_authorization_in_view_and_service(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(self.url, {"question": QUESTION}).status_code, 404)
        with self.assertRaises(PermissionDenied):
            rag.ask_lecture(self.lecture, user=self.other, question=QUESTION)
        self.mocks[0].assert_not_called()
        self.llm.chat.assert_not_called()

    def test_login_post_csrf_and_question_validation(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        self.assertRedirects(self.client.post(self.url, {"question": QUESTION}), f"/login/?next={self.url}")
        client = EagerClient(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(self.url, {"question": QUESTION}).status_code, 403)
        client.get(reverse("lectures:detail", args=[self.lecture.pk]))
        response = client.post(self.url, {"question": QUESTION}, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 202)
        self.client.force_login(self.owner)
        for question in ("   ", "x" * 1001):
            self.assertEqual(self.client.post(self.url, {"question": question}, HTTP_ACCEPT="application/json").status_code, 400)

    def test_unavailable_local_service_does_not_create_fake_answer_or_change_lecture_status(self):
        self.llm.chat.side_effect = LLMServiceError("Local Ollama unavailable")
        with self.assertLogs(rag.logger, level="WARNING"):
            response = self.client.post(self.url, {"question": QUESTION}, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.client.get(response.json()["status_url"]).json()["status"], "failed")
        self.assertFalse(ChatMessage.objects.exists())
        self.lecture.refresh_from_db()
        self.assertEqual(self.lecture.status, "completed")

    def test_transcript_change_rejects_stale_answer(self):
        def change(*args, **kwargs):
            TranscriptSegment.objects.filter(lecture=self.lecture).update(text="Changed transcript")
            return supported_answer()
        self.llm.chat.side_effect = change
        with self.assertRaisesMessage(rag.RAGError, "transcript changed"):
            rag.ask_lecture(self.lecture, user=self.owner, question=QUESTION)
        self.assertFalse(ChatMessage.objects.exists())

    def test_history_and_source_buttons_render_safely(self):
        message = rag.ask_lecture(self.lecture, user=self.owner, question="<script>alert(1)</script>")
        message.answer = "<img src=x onerror=alert(1)>"
        message.save()
        response = self.client.get(reverse("lectures:detail", args=[self.lecture.pk]))
        self.assertContains(response, "Sources")
        self.assertContains(response, 'data-seek="1338')
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertNotContains(response, "<img src=x")

    def test_chat_model_ownership_validation_and_cascade(self):
        message = ChatMessage(user=self.other, lecture=self.lecture, question=QUESTION,
                              answer="Test", sources=[], transcript_fingerprint=self.fingerprint)
        with self.assertRaises(ValidationError):
            message.full_clean()
        rag.ask_lecture(self.lecture, user=self.owner, question=QUESTION)
        self.lecture.delete()
        self.assertFalse(ChatMessage.objects.exists())

    def test_only_current_users_history_is_visible_and_older_sources_are_labeled(self):
        ChatMessage.objects.create(user=self.other, lecture=self.lecture, question="Foreign private question",
                                   answer="Foreign private answer", sources=[], transcript_fingerprint=self.fingerprint)
        message = rag.ask_lecture(self.lecture, user=self.owner, question=QUESTION)
        TranscriptSegment.objects.filter(lecture=self.lecture).update(text="Updated transcript")
        response = self.client.get(reverse("lectures:detail", args=[self.lecture.pk]))
        self.assertNotContains(response, "Foreign private question")
        self.assertContains(response, "earlier transcript version")
