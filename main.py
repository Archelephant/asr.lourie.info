import os
import io
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Request, Form, File, UploadFile, status
from fastapi.responses import Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field
import uuid
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, BinaryIO
import requests
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import mimetypes
import logging
from logging.handlers import RotatingFileHandler

from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole, ChatCompletion

# Default system prompt for main application: IoT A2A
DEFAULT_SYSTEM_PROMPT = """Ты полезный ассистент, который отвечает на вопросы пользователя. 
Отвечай коротко и по сути, чтобы твой ответ длился не более 30 секунд."""

#Helper function to determine the MIME media type

def get_secret(env_var_name: str) -> Optional[str]:
    """Retrieve a secret either from a standard env var or from a file."""
    # For consistency it its strongly suggested to place the secrets locally
    # i.e. making sure that both your server and local installations match
    file_path = os.environ.get(f"{env_var_name}_FILE")
    if file_path and os.path.exists(file_path):
        with open(file_path, 'r') as f:
            return f.read().strip()
    return os.environ.get(env_var_name)

def get_config(key: str, required: bool = False, default: str = None) -> str:
    """Get config from environment, with optional default and required check."""
    value = get_secret(key) or os.getenv(key, default)
    if required and not value:
        raise ValueError(f"Missing required configuration: {key}")
    return value

# Load environment variables from .env (if present)
load_dotenv()
ASR_URL = get_secret("ASR_URL")
TTS_URL = get_secret("TTS_URL")
API_KEY = get_secret("API_KEY")
GIGACHAT_CREDENTIALS = get_secret("GIGACHAT_CREDENTIALS")

class SpeechServiceError(Exception):
    """Base exception for all speech service errors."""
    pass

# ------------------------------------------------------------
#  ASR Client (Whisper)
# ------------------------------------------------------------

