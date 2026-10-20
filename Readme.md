# ASR and TTS orchestration with a demo frontend

A FastAPI-based service that provides:

- **Speech-to-Text (ASR)** using a self-hosted Whisper instance.
- **Text-to-Speech (TTS)** (placeholder for future implementation).
- **Demo Frontend** – a questionnaire that collects a visitor's input and sends the enriched context to GigaChat.

The service is deployed at: [https://asr.lourie.info](https://asr.lourie.info)

Interactive API documentation (Swagger UI) is available at: [https://asr.lourie.info/docs](https://asr.lourie.info/docs)

---

## Architecture

Because the SaluteSpeech API has been discontinued for private use, this service no longer depends on it. The legacy 1.0 version is available at branch **archive/SaluteSpeech**.

Currently deployed service offers:

- **ASR**: powered by a self-hosted Whisper instance with an OpenAI-compatible API.
- **TTS**: placeholder for a future self-hosted TTS service (e.g. Kokoro, Qwen3-TTS).

---

## 📁 Project Structure

```
├── main.py               # FastAPI application + ASR/TTS clients + demo frontend
├── requirements.txt      # Runtime dependencies
├── requirements-dev.txt  # Development dependencies (tests)
├── Dockerfile
├── docker-compose.yml
├── pytest.ini            # Test runner configuration
├── test.sh               # Pre-deployment smoke test
├── frontend/             # Frontend assets
│ ├── static/
│ │ ├── style.css
│ │ └── script.js
│ └── templates/
│ └── index.html
├── tests/                # Automated test suite
│ ├── conftest.py        # Shared fixtures: isolated environment, upstream stubs
│ ├── test_config_resolution.py
│ ├── test_authentication.py
│ ├── test_asr_endpoint.py
│ ├── test_questionnaire_prompt.py
│ ├── test_questionnaire_files.py
│ ├── test_readiness_logging.py
│ └── test_isolation.py
├── test/fixtures/        # Audio fixture used by the smoke test
├── openspec/             # Specifications and change history
└── .opencode/            # Shared agent tooling
```

---

## 🔐 Configuration

Every value the service reads is listed here. Each one can be supplied either
as a plain environment variable or as a file-mounted secret, through the
matching `<NAME>_FILE` variable. The file-mounted form wins.

| Variable | Purpose | Required |
| :--- | :--- | :--- |
| `ASR_URL` | Endpoint of the upstream speech-to-text service | yes |
| `ASR_UPSTREAM_KEY` | Credential presented to the upstream speech service | yes |
| `ASR_API_KEY` | API key that callers present in `X-API-Key` | yes |
| `TTS_URL` | Endpoint of the TTS service | no (TTS is a stub) |
| `GIGACHAT_CREDENTIALS` | GigaChat credentials | yes |
| `SCOPE` | GigaChat OAuth scope (`GIGACHAT_API_PERS`) | no |
| `GIGACHAT_MODEL` | Model for text generation | no |
| `GIGACHAT_VISION_MODEL` | Model used when an image is attached | no |
| `CA_BUNDLE_PATH` | Path to a CA bundle for SSL verification | no |
| `UPLOAD_ASR_LANGUAGE` | Language used when transcribing an uploaded audio file | no |
| `UPLOAD_ASR_MODEL` | Model used when transcribing an uploaded audio file | no |
| `LOG_LEVEL` | `INFO` or `DEBUG` | no |
| `VERBOSE_LOGGING` | When enabled, the visitor's own words are written to the log. On by default | no |
| `LOG_DIR` | Directory for the log file, default `logs` | no |

### The two key roles

`ASR_API_KEY` and `ASR_UPSTREAM_KEY` are **different secrets with different
jobs**, and should not be the same value:

- `ASR_API_KEY` is what *your* clients present to *you*. You generate it and
  hand it out.
- `ASR_UPSTREAM_KEY` is the credential of the *upstream speech service*. You
  obtain it from whoever runs that service.

Using one value for both would mean every client of your service also holds
the key to the upstream service. Keep them apart.

For a deployment that predates this separation, the historical names
`API_KEY` and `AUTH_KEY` are still accepted and still serve both roles, so
such a deployment keeps working without renaming anything. Set
`ASR_UPSTREAM_KEY` explicitly to separate the two.

---

## 🔑 Generating your own API key

The inbound key is a random value that only you and your clients know:

```bash
openssl rand -hex 32
```

Put the result in the file that holds the inbound key (see below) and give it
to your clients. To change it later, replace the file's contents and restart
the container.

---

## 📦 Installing the service

### From a clone of this repository

```bash
git clone https://github.com/Archelephant/asr.lourie.info.git
cd asr.lourie.info
```

### Local development

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt          # runtime
pip install -r requirements-dev.txt      # plus the test tooling
```

Create your configuration file and fill in the values it asks for:

```bash
cp .env.example .env
$EDITOR .env
```

Run the service:

```bash
uvicorn main:app --reload
```

It is now on [http://127.0.0.1:8000](http://127.0.0.1:8000), with the API
documentation at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

The reloader prints `change detected` for changes it does not act on — the
compiled bytecode and the log file are rewritten while the service runs. It
only restarts the server for a change to a `.py` file, so these messages are
noise rather than a restart loop.

### On a server, with Docker Compose

The service runs in a container. `docker-compose.yml` mounts the secrets
listed below and passes them to the container as files:

```bash
docker compose up -d
docker compose logs -f fastapi
```

---

## 🔒 Delivering the keys to a server

The secrets are **files**, not environment variables. Create a `secrets/`
directory next to `docker-compose.yml` and put one file per secret:

```bash
mkdir -p secrets
chmod 700 secrets

printf '%s' "$UPSTREAM_KEY" > secrets/asr_upstream_key.txt
printf '%s' "$YOUR_OWN_KEY" > secrets/asr_api_key.txt
printf '%s' "$GIGACHAT_CREDENTIALS" > secrets/gigachat_credentials.txt
printf '%s' 'https://whisper.lourie.info/v1/audio/transcriptions' > secrets/asr_url.txt
printf '%s' 'GIGACHAT_API_PERS' > secrets/scope.txt

# Readable only by the owner.
chmod 600 secrets/*.txt
```

The `secrets/` directory is listed in `.gitignore`, so the files never reach
the repository. The container receives them under `/run/secrets/`, and the
service reads them from there — they are never exposed as plain environment
variables.

| File | Mounted as | Purpose |
| :--- | :--- | :--- |
| `asr_url.txt` | `ASR_URL_FILE` | Upstream speech-to-text endpoint |
| `asr_upstream_key.txt` | `ASR_UPSTREAM_KEY_FILE` | Credential for the upstream speech service |
| `asr_api_key.txt` | `ASR_API_KEY_FILE` | API key callers present in `X-API-Key` |
| `gigachat_credentials.txt` | `GIGACHAT_CREDENTIALS_FILE` | GigaChat credentials |
| `scope.txt` | `SCOPE_FILE` | GigaChat OAuth scope |
| `tts_url.txt` | `TTS_URL_FILE` | TTS endpoint (future use) |

To change a secret, replace the file's contents and recreate the container:

```bash
docker compose up -d --force-recreate
```

> `docker compose up` fails outright if a file listed under `secrets:` is
> missing. If you are upgrading from a deployment that used different file
> names, create the new files first — the old ones can stay until the service
> has been verified.

---

## 🔌 API

Endpoints that forward a request onward to another service require the API
key. Endpoints that answer locally do not.

| Endpoint | Method | Key | Purpose |
| :--- | :--- | :--- | :--- |
| `/asr` | POST | required | Transcribe an audio file |
| `/tts` | POST | required | Synthesize speech (not implemented: `501`) |
| `/submit_questionnaire` | POST | not required | The demo questionnaire |
| `/health` | GET | not required | Liveness: the process is running |
| `/ready` | GET | not required | Readiness: the dependencies are usable |
| `/` | GET | not required | The demo questionnaire page |
| `/static/*` | GET | not required | Frontend assets |

The key is passed in the `X-API-Key` header:

```
X-API-Key: <your_asr_api_key>
```

A request without a valid key is answered with `401`. A request that reaches
a forwarding endpoint while the service itself is misconfigured is answered
with `503`, so the two are never confused.

The demo questionnaire is deliberately left open: a browser cannot hold a
secret without handing it to every visitor.

### Recognize speech (ASR)

Endpoint: `POST /asr`

Request (multipart/form-data):

```
- file: audio file (WAV, MP3, OGG, FLAC, ...)
- language (optional): language code, default ru-RU
- model (optional): Whisper model, default large-v3-turbo
```

Response (200 OK):

```json
{
  "text": "transcribed text from the audio",
  "language": "ru-RU"
}
```

Example:

```bash
curl -X POST https://asr.lourie.info/asr \
  -H "X-API-Key: your_api_key_here" \
  -F "file=@speech.wav" \
  -F "language=ru-RU"
```

The file's media type is derived from the file itself, so an MP3 is forwarded
as an MP3 rather than being mislabelled as a WAV.

### Synthesize speech (TTS)

Endpoint: `POST /tts`

Not implemented yet; it answers `501 Not Implemented`.

### Audio-to-Audio (A2A) Pipeline

Not implemented.

---

## ✅ Checking the service

### Automated tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite never touches the network and never spends anyone's quota: the
upstream speech service and GigaChat are replaced by stubs. It also runs
from a clean checkout without a `.env` — a developer's own secrets cannot
influence the result.

### The pre-deployment check

`test.sh` checks a *running* service over the network: the local endpoints,
the key enforcement, a real transcription and a real questionnaire
submission. Run it before deploying a new image:

```bash
# against a local instance
ASR_BASE_URL=http://127.0.0.1:8000 ./test.sh

# against a deployed instance
ASR_BASE_URL=https://asr.lourie.info ./test.sh
```

The key is read from the environment or from an untracked `.env`; it is not
stored in the script, and it is not visible in the process list:

```bash
export ASR_API_KEY='your key'
./test.sh
```

The script may be run as `./test.sh`, `bash test.sh` or `sh test.sh` — under a
plain POSIX shell it re-executes itself with bash. It needs `curl`, and
either `jq` or `python3` to read the JSON responses.

The script exits non-zero if any check fails, and prints the expected and the
actual status for each one — so it can gate a deployment:

```
Public surface (no key required)
  PASS  GET /health (HTTP 200)
  PASS  GET / (HTTP 200)
  PASS  GET /static/script.js (HTTP 200)
Forwarding endpoints (key required)
  FAIL  POST /asr without key: expected HTTP 401, got HTTP 200
...
Result: 6 passed, 2 failed
```

### Reading the startup log

At startup the service reports what it resolved and whether its dependencies
answer. Configuration values are named, never shown:

```
SELF-TEST [config]: inbound API key        resolved from ASR_API_KEY
SELF-TEST [config]: upstream API key       resolved from ASR_UPSTREAM_KEY
SELF-TEST [asr/credential]: PASS – the upstream service accepted the credential from ASR_UPSTREAM_KEY (HTTP 405 in 0.05s)
SELF-TEST [gigachat/auth]: PASS – access token received in 0.42s (scope=GIGACHAT_API_PERS, model=GigaChat-2)
```

A missing configuration is fatal — the container restarts and says which
value is missing. An unreachable dependency only degrades the service: the
container starts, and the affected endpoints answer `503` until it recovers.

### What the log contains

`logs/app.log` holds two kinds of record.

**Always:** the method, path, status and duration of every request, and what
the service resolved at startup.

**When `VERBOSE_LOGGING` is on — which it is by default:** the visitor's
own words. The answers they submitted, the prompt built from them, and the
model's reply. This service runs as a test instance and keeps that content
for later analysis.

To keep the visitor's words out of the log, set:

```bash
VERBOSE_LOGGING=false
```

Credentials are never written to the log, whatever that setting says. The
log directory is excluded from version control, so its contents stay on the
server.

---

## 🔁 Continuous Deployment

The Docker image is built and published to GitHub Container Registry by
GitHub Actions (see `.github/workflows/docker-publish.yml`). Every push to
the `main` branch triggers a new image build.

On the server:

```bash
docker compose pull
docker compose up -d
./test.sh    # must pass before the new image is considered good
```

---

## 📄 License

```GNU General Public License (GPL)```
