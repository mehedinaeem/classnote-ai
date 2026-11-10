"""Ollama's local HTTP API, isolated behind the LLMClient interface."""
import http.client
import json
import logging
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from django.conf import settings

from .interfaces import LLMHealth, LLMServiceError

logger = logging.getLogger(__name__)


class OllamaError(LLMServiceError):
    pass


class OllamaConfigurationError(OllamaError):
    pass


class OllamaConnectionError(OllamaError):
    pass


class OllamaTimeoutError(OllamaError):
    pass


class OllamaResponseError(OllamaError):
    pass


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A local inference request should not be forwarded to another server.
        return None


class OllamaClient:
    def __init__(self, *, base_url=None, model=None, timeout=None, health_timeout=None):
        self.base_url = (settings.OLLAMA_BASE_URL if base_url is None else base_url).rstrip("/")
        self.model = settings.OLLAMA_MODEL if model is None else model
        self.timeout = settings.OLLAMA_TIMEOUT_SECONDS if timeout is None else timeout
        self.health_timeout = settings.OLLAMA_HEALTH_TIMEOUT_SECONDS if health_timeout is None else health_timeout
        try:
            parsed = urlsplit(self.base_url)
            valid = (
                parsed.scheme in ("http", "https") and parsed.hostname
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
            )
            # Accessing port also validates invalid/non-numeric port values.
            parsed.port
        except ValueError as exc:
            raise OllamaConfigurationError("OLLAMA_BASE_URL must be a valid local HTTP URL.") from exc
        if not valid:
            raise OllamaConfigurationError("OLLAMA_BASE_URL must be a valid local HTTP URL.")
        if not isinstance(self.model, str) or not self.model.strip():
            raise OllamaConfigurationError("OLLAMA_MODEL must name a locally installed model.")
        self.model = self.model.strip()
        for value in (self.timeout, self.health_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise OllamaConfigurationError("Ollama timeouts must be positive finite seconds.")
        # Local HTTP must not accidentally use an environment-configured proxy.
        self._opener = build_opener(ProxyHandler({}), _NoRedirects())

    def _request(self, endpoint, *, payload=None, timeout):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + endpoint, data=body, headers=headers,
            method="POST" if body is not None else "GET",
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise OllamaResponseError(
                f"Local Ollama returned HTTP {status}. Check that Ollama is running and the model is installed."
            ) from exc
        except TimeoutError as exc:
            raise OllamaTimeoutError("Local Ollama did not respond before the timeout.") from exc
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise OllamaTimeoutError("Local Ollama did not respond before the timeout.") from exc
            raise OllamaConnectionError(
                "Could not connect to local Ollama. Start Ollama and check OLLAMA_BASE_URL."
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise OllamaConnectionError("The local Ollama connection failed. Please try again.") from exc
        except (ValueError, UnicodeError) as exc:
            raise OllamaResponseError("Local Ollama returned an invalid JSON response.") from exc
        if not isinstance(result, dict):
            raise OllamaResponseError("Local Ollama returned an unexpected response.")
        if result.get("error"):
            raise OllamaResponseError("Local Ollama reported an error. Check the installed model and server logs.")
        return result

    def health_check(self):
        """Check API reachability and local model availability without inference."""
        try:
            data = self._request("/api/tags", timeout=self.health_timeout)
            models = data.get("models")
            if not isinstance(models, list) or any(not isinstance(item, dict) for item in models):
                raise OllamaResponseError("Local Ollama returned an invalid model list.")
            names = {
                name for item in models for name in (item.get("name"), item.get("model"))
                if isinstance(name, str)
            }
            configured = self.model if ":" in self.model.rsplit("/", 1)[-1] else self.model + ":latest"
            available = self.model in names or configured in names
            return LLMHealth(
                connected=True, model=self.model, model_available=available,
                message="Configured model is installed." if available else (
                    "Ollama is reachable, but the configured model is not installed."
                ),
            )
        except OllamaError as exc:
            logger.warning("Ollama health check failed: %s", type(exc).__name__)
            return LLMHealth(connected=False, model=self.model, message=str(exc))

    def chat(self, messages, *, response_schema=None):
        """Return local text; optionally constrain output with a JSON schema."""
        if not isinstance(messages, (list, tuple)) or not messages:
            raise ValueError("Provide at least one chat message.")
        normalized = []
        for message in messages:
            if (
                not isinstance(message, dict)
                or message.get("role") not in ("system", "user", "assistant")
                or not isinstance(message.get("content"), str)
            ):
                raise ValueError("Chat messages require a role and text content.")
            normalized.append({"role": message["role"], "content": message["content"]})
        payload = {"model": self.model, "messages": normalized, "stream": False}
        if response_schema is not None:
            payload.update({
                "format": response_schema, "think": False,
                "options": {"temperature": 0, "num_ctx": settings.OLLAMA_CONTEXT_SIZE},
            })
        data = self._request(
            "/api/chat", payload=payload,
            timeout=self.timeout,
        )
        message = data.get("message")
        if (
            data.get("done") is not True or not isinstance(message, dict)
            or not isinstance(message.get("content"), str)
        ):
            raise OllamaResponseError("Local Ollama returned an incomplete chat response.")
        return message["content"]