class ASRClient:
    """
    Client for Whisper ASR service (OpenAI‑compatible API).
    Uses Basic Auth with the same AUTH_KEY as the rest of the system.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.asr_url = self._get_config("ASR_URL", required=True)
        self.auth_key = self._get_config("API_KEY", required=True)

        # Logging
        log_level = self._get_config("LOG_LEVEL", default="INFO").upper()
        self.logger = self._setup_logging(log_level)

        # Reusable session
        self.session = requests.Session()
        self.session.verify = True  # Use system CA bundle
        self.logger.info("ASRClient initialized (endpoint: %s)", self.asr_url)

    def _get_config(self, key: str, required: bool = False, default: str = None) -> str:
        value = self.config.get(key) or get_secret(key) or os.getenv(key, default)
        if required and not value:
            raise SpeechServiceError(f"Missing required configuration: {key}")
        return value

    def _setup_logging(self, level: str) -> logging.Logger:
        logger = logging.getLogger("ASRClient")
        logger.setLevel(level)
        if not logger.handlers:
            ch = logging.StreamHandler()
            ch.setLevel(level)
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            ch.setFormatter(formatter)
            logger.addHandler(ch)
        return logger

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _request_with_retries(self, method: str, url: str, **kwargs) -> requests.Response:
        """Make an HTTP request with automatic retries on network errors."""
        self.logger.debug("Request: %s %s", method, url)
        return self.session.request(method, url, **kwargs)

    def recognize_audio(
        self,
        file: BinaryIO,
        filename: str,
        language: str = "ru-RU",
        model: str = "large-v3-turbo",
        max_retries: int = 2,
    ) -> str:
        """
        Send audio file to Whisper and return transcribed text.

        :param file: Open file-like object (e.g., from UploadFile.file)
        :param filename: Original filename (used for MIME type detection)
        :param language: Language code (e.g., 'ru-RU' → 'ru')
        :param model: Whisper model name (must match the server's available models)
        :param max_retries: Number of retries on recoverable errors
        :return: Transcribed text
        """
        # Prepare authentication
        headers = {"Authorization": f"Basic {self.auth_key}"}

        # Read file content (the file object is already open)
        file_content = file.read()

        # Build multipart form‑data (OpenAI‑compatible)
        files = {
            "file": (filename, file_content, "audio/wav")  # adjust MIME if needed
        }
        data = {
            "model": model,
            "language": language.split("-")[0],  # 'ru-RU' → 'ru'
            "response_format": "json",
        }

        @retry(
            stop=stop_after_attempt(max_retries + 1),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type((
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.RetryError,
            )),
            reraise=True,
        )
        def _call():
            resp = self._request_with_retries(
                "POST",
                self.asr_url,
                headers=headers,
                files=files,
                data=data,
                timeout=(10, 120),  # connect, read
            )
            resp.raise_for_status()
            return resp.json()

        try:
            self.logger.info("Transcribing: %s (language: %s)", filename, language)
            result = _call()
            text = result.get("text", "").strip()
            self.logger.info("Transcription successful: %d chars", len(text))
            return text
        except requests.exceptions.HTTPError as e:
            self.logger.error("HTTP error: %d - %s", e.response.status_code, e.response.text)
            raise SpeechServiceError(f"Whisper ASR error: {e}") from e
        except Exception as e:
            self.logger.error("Unexpected error: %s", e)
            raise SpeechServiceError(f"ASR failed: {e}") from e

    def close(self):
        """Close the HTTP session."""
        self.session.close()
        self.logger.info("ASRClient closed")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

# ------------------------------------------------------------
#  TTS Client (stub for future service)
# ------------------------------------------------------------

class TTSClient:
    """
    Client for TTS service (to be implemented later).
    Currently raises NotImplementedError to prevent accidental use.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.tts_url = self._get_config("TTS_URL", required=True)
        self.auth_key = self._get_config("AUTH_KEY", required=True)

        log_level = self._get_config("LOG_LEVEL", default="INFO").upper()
        self.logger = self._setup_logging(log_level)

        self.session = requests.Session()
        self.session.verify = True
        self.logger.info("TTSClient initialized (stub) – endpoint: %s", self.tts_url)

    def _get_config(self, key: str, required: bool = False, default: str = None) -> str:
        value = self.config.get(key) or get_secret(key) or os.getenv(key, default)
        if required and not value:
            raise SpeechServiceError(f"Missing required configuration: {key}")
        return value

    def _setup_logging(self, level: str) -> logging.Logger:
        logger = logging.getLogger("TTSClient")
        logger.setLevel(level)
        if not logger.handlers:
            ch = logging.StreamHandler()
            ch.setLevel(level)
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            ch.setFormatter(formatter)
            logger.addHandler(ch)
        return logger

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _request_with_retries(self, method: str, url: str, **kwargs) -> requests.Response:
        self.logger.debug("Request: %s %s", method, url)
        return self.session.request(method, url, **kwargs)

    def synthesize_text(
        self,
        text: str,
        voice: str = "default",
        audio_format: str = "mp3",
        max_retries: int = 2,
    ) -> bytes:
        """
        Convert text to speech (stub – not yet implemented).
        Will be replaced with actual API calls when TTS service is ready.
        """
        # TODO: Implement actual TTS call.
        # The code below is a placeholder that shows the intended structure.
        # Uncomment and adapt when you have a real TTS endpoint.

        # headers = {"Authorization": f"Basic {self.auth_key}", "Content-Type": "application/json"}
        # payload = {"text": text, "voice": voice, "format": audio_format}
        #
        # @retry(...)
        # def _call():
        #     resp = self._request_with_retries("POST", self.tts_url, headers=headers, json=payload, timeout=(10, 30))
        #     resp.raise_for_status()
        #     return resp.content
        #
        # try:
        #     self.logger.info("Synthesizing text (%d chars, voice=%s)", len(text), voice)
        #     audio = _call()
        #     self.logger.info("Synthesis successful: %d bytes", len(audio))
        #     return audio
        # except Exception as e:
        #     self.logger.error("TTS error: %s", e)
        #     raise SpeechServiceError(f"TTS failed: {e}") from e

        raise NotImplementedError(
            "TTS synthesis is not yet implemented. "
            "Please set up a TTS service and update TTSClient.synthesize_text()."
        )

    def close(self):
        self.session.close()
        self.logger.info("TTSClient closed")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

class GigaChatClient:
    """Client for GigaChat API with retry, exponential backoff, and token management."""
    
    def __init__(self, credentials: str, ca_bundle_file: Optional[str] = None, scope: str = "GIGACHAT_API_PERS"):
        self.credentials = credentials
        self.ca_bundle_file = ca_bundle_file
        self.scope = scope
        self.logger = logging.getLogger("GigaChatClient")
        self._client = None
    
    def _get_client(self) -> GigaChat:
        """Get or create the GigaChat client instance."""
        if self._client is None:
            # Configure the client with SSL certificate if provided
            if self.ca_bundle_file and os.path.exists(self.ca_bundle_file):
                self._client = GigaChat(
                    credentials=self.credentials,
                    scope=self.scope,
                    ca_bundle_file=self.ca_bundle_file,
                    verify_ssl_certs=True,
                    timeout=30.0
                )
            else:
                # Fallback for development - disable SSL verification (not recommended for production)
                self.logger.warning("Using GigaChat without SSL certificate verification. This is not recommended for production.")
                self._client = GigaChat(
                    credentials=self.credentials,
                    scope=self.scope,
                    verify_ssl_certs=False,
                    timeout=30.0
                )
        return self._client
    
    def generate_response(self, text: str, system_prompt: str = DEFAULT_SYSTEM_PROMPT) -> str:
        """
        Generate a response from GigaChat based on the input text.
        Implements retry logic with exponential backoff.
        
        :param text: Input text to send to GigaChat
        :return: Generated response text
        :raises Exception: on unrecoverable errors
        """
        # Coerce to string if a list or other type is passed
        if isinstance(text, list):
            text = " ".join(text)
        elif not isinstance(text, str):
            text = str(text)

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_exception_type((
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.Timeout,
                requests.exceptions.RetryError
            )),
            reraise=True
        )
        def _call():
            client = self._get_client()
            messages = [
                Messages(
                    role=MessagesRole.SYSTEM,
                    content=system_prompt
                ),
                Messages(
                    role=MessagesRole.USER,
                    content=text
                    )
            ]
            chat = Chat(messages=messages)
            response = client.chat(chat)
            return response.choices[0].message.content
        
        try:
            self.logger.info(f"Generating GigaChat response for: {text[:50]}...")
            result = _call()
            self.logger.info(f"GigaChat generated: {result[:50]}...")
            return result
        except Exception as e:
            self.logger.error(f"Error generating GigaChat response: {e}")
            raise SaluteSpeechError(f"GigaChat generation failed: {e}") from e

    def close(self):
        """Close the underlying GigaChat client."""
        if self._client is not None:
            self._client.close()
            self._client = None


