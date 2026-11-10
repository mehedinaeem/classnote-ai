import io
import json
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .services.interfaces import LLMHealth
from .services.ollama_client import (
    OllamaClient, OllamaConfigurationError, OllamaConnectionError,
    OllamaResponseError, OllamaTimeoutError, _NoRedirects,
)


@override_settings(
    OLLAMA_BASE_URL="http://localhost:11434",
    OLLAMA_MODEL="qwen3:4b", OLLAMA_TIMEOUT_SECONDS=120,
    OLLAMA_HEALTH_TIMEOUT_SECONDS=3,
)
class OllamaClientTests(SimpleTestCase):
    def setUp(self):
        self.client_service = OllamaClient()
        patcher = patch.object(self.client_service._opener, "open")
        self.open = patcher.start()
        self.addCleanup(patcher.stop)

    def respond(self, payload):
        self.open.return_value.__enter__.return_value.read.return_value = json.dumps(
            payload, ensure_ascii=False
        ).encode("utf-8")

    def test_health_reports_connected_and_installed_without_inference(self):
        self.respond({"models": [{"name": "qwen3:4b"}]})
        health = self.client_service.health_check()
        self.assertTrue(health.connected)
        self.assertTrue(health.model_available)
        self.assertEqual(health.model, "qwen3:4b")
        request = self.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:11434/api/tags")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(self.open.call_args.kwargs["timeout"], 3)

    def test_reachable_without_installed_model_is_distinct(self):
        self.respond({"models": []})
        health = self.client_service.health_check()
        self.assertTrue(health.connected)
        self.assertFalse(health.model_available)
        self.assertIn("not installed", health.message)

    def test_configured_model_and_base_url_are_used(self):
        with override_settings(OLLAMA_BASE_URL="http://ollama:11434/", OLLAMA_MODEL="another:7b"):
            client = OllamaClient()
        with patch.object(client, "_request", return_value={"models": [{"model": "another:7b"}]}) as request:
            health = client.health_check()
        self.assertEqual(client.base_url, "http://ollama:11434")
        self.assertEqual(health.model, "another:7b")
        self.assertTrue(health.model_available)
        request.assert_called_once_with("/api/tags", timeout=3)

    def test_default_model_tag_alias(self):
        client = OllamaClient(model="qwen3")
        with patch.object(client, "_request", return_value={"models": [{"name": "qwen3:latest"}]}):
            self.assertTrue(client.health_check().model_available)

    def test_connection_failure_returns_disconnected_health(self):
        self.open.side_effect = URLError(ConnectionRefusedError())
        with self.assertLogs("notes.services.ollama_client", level="WARNING"):
            health = self.client_service.health_check()
        self.assertFalse(health.connected)
        self.assertFalse(health.model_available)
        self.assertIn("Start Ollama", health.message)

    def test_direct_chat_connection_failure_is_typed(self):
        self.open.side_effect = URLError("connection refused")
        with self.assertRaises(OllamaConnectionError):
            self.client_service.chat([{"role": "user", "content": "Hello"}])

    def test_timeout_errors_are_typed(self):
        for error in (TimeoutError(), URLError(TimeoutError())):
            with self.subTest(error=error):
                self.open.side_effect = error
                with self.assertRaises(OllamaTimeoutError):
                    self.client_service.chat([{"role": "user", "content": "Hello"}])
                with self.assertLogs("notes.services.ollama_client", level="WARNING"):
                    self.assertFalse(self.client_service.health_check().connected)

    def test_nonstreaming_chat_contract_preserves_unicode(self):
        self.respond({"done": True, "message": {"role": "assistant", "content": "স্বাগতম, student!"}})
        text = self.client_service.chat([{"role": "user", "content": "হ্যালো"}])
        self.assertEqual(text, "স্বাগতম, student!")
        request = self.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:11434/api/chat")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {
            "model": "qwen3:4b", "messages": [{"role": "user", "content": "হ্যালো"}], "stream": False,
        })
        self.assertEqual(self.open.call_args.kwargs["timeout"], 120)

    def test_http_and_api_errors(self):
        self.open.side_effect = HTTPError("http://localhost:11434/api/chat", 404, "Missing", {}, io.BytesIO())
        with self.assertRaisesMessage(OllamaResponseError, "HTTP 404"):
            self.client_service.chat([{"role": "user", "content": "Hello"}])
        self.open.side_effect = None
        self.respond({"error": "a model error"})
        with self.assertRaises(OllamaResponseError):
            self.client_service.chat([{"role": "user", "content": "Hello"}])

    def test_bad_json_and_chat_response_shape(self):
        for payload in ([], {"done": False, "message": {"content": "partial"}}, {"done": True}, {"done": True, "message": {"content": None}}):
            with self.subTest(payload=payload):
                self.respond(payload)
                with self.assertRaises(OllamaResponseError):
                    self.client_service.chat([{"role": "user", "content": "Hello"}])
        self.open.return_value.__enter__.return_value.read.return_value = b"not json"
        with self.assertRaises(OllamaResponseError):
            self.client_service.chat([{"role": "user", "content": "Hello"}])

    def test_invalid_health_response_does_not_claim_connected(self):
        for payload in ({"models": "invalid"}, {"models": [None]}, {}):
            self.respond(payload)
            with self.assertLogs("notes.services.ollama_client", level="WARNING"):
                self.assertFalse(self.client_service.health_check().connected)

    def test_invalid_chat_input_does_not_make_request(self):
        for messages in ([], None, [{"role": "invalid", "content": "Hello"}], [{"role": "user", "content": 3}]):
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                self.client_service.chat(messages)
        self.open.assert_not_called()

    def test_invalid_configuration(self):
        for base_url in ("", "file:///etc/passwd", "http://", "http://localhost:bad", "http://name:password@localhost", "http://localhost/?x=1"):
            with self.subTest(base_url=base_url), self.assertRaises(OllamaConfigurationError):
                OllamaClient(base_url=base_url)
        for timeout in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(timeout=timeout), self.assertRaises(OllamaConfigurationError):
                OllamaClient(timeout=timeout)
        with self.assertRaises(OllamaConfigurationError):
            OllamaClient(model="")

    def test_construction_does_not_contact_ollama(self):
        with patch("notes.services.ollama_client.build_opener") as build:
            OllamaClient()
            build.return_value.open.assert_not_called()
            self.assertEqual(build.call_args.args[0].proxies, {})

    def test_http_redirects_are_not_followed(self):
        self.assertIsNone(_NoRedirects().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_structured_requests_send_schema_with_thinking_disabled(self):
        self.respond({"done": True, "message": {"content": "{}"}})
        schema = {"type": "object"}
        self.client_service.chat([{"role": "user", "content": "Return JSON"}], response_schema=schema)
        payload = json.loads(self.open.call_args.args[0].data)
        self.assertEqual(payload["format"], schema)
        self.assertFalse(payload["think"])
        self.assertEqual(payload["options"]["temperature"], 0)
        self.assertEqual(payload["options"]["num_ctx"], 16384)


class OllamaDiagnosticTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="diagnostic-user")

    def setUp(self):
        self.url = reverse("notes:ollama_diagnostic")

    def test_login_required_without_contacting_service(self):
        with patch("notes.views.get_llm_client") as client:
            self.assertRedirects(self.client.get(self.url), f"/login/?next={self.url}")
        client.assert_not_called()

    def test_connected_model_status(self):
        self.client.force_login(self.user)
        with patch("notes.views.get_llm_client") as client:
            client.return_value.health_check.return_value = LLMHealth(True, "qwen3:4b", True, "Model is installed.")
            response = self.client.get(self.url)
            client.return_value.health_check.assert_called_once_with()
            client.return_value.chat.assert_not_called()
        self.assertContains(response, "Connected")
        self.assertContains(response, "qwen3:4b")
        self.assertContains(response, "Yes")
        self.assertContains(response, "This diagnostic does not generate notes.")

    def test_disconnected_status_renders_successfully(self):
        self.client.force_login(self.user)
        with patch("notes.views.get_llm_client") as client:
            client.return_value.health_check.return_value = LLMHealth(False, "qwen3:4b", message="Start Ollama.")
            response = self.client.get(self.url)
        self.assertContains(response, "Not Connected")
        self.assertContains(response, "Not checked")

    def test_configuration_errors_do_not_break_page(self):
        self.client.force_login(self.user)
        with patch("notes.views.get_llm_client", side_effect=OllamaConfigurationError("Invalid local URL.")):
            response = self.client.get(self.url)
        self.assertContains(response, "Not Connected")
        self.assertContains(response, "Invalid local URL.")

    def test_reachable_but_missing_model(self):
        self.client.force_login(self.user)
        with patch("notes.views.get_llm_client") as client:
            client.return_value.health_check.return_value = LLMHealth(True, "custom:4b", False, "Model missing.")
            response = self.client.get(self.url)
        self.assertContains(response, "Connected")
        self.assertContains(response, "custom:4b")
        self.assertContains(response, "Model missing.")

    def test_page_is_read_only_and_escapes_service_messages(self):
        self.client.force_login(self.user)
        with patch("notes.views.get_llm_client") as client:
            client.return_value.health_check.return_value = LLMHealth(False, "<script>", message="<script>")
            self.assertEqual(self.client.post(self.url).status_code, 405)
            response = self.client.get(self.url)
        self.assertNotContains(response, "<script>")
        self.assertContains(response, "&lt;script&gt;")
