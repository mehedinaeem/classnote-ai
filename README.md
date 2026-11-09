# ClassNote AI

A local-first Django application for turning classroom lectures in Bangla,
English, or mixed Bangla-English into structured academic notes.

## Current scope

This initial foundation includes Django 5.2 LTS, six domain apps, a home page,
a JSON health endpoint, SQLite, Django built-in authentication and admin,
environment configuration, local Bootstrap 5 styles, and automated tests.

Recording, uploads, transcription, note generation, semantic search, student
authentication screens, and chat are not implemented yet. No AI requests or
model downloads occur at startup.

## Quick start

Python 3.12 or 3.13 is a good baseline for future audio/ML dependencies.
The foundation also supports Python 3.14 with the pinned Django version.
Use Python 3.10 or newer for Django itself.

Run from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000/ and http://127.0.0.1:8000/health/.
The health endpoint returns `{"status":"ok"}`; it is a liveness check,
not a database or model readiness check.

Optional admin access:

```bash
python manage.py createsuperuser
```

Then visit http://127.0.0.1:8000/admin/.

## Configuration

Settings load the root `.env` using python-dotenv. Existing process environment
variables take precedence. See [.env.example](.env.example) for all variables.

- `DJANGO_DEBUG`: defaults to true for local development.
- `DJANGO_SECRET_KEY`: development fallback only when debug is enabled;
  required when debug is disabled. Set a unique private key for deployment.
- `DJANGO_ALLOWED_HOSTS`: comma-separated hosts, defaults to localhost.
- `DJANGO_TIME_ZONE`: defaults to Asia/Dhaka.
- `SQLITE_PATH`: defaults to db.sqlite3; parent directory must exist.
- `OLLAMA_BASE_URL`, `OLLAMA_MODEL`: reserved configuration only,
  defaulting to http://127.0.0.1:11434 and qwen3:4b.

Shared templates live in `templates/`; source static assets in `static/`.
Bootstrap 5.3.8 CSS is vendored with its MIT license, so rendering needs no CDN.
No JavaScript or HTMX is needed yet.
`collectstatic` writes to `staticfiles/`. Local media lives in `media/`.
Django serves media only in debug mode. Real lecture files will require
authenticated access before uploads are introduced.

This is a development configuration. Production hosting, HTTPS, secure cookie
settings, protected media delivery, and static serving need deployment setup.

## Validation

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
python manage.py collectstatic --noinput
```

Tests cover the home page, health response, HTTP methods, local assets,
app registration, environment parsing, built-in authentication, and admin access.

## Planned local stack

Future phases will add FFmpeg, faster-whisper, Silero VAD, Ollama
(default qwen3:4b), sentence-transformers, and FAISS. Celery and Redis will
follow for background work. These tools are intentionally absent from the
foundation requirements to avoid unnecessary ML installations and downloads.
Model weights will need an initial download and suitable local hardware.
No paid or remote AI API is permitted.

See [architecture](docs/architecture.md) and [coding rules](AGENTS.md).
# classnote-ai
