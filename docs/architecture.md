# ClassNote AI architecture

## Implemented foundation

ClassNote AI is a modular Django monolith. The project package is config.
Six Django apps define domain boundaries without introducing premature models.

| Package | Responsibility |
| --- | --- |
| config | Settings, root routes, home page, liveness, ASGI/WSGI |
| accounts | Student account flows using Django authentication |
| courses | Course organization and future enrollment |
| lectures | Lecture metadata and future audio capture/storage |
| transcription | Future local audio preparation and speech recognition |
| notes | Future structured notes and semantic retrieval |
| ai_chat | Future local LLM gateway and lecture-grounded chat |

Only config currently exposes views. GET / renders a shared Django template.
GET /health/ returns JSON {"status":"ok"} without database or model access.
Both allow HEAD and reject write methods. Django admin uses built-in auth,
sessions, CSRF protection, middleware, and password validation. Student-facing
authentication flows are deferred.

SQLite stores Django's built-in tables. No domain tables or migrations are
needed yet. Domain apps retain standard migrations packages for later changes.

## Configuration and frontend

Settings load environment variables from .env without overriding process
environment. Debug defaults to true for development; a private secret key is
required when debug is false. Templates live in templates/, static source files
in static/, collected files in staticfiles/, and future uploads in media/.
The latter two are separate and ignored by version control.

Bootstrap 5.3.8 CSS and its license are stored locally. The home page needs
no JavaScript or external network connection. Add vanilla JavaScript or HTMX
only for implemented interactions that benefit from them.

## Planned processing boundaries — not implemented

The intended local pipeline is:

1. lectures validates and stores recorded or uploaded audio.
2. transcription services use FFmpeg to normalize audio and Silero VAD to
   identify speech before faster-whisper transcription.
3. notes calls a shared local LLM service to produce structured academic notes.
4. notes retrieval services embed lecture chunks with sentence-transformers
   and maintain FAISS indexes.
5. ai_chat retrieves authorized lecture context and calls the same LLM service
   to answer grounded questions.

All LLM calls must go through a service class: the planned
ai_chat/services/llm.py OllamaLLMService. Its public interface should accept
messages and return normalized text, hiding Ollama transport and response
details. Read OLLAMA_BASE_URL and OLLAMA_MODEL from settings; default model
qwen3:4b. Changing the model or implementation must not require view changes.
Do not implement the class until the AI phase.

Heavy processing belongs in services, not request handlers. A later Celery
and Redis integration will invoke those services with persisted job status,
bounded retries, and explicit failure handling. No workers are configured now.
Never initialize inference models at import time or during Django startup.

## Privacy and access

Lecture content must stay on the local deployment. Paid and hosted AI APIs
are prohibited. Download model weights explicitly during future setup, rather
than silently on requests. Confirm model licenses and hardware support when
selecting dependencies.

Every future lecture, transcript, note, retrieval, and chat query must enforce
user ownership or explicit course access. Uploaded media must use protected
delivery; debug media serving is a development convenience only. Validate file
types and sizes and safely execute FFmpeg with argument lists.

Production deployment needs separate HTTPS, host, cookie, static, media,
and worker configuration. The health endpoint intentionally reports process
liveness only; future readiness checks should be separate.

## Verification

Use Django's test runner. Current tests cover public pages, health JSON and
methods, local static assets, installed apps, environment parsing, authentication,
and admin access. Future inference tests should use service doubles rather than
loading models or reaching the network.
