# SaluteSpeech TTS API

FastAPI‑based text‑to‑speech service using the SaluteSpeech API from Sberbank.  
The service is deployed at: [https://asr.lourie.info](https://asr.lourie.info)

Interactive API documentation (Swagger UI) is available at:  
[https://asr.lourie.info/docs](https://asr.lourie.info/docs)

---

## 📁 Project Structure (on the server)

```/var/www/asr.lourie.info/
├── main.py # FastAPI application + SaluteSpeechClient
├── requirements.txt # Python dependencies
├── Dockerfile # Docker image recipe
├── docker-compose.yml # Docker Compose definition
├── russiantrustedca.pem # Custom CA bundle (for SaluteSpeech API)
├── secrets/ # These files are supplied separately directly to server
│ ├── salute_speech_api_url.txt # OAuth endpoint
│ ├── tts_url.txt # TTS synthesis endpoint
│ ├── scope.txt # OAuth scope (e.g., SALUTE_SPEECH_PERS)
│ ├── auth_key.txt # Basic Auth key (client_id:client_secret)
│ └── asr_api_key.txt # API key for authenticating requests to this service
└── .gitignore # Excludes secrets/, pycache, venv/, .env
```

---

### 🔐 Secrets explained

| Secret file | Environment variable (inside container) | Purpose |
|-------------|------------------------------------------|---------|
| `salute_speech_api_url.txt` | `SALUTE_SPEECH_API_URL_FILE` | OAuth token endpoint |
| `tts_url.txt`               | `TTS_URL_FILE`                         | TTS synthesis endpoint |
| `scope.txt`                 | `SCOPE_FILE`                           | OAuth scope |
| `auth_key.txt`              | `AUTH_KEY_FILE`                        | Basic auth credentials (Base64) |
| `asr_api_key.txt`           | `ASR_API_KEY_FILE`                     | API key for `X-API-Key` header |

These files are **mounted as Docker secrets** – they are never exposed in environment variables directly, only read via `get_secret()` in the code.

---

## 🐳 Deployment (Docker Compose)

The service runs in a Docker container. The `docker-compose.yml`:

```yaml
services:
  fastapi:
    image: ghcr.io/archelephant/asr.lourie.info:latest   # or local build
    restart: always
    secrets:
      - salute_speech_api_url
      - tts_url
      - scope
      - auth_key
      - asr_api_key
    environment:
      - SALUTE_SPEECH_API_URL_FILE=/run/secrets/salute_speech_api_url
      - TTS_URL_FILE=/run/secrets/tts_url
      - SCOPE_FILE=/run/secrets/scope
      - AUTH_KEY_FILE=/run/secrets/auth_key
      - ASR_API_KEY_FILE=/run/secrets/asr_api_key
      - CA_BUNDLE_PATH=/app/russiantrustedca.pem
    volumes:
      - ./russiantrustedca.pem:/app/russiantrustedca.pem:ro
    ports:
      - "127.0.0.1:8000:8000"
```


---

## Secret files
```
secrets:
  salute_speech_api_url: { file: ./secrets/salute_speech_api_url.txt }
  tts_url:               { file: ./secrets/tts_url.txt }
  scope:                 { file: ./secrets/scope.txt }
  auth_key:              { file: ./secrets/auth_key.txt }
  asr_api_key:           { file: ./secrets/asr_api_key.txt }
```

---

## Start the service:
```bash
docker compose up -d
```
---

## View logs:
```bash
docker compose logs -f fastapi
```

---

### 🧪 API Usage

All endpoints require the header:
```text
X-API-Key: <your_asr_api_key>
```

---

## Synthesize speech

Endpoint: ```POST /asr/synthesize```

Request body (JSON):
```json
{
  "text": "Привет, мир!",
  "voice": "Nec_24000",
  "format": "opus"
}
```

## Parameters:

| Field	| Type	| Default |	Description |
|-------|----------|------------|--------------------|
| text	| string	| –	| Text to synthesize (max 4000 characters) |
| voice |	string	| Ost_24000	| Voice model  (e.g., Nec_24000, Bys_24000) |
| format |	string |	opus |	Audio format: opus, wav16, pcm16, alaw, g729 |

**Full list of voices**: https://developers.sber.ru/docs/ru/salutespeech/guides/synthesis/voices
**SaluteSpeech documentation**: https://developers.sber.ru/docs/ru/salutespeech/overview

Response: 
```Binary audio data (Content-Type: audio/ogg for opus, audio/wav for wav16, etc.)```

Example curl
```bash
curl -X POST https://asr.lourie.info/asr/synthesize \
  -H "X-API-Key: your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"text":"Привет, мир!", "voice":"Nec_24000", "format":"wav16"}' \
  --output speech.wav
```

🔁 Continuous Deployment

    The Docker image is built and published to GitHub Container Registry via GitHub Actions (see .github/workflows/docker-publish.yml).

    Every push to the main branch triggers a new image build.

    On the server, a simple docker compose pull && docker compose up -d updates the running container (can be automated with a webhook or cron).

🛠 Development (local)

    Clone the repository.

    Create a virtual environment and install dependencies:
    bash

    python -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

    Set environment variables (or create .env file) with the same keys as the secrets.

    Run the FastAPI server:
    bash

    uvicorn main:app --reload

📄 License

```GNU General Public License (GPL)```
