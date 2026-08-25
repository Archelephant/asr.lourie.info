# TTS and ASR orchestration with demo frontend

A FastAPI‑based service that provides:

- **Speech-to-Text (ASR)** using a self‑hosted Whisper instance.
- **Text-to-Speech (TTS)** (placeholder for future implementation).
- **Demo Frontend** – a questionnaire frontend that collects user input and sends the enriched context to GigaChat.

The service is deployed at: [https://asr.lourie.info](https://asr.lourie.info)

Interactive API documentation (Swagger UI) is available at: [https://asr.lourie.info/docs](https://asr.lourie.info/docs)

---

## Architecture

Because SaluteSpeech API has been discontinued for private use on 15.07.2026, this service no longer depends on the SaluteSpeech API. The legacy 1.0 version is available at branch **archive/SaluteSpeech**.

Currently deployed service offers:

- **ASR**: Powered by a self‑hosted Whisper instance (`whisper.lourie.info`) with an OpenAI‑compatible API.
- **TTS**: Placeholder for a future self‑hosted TTS service (e.g., Kokoro, Qwen3‑TTS).

Authentication is done via a static **API Key** passed in the `X-API-Key` header.

---

The service is deployed at: [https://asr.lourie.info](https://asr.lourie.info)

Interactive API documentation (Swagger UI) is available at:  
[https://asr.lourie.info/docs](https://asr.lourie.info/docs)

---

## 📁 Project Structure (on the server)
```
├── main.py # FastAPI application + ASR/TTS clients + Demo Frontend
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── frontend/ # Frontend assets
│ ├── static/
│ │ ├── style.css
│ │ └── script.js
│ └── templates/
│ └── index.html
└── secrets/ # These files are supplied separately directly to server
├── asr_url.txt # Whisper ASR endpoint URL
├── tts_url.txt # TTS endpoint URL (future use)
├── asr_api_key.txt # API key for authenticating requests to this service
└── gigachat_credentials.txt # API key for authenticating requests to GigaChat API
```


---

## Secrets explained

| Secret file | Environment variable (inside container) | Purpose |
| :--- | :--- | :--- |
| `asr_url.txt` | `ASR_URL_FILE` | Whisper ASR endpoint URL |
| `tts_url.txt` | `TTS_URL_FILE` | TTS endpoint URL (future use) |
| `asr_api_key.txt` | `ASR_API_KEY_FILE` | API key for `X-API-Key` header |
| `gigachat_credentials.txt` | `GIGACHAT_CREDENTIALS_FILE` | API key for GigaChat API |

These files are mounted as Docker secrets – they are never exposed in environment variables directly, only read via `get_secret()` in the code.

---

## Deployment (Docker Compose)

The service runs in a Docker container. The `docker-compose.yml`:

```yaml
services:
  fastapi:
    image: ghcr.io/archelephant/asr.lourie.info:latest # or local build
    restart: always
    secrets:
      - asr_url
      - tts_url
      - asr_api_key
      - gigachat_credentials
    environment:
      - ASR_URL_FILE=/run/secrets/asr_url
      - TTS_URL_FILE=/run/secrets/tts_url
      - ASR_API_KEY_FILE=/run/secrets/asr_api_key
      - GIGACHAT_CREDENTIALS_FILE=/run/secrets/gigachat_credentials
    ports:
      - "127.0.0.1:8000:8000"

secrets:
  asr_url:
    file: ./secrets/asr_url.txt
  tts_url:
    file: ./secrets/tts_url.txt
  asr_api_key:
    file: ./secrets/asr_api_key.txt
  gigachat_credentials:
    file: ./secrets/gigachat_credentials.txt
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

Endpoint: ```POST /tts```

Currently not implemented, will return 501 Not Implemented.

## Recognize speech (ASR)

Endpoint: ```POST /asr```

Transcribes an audio file into text using the self‑hosted Whisper instance.

Request (multipart/form-data):
```
- file: audio file (WAV, MP3, OGG, etc.)
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

Example curl:

```bash
curl -X POST https://asr.lourie.info/asr \
  -H "X-API-Key: your_api_key_here" \
  -F "file=@speech.wav" \
  -F "language=ru-RU"
  ```

Note: "language=ru-RU" is optional. You may tweak this field depending on which ASR model you use.

Note: the size limit is set in the nginx server settings at 30 MBytes. However, the longer the file - the longer it takes for processing the sound.

## Audio-to-Audio (A2A) Pipeline

Currently not implemented - being reworked since SberDevices had discontinued serving SaluteSpeech on 15.07.2026.


## 🔁 Continuous Deployment ##

The Docker image is built and published to GitHub Container Registry via GitHub Actions (see .github/workflows/docker-publish.yml).

Every push to the main branch triggers a new image build.

On the server:

```bash
    docker compose down
    docker compose pull
    docker compose up -d
```
    

## 🛠 Development (local) ##

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


## 📄 License ##

```GNU General Public License (GPL)```