# Load environment (if .env exists)
from dotenv import load_dotenv
load_dotenv()

# Setup logging for FastAPI (optional, but good for production)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("asr-api")

# -----------------------------FAST API starts here--------------------------------------------------
app = FastAPI(
    title="ASR TTS API",
    description="ASR and Text-to-Speech with self-hosted Whisper and Kokoro",
    version="1.1"#,
    #lifespan=lifespan
)

#-------------------------Logging------------------------------------
# ---- 1. Configure rotating file logger ----
LOG_DIR = "logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)                 # create directory if missing
LOG_FILE = os.path.join(LOG_DIR, "app.log")

# Create a logger for our application
logger = logging.getLogger("app")
logger.setLevel(logging.INFO)

# Rotating file handler: 10 MB per file, keep 5 backups
handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=10_000_000,    # 10 MB
    backupCount=5           # keep 5 old files (total ~60 MB max)
)
formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
handler.setFormatter(formatter)
logger.addHandler(handler)

# Also log to console (optional) - useful during development
console = logging.StreamHandler()
console.setFormatter(formatter)
logger.addHandler(console)

# ---- 2. Middleware to time every request ----
class TimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.perf_counter()
        response = await call_next(request)
        process_time = time.perf_counter() - start_time

        # Log the request details
        logger.info(
            f"{request.method} {request.url.path} "
            f"status={response.status_code} "
            f"duration={process_time:.3f}s"
        )
        # Optionally add a header for client‑side debugging
        response.headers["X-Process-Time"] = f"{process_time:.3f}"
        return response

app.add_middleware(TimingMiddleware)
#-------------------------End logging--------------------------------

# Global clients (lazy‑initialized)
_asr_client: Optional[ASRClient] = None
_tts_client: Optional[TTSClient] = None
giga_client: Optional[GigaChatClient] = None

def get_asr_client() -> ASRClient:
    global _asr_client
    if _asr_client is None:
        _asr_client = ASRClient()
    return _asr_client


def get_tts_client() -> TTSClient:
    global _tts_client
    if _tts_client is None:
        _tts_client = TTSClient()
    return _tts_client


