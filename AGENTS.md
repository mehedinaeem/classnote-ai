# ClassNote AI architecture and coding rules

## Scope and structure

Use Python, Django 5.2 LTS, Django templates, Bootstrap 5, vanilla JavaScript,
and HTMX only when it simplifies a concrete interaction. SQLite is the
development database. Use Django built-in authentication.

- config: project settings, routing, ASGI/WSGI, public home and health views.
- accounts: future student account flows around Django authentication.
- courses: future courses and enrollment.
- lectures: future lecture metadata, recording, and audio uploads.
- transcription: future FFmpeg, Silero VAD, and faster-whisper integration.
- notes: future structured academic notes and retrieval indexes.
- ai_chat: future lecture-grounded chat and shared local LLM service.

The domain apps are currently scaffolds. Do not imply unimplemented features
are available. Do not add AI, transcription, or background workers unless tasked.

## Coding rules

- Use free, local/open-source processing only. Never add OpenAI, Gemini,
  Claude, AssemblyAI, Deepgram, or another paid or hosted AI API.
- All future LLM calls must go through a service class, planned as
  ai_chat/services/llm.py with an OllamaLLMService. The default is qwen3:4b,
  configured through Django settings. Views and tasks must not call Ollama directly.
- Keep views thin. Put domain orchestration and external/local process
  integrations in app-local services; keep persistence in models.
- Never load models or start background work on module import or AppConfig.ready.
- Use explicit imports, readable names, Django conventions, and small modules.
- Reference users through settings.AUTH_USER_MODEL in model relations.
- Enforce ownership before serving lecture audio, transcripts, notes, or chat.
- Validate future uploads, enforce limits, and invoke FFmpeg without shell=True.
- Keep secrets in environment variables. Do not commit .env, databases, media,
  model weights, or search indexes.
- Commit schema migrations with model changes. Avoid speculative schemas.
- Keep Bootstrap and other browser dependencies local and retain licenses.
- Add meaningful tests for behavior and regressions; mock model services and
  avoid network access or real model downloads in tests.
- Future Celery tasks should delegate to services and support safe retries.
- Document setup and configuration changes in README.md and architecture docs.

## Checks

Before completing code changes, run:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test
```

Run migrations when schema changes or initial setup require them.