def get_giga_client() -> GigaChatClient:
    global giga_client
    if giga_client is None:
        credentials = get_secret("GIGACHAT_CREDENTIALS")
        if not credentials:
            raise SpeechServiceError("GIGACHAT_CREDENTIALS not configured")
        ca_bundle = get_secret("CA_BUNDLE_PATH")
        scope = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
        giga_client = GigaChatClient(
            credentials=credentials,
            ca_bundle_file=ca_bundle if ca_bundle and os.path.exists(ca_bundle) else None,
            scope=scope
        )
    return giga_client


@app.on_event("shutdown")
def shutdown_event():
    """Clean up clients on shutdown."""
    global _asr_client, _tts_client, giga_client
    if _asr_client:
        _asr_client.close()
    if _tts_client:
        _tts_client.close()
    if giga_client:
        giga_client.close()


# ------------------------------------------------------------
#  Health Check
# ------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "speech"}


# ------------------------------------------------------------
#  ASR Endpoint
# ------------------------------------------------------------

@app.post("/asr")
async def speech_to_text(
    file: UploadFile = File(...),
    language: str = Form("ru-RU"),
    model: str = Form("large-v3-turbo"),
):
    """
    Accept an audio file and return the transcribed text.
    Uses the local Whisper service via ASRClient.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    try:
        client = get_asr_client()
        text = client.recognize_audio(
            file.file,
            filename=file.filename,
            language=language,
            model=model,
        )
        return {"text": text, "language": language}
    except SpeechServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


# ------------------------------------------------------------
#  TTS Endpoint (stub)
# ------------------------------------------------------------

@app.post("/tts")
async def text_to_speech(
    text: str = Form(...),
    voice: str = Form("default"),
    audio_format: str = Form("mp3"),
):
    """
    Synthesize speech from text (currently a stub).
    Returns audio bytes once the TTS service is integrated.
    """
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    try:
        client = get_tts_client()
        audio_bytes = client.synthesize_text(text, voice=voice, audio_format=audio_format)
        # Once implemented, return the audio with proper Content-Type
        # return Response(audio_bytes, media_type=f"audio/{audio_format}")
        # For now, we raise a 501 to clearly indicate not implemented.
        raise HTTPException(
            status_code=501,
            detail="TTS service is not yet implemented. Please set up a TTS server."
        )
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except SpeechServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


# ------------------------------------------------------------
#  Optional: root and documentation
# ------------------------------------------------------------

@app.get("/")
async def root():
    return {
        "service": "Speech ASR/TTS",
        "endpoints": {
            "/asr": "POST – transcribe audio (multipart/form-data)",
            "/tts": "POST – synthesize speech (form data, stub)",
            "/health": "GET – health check",
        },
    }

#--------------------------Front-end----------------------------------------------------
# Mount static files (CSS, JS)
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

# Set up Jinja2 templates
templates = Jinja2Templates(directory="frontend/templates")

# Initial entry URL, I'll keep it for now
@app.get("/questionnaire", response_class=HTMLResponse)
async def get_questionnaire(request: Request):
    """Serve the questionnaire frontend."""
    #debug
    print(type(templates)) 
    print(templates.__dict__.keys()) 
    return templates.TemplateResponse(request=request, name="questionnaire.html", context={"request": request})

# Multi-step intro
@app.get("/", response_class=HTMLResponse)
async def get_index(request: Request):
    """Serve the new multi-step questionnaire."""
    return templates.TemplateResponse(request, "index.html", {"request": request})

#--------------------------Back-end------------------------------------------------------

# --- Questionnaire submission endpoint ---

@app.post("/submit_questionnaire")
async def submit_questionnaire(
    request: Request,
    gender: str = Form(...),
    age: str = Form(...),
    track: str = Form(...),
    goal: str = Form(...),
    goal_other: Optional[str] = Form(None),
    # Track-specific fields – we'll use dynamic keys; we can accept all as Form data
    # Better: use Request to get form data, but we can also use Form(...) for each.
    # However, to keep it flexible, we'll use `Request` to parse form data manually.
):
    """
    Receive all questionnaire data, enrich context, and call GigaChat.
    """
    try:
        giga_client = get_giga_client()
    except SpeechServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # Parse form data
    form_data = await request.form()
    # Convert to dict
    data = {k: v for k, v in form_data.items()}

    # Extract common fields
    gender = data.get("gender")
    age = data.get("age")
    track = data.get("track")
    goal = data.get("goal")
    goal_other = data.get("goal_other")

    # Extract track-specific answers – we know the field names from trackData
    # For past track:
    if track == "past":
        event_name = data.get("event_name", "")
        age_at_moment = data.get("age_at_moment", "")
        place_action = data.get("place_action", "")
        light_weather = data.get("light_weather", "")
        light_weather_other = data.get("light_weather_other", "")
        # If light_weather is "другое", use the other value
        if light_weather == "другое" and light_weather_other:
            light_weather = light_weather_other
        else:
            light_weather = "не помню"
        # Same with goal
        if goal == "другое" and goal_other:
            goal = goal_other
        else:
            goal = "просто вспомнить"
        # Build user message
        user_message = (
            f"""Я хочу заново пережить опыт {event_name} из прошлого c целью {goal}. 
            Это было в {place_action}, в тот момент мне было {age_at_moment}.
            Помню, что тогда было {light_weather}.
            Сейчас мне {age} лет, я {gender}."""
        )
    elif track == "present":
        present_role = data.get('present_role')
        present_place = data.get('present_place')
        time_day = data.get('time_day')
        one_thing = data.get('one_thing')
        light_weather = data.get("light_weather", "")
        light_weather_other = data.get("light_weather_other", "")
        # If light_weather is "другое", use the other value
        if light_weather == "другое" and light_weather_other:
            light_weather = light_weather_other
        else:
            light_weather = "не важно"
        # Same with goal
        if goal == "другое" and goal_other:
            goal = goal_other
        else:
            goal = "самоосмысления"
        # Build user message
        user_message = (
            f"""Я хочу посмотреть на себя со стороны c целью {goal}. Мне {age} лет, я {gender}.
            Я сейчас нахожусь в {present_place}, где играю роль {present_role}. 
            Смоделируй ситуацию, которая происходит {time_day} в условиях {light_weather}.
            """
        )
    elif track == "future":
        future_date = data.get('future_date')
        future_role = data.get('future_role')
        future_place = data.get('future_place')
        future_action = data.get('future_action')
        light_weather = data.get("light_weather", "")
        light_weather_other = data.get("light_weather_other", "")
        # If light_weather is "другое", use the other value
        if light_weather == "другое" and light_weather_other:
            light_weather = light_weather_other
        else:
            light_weather = "не важно"
        # Same with goal
        if goal == "другое" and goal_other:
            goal = goal_other
        else:
            goal = "проектирования пути развития"
        # Build user message
        user_message = (
            f"""Я хочу визуализировать свое вероятное будущее c целью {goal}. Сейчас мне {age} лет, я {gender}.
            Я заглянуть во время {future_date} и {future_place}, где буду играть роль {future_role} и делать {future_action}. 
            Смоделируй ситуацию, которая происходит в условиях {light_weather}.
            """
        )
    else:
        raise HTTPException(status_code=400, detail="Invalid track")

    # --- 2. TODO: Enrich context (your logic here) ---
    enriched_prompt = user_message  # Placeholder – replace with your enrichment

    # --- Log the enriched prompt (truncated) ---
    logger.info(f"TRACK: {track}; GOAL: {goal}; ENRICHED_PROMPT: \n{enriched_prompt}")

    # --- 3. Call GigaChat ---
    director_prompt = """
    Ты - сценарист-психолог.
    Твоя задача - сгенерировать сценарий видеоролика, который впоследствии будет использован нейросетью для генерации видео.
    Четко опиши каждую сцену и дай указания нейросети, как сгенерировать видео.
    Не используй имен собственных, если пользователь их в явном виде не сообщил.
    Помни, что нейросетевые видео ограничены по времени.
    """
    start = time.perf_counter()

    try:
        response_text = giga_client.generate_response(enriched_prompt, system_prompt=director_prompt)
    except Exception as e:
        duration = time.perf_counter() - start
        logger.error(f"GigaChat error after {duration:.3f}s: {e}")
        raise HTTPException(status_code=500, detail=f"GigaChat error: {str(e)}")
    duration = time.perf_counter() - start
    logger.info(f"LLM call completed in {duration:.3f}s")
    logger.info(f"LLM response:{response_text}")

    # --- 4. Return the response ---
    return {"response": response_text}


# A2A Endpoint To be added later------------------------