import os
import io
import hmac
import logging
import mimetypes
import time
from contextlib import asynccontextmanager
from typing import Any, BinaryIO, Dict, List, Optional
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole
from starlette.middleware.base import BaseHTTPMiddleware
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

# Default system prompt for main application: IoT A2A
DEFAULT_SYSTEM_PROMPT = """Ты полезный ассистент, который отвечает на вопросы пользователя. 
Отвечай коротко и по сути, чтобы твой ответ длился не более 30 секунд."""

#Helper function to determine the MIME media type

def get_secret(env_var_name: str) -> Optional[str]:
    """Retrieve a secret either from a standard env var or from a file."""
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

# ------------------------------------------------------------
#  Configuration resolution
# ------------------------------------------------------------
# Each role lists the environment variable names it accepts, most specific
# first. For every name the file-mounted form (<NAME>_FILE) wins over the
# plain environment variable.
#
# The two key roles are deliberately separate:
#   - the inbound key is presented by callers of THIS service in X-API-Key
#   - the upstream key is presented by THIS service to the speech backend
# Keeping them apart lets an operator issue their own inbound key without
# handing the upstream credential to their own clients. The historical names
# stay in both lists so a deployment written before the Whisper rework keeps
# working without renaming any secret on disk.

INBOUND_API_KEY_NAMES = ("ASR_API_KEY", "API_KEY", "AUTH_KEY")
UPSTREAM_API_KEY_NAMES = ("ASR_UPSTREAM_KEY", "ASR_API_KEY", "API_KEY", "AUTH_KEY")

UPSTREAM_URL_NAMES = ("ASR_URL",)
TTS_URL_NAMES = ("TTS_URL",)
GIGACHAT_CREDENTIAL_NAMES = ("GIGACHAT_CREDENTIALS",)
GIGACHAT_SCOPE_NAMES = ("SCOPE",)
CA_BUNDLE_NAMES = ("CA_BUNDLE_PATH",)
LOG_LEVEL_NAMES = ("LOG_LEVEL",)
VERBOSE_LOGGING_NAMES = ("VERBOSE_LOGGING",)
VISION_MODEL_NAMES = ("GIGACHAT_VISION_MODEL",)
GIGACHAT_MODEL_NAMES = ("GIGACHAT_MODEL",)
UPLOAD_ASR_LANGUAGE_NAMES = ("UPLOAD_ASR_LANGUAGE",)
UPLOAD_ASR_MODEL_NAMES = ("UPLOAD_ASR_MODEL",)

# Models that can read an image. The default text model cannot: it answers
# "Model does not support image", so a request that carries an image names
# one of these instead.
IMAGE_CAPABLE_MODELS = (
    "GigaChat-2-Pro",
    "GigaChat-2-Max",
    "GigaChat-Pro",
    "GigaChat-Pro-preview",
)


def _is_truthy(value: Optional[str]) -> bool:
    """
    Whether a configuration value means "on".

    Accepts the spellings a person is likely to write in a .env file:
    1, true, yes, on — in any case. Anything else, including an empty
    value, means "off".
    """
    if not value:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def resolve_config(names, required: bool = False, default: Optional[str] = None):
    """
    Resolve a configuration value from an ordered list of environment variable
    names.

    Returns (value, name) where `name` is the variable the value came from.
    When the built-in default is used, `name` is None — the caller is
    expected to say so rather than print "None". The name is safe to log;
    the value is not.
    """
    for name in names:
        value = get_secret(name)
        if value:
            return value, name
    if required:
        raise ValueError(
            "Missing required configuration: " + ", ".join(names)
        )
    return default, None


def resolve_inbound_api_key(required: bool = False):
    """The key callers present in the X-API-Key header."""
    return resolve_config(INBOUND_API_KEY_NAMES, required=required)


def resolve_upstream_api_key(required: bool = False):
    """The credential presented to the upstream speech service."""
    return resolve_config(UPSTREAM_API_KEY_NAMES, required=required)


# Load environment variables from .env (if present)
load_dotenv()
ASR_URL, ASR_URL_NAME = resolve_config(UPSTREAM_URL_NAMES)
TTS_URL, TTS_URL_NAME = resolve_config(TTS_URL_NAMES)
GIGACHAT_CREDENTIALS, GIGACHAT_CREDENTIALS_NAME = resolve_config(GIGACHAT_CREDENTIAL_NAMES)
SCOPE, SCOPE_NAME = resolve_config(GIGACHAT_SCOPE_NAMES, default="GIGACHAT_API_PERS")
CA_BUNDLE_PATH, CA_BUNDLE_PATH_NAME = resolve_config(CA_BUNDLE_NAMES)
LOG_LEVEL, LOG_LEVEL_NAME = resolve_config(LOG_LEVEL_NAMES, default="INFO")
# Verbose logging is on by default: this service runs as a test instance
# and the questionnaire content and the model's reply are kept in the log
# for later analysis. Set VERBOSE_LOGGING=false to turn it off.
VERBOSE_LOGGING, VERBOSE_LOGGING_NAME = resolve_config(
    VERBOSE_LOGGING_NAMES, default="true"
)
VERBOSE_LOGGING = _is_truthy(VERBOSE_LOGGING)
VISION_MODEL, VISION_MODEL_NAME = resolve_config(
    VISION_MODEL_NAMES, default="GigaChat-2-Pro"
)
GIGACHAT_MODEL, GIGACHAT_MODEL_NAME = resolve_config(
    GIGACHAT_MODEL_NAMES, default="GigaChat-2"
)
UPLOAD_ASR_LANGUAGE, UPLOAD_ASR_LANGUAGE_NAME = resolve_config(
    UPLOAD_ASR_LANGUAGE_NAMES, default="ru-RU"
)
UPLOAD_ASR_MODEL, UPLOAD_ASR_MODEL_NAME = resolve_config(
    UPLOAD_ASR_MODEL_NAMES, default="large-v3-turbo"
)
INBOUND_API_KEY, INBOUND_API_KEY_NAME = resolve_inbound_api_key()
UPSTREAM_API_KEY, UPSTREAM_API_KEY_NAME = resolve_upstream_api_key()

class SpeechServiceError(Exception):
    """Base exception for all speech service errors."""
    pass


class UpstreamRequestRejected(SpeechServiceError):
    """The upstream service refused the request as invalid — the caller's fault."""
    pass


class UpstreamCredentialRejected(SpeechServiceError):
    """The upstream service refused our credential — a deployment fault."""
    pass


class UpstreamUnavailable(SpeechServiceError):
    """The upstream service could not be reached."""
    pass

# ------------------------------------------------------------
#  ASR Client (Whisper)
# ------------------------------------------------------------

class ASRClient:
    """
    Client for Whisper ASR service (OpenAI‑compatible API).
    Presents the upstream credential, resolved separately from the key that
    callers of this service present.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.asr_url = self._get_config(UPSTREAM_URL_NAMES, required=True)
        self.auth_key = self._get_config(UPSTREAM_API_KEY_NAMES, required=True)

        # How many times a single request may reach the upstream service.
        self.max_attempts = 3

        # Logging
        log_level = self._get_config(LOG_LEVEL_NAMES, default="INFO").upper()
        self.logger = self._setup_logging(log_level)

        # Reusable session
        self.session = requests.Session()
        self.session.verify = True  # Use system CA bundle
        self.logger.info("ASRClient initialized (endpoint: %s)", self.asr_url)

    def _get_config(self, names, required: bool = False, default: str = None) -> str:
        """Resolve a configuration value from an ordered list of names."""
        if isinstance(names, str):
            names = (names,)
        for name in names:
            value = self.config.get(name) or get_secret(name) or os.getenv(name)
            if value:
                return value
        if required:
            raise SpeechServiceError(
                "Missing required configuration: " + ", ".join(names)
            )
        return default

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
        # This logger has a handler of its own; without this the record
        # would also reach the root logger's handler and be printed twice.
        logger.propagate = False
        return logger

    def _request_with_retries(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        Make an HTTP request, retrying only what is worth retrying.

        A single retry layer, so the number of attempts stays bounded: the
        previous code had two nested decorators, which meant up to nine
        attempts — each carrying a full upload — before the caller was
        answered. Transport failures and upstream 5xx are retried; a 4xx is
        not, because a rejected payload or a rejected credential will not
        become acceptable on a second attempt.
        """
        self.logger.debug("Request: %s %s", method, url)

        @retry(
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type((
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
            )),
            reraise=True,
        )
        def _attempt():
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code >= 500:
                raise requests.exceptions.HTTPError(
                    f"upstream returned {resp.status_code}", response=resp
                )
            return resp

        return _attempt()

    def _media_type(self, filename: str) -> str:
        """
        The media type of the uploaded file, derived from its own name.

        The upstream service dispatches on the file extension, but declaring
        the real type keeps the request honest — a .mp3 must not be announced
        as a .wav.
        """
        guessed, _ = mimetypes.guess_type(filename)
        if not guessed:
            return "application/octet-stream"
        # Python's mime table reports .wav as the x- variant; the IANA
        # registered name for it is audio/wav.
        if guessed == "audio/x-wav":
            return "audio/wav"
        return guessed

    def recognize_audio(
        self,
        file: BinaryIO,
        filename: str,
        language: str = "ru-RU",
        model: str = "large-v3-turbo",
    ) -> str:
        """
        Send audio file to Whisper and return transcribed text.

        :param file: Open file-like object (e.g., from UploadFile.file)
        :param filename: Original filename (used for MIME type detection)
        :param language: Language code (e.g., 'ru-RU' → 'ru')
        :param model: Whisper model name (must match the server's available models)
        :return: Transcribed text
        """
        headers = {"Authorization": f"Basic {self.auth_key}"}
        file_content = file.read()

        files = {
            "file": (filename, file_content, self._media_type(filename))
        }
        data = {
            "model": model,
            "language": language.split("-")[0],  # 'ru-RU' → 'ru'
            "response_format": "json",
        }

        try:
            self.logger.info("Transcribing: %s (language: %s)", filename, language)
            resp = self._request_with_retries(
                "POST",
                self.asr_url,
                headers=headers,
                files=files,
                data=data,
                timeout=(10, 120),  # connect, read
            )

            if resp.status_code in (401, 403):
                self.logger.error(
                    "The upstream service rejected the credential (HTTP %d)", resp.status_code
                )
                raise UpstreamCredentialRejected(
                    "The upstream speech service rejected the configured credential"
                )
            if resp.status_code >= 400:
                self.logger.error(
                    "The upstream service rejected the request: HTTP %d - %s",
                    resp.status_code, resp.text[:500],
                )
                raise UpstreamRequestRejected(
                    f"The upstream speech service rejected the request: {resp.text[:500]}"
                )

            result = resp.json()
            text = (result.get("text") or "").strip()
            self.logger.info("Transcription successful: %d chars", len(text))
            return text
        except SpeechServiceError:
            raise
        except (requests.ConnectionError, requests.Timeout) as e:
            self.logger.error("The upstream speech service is unreachable: %s", e)
            raise UpstreamUnavailable(
                f"The upstream speech service is unavailable: {e}"
            ) from e
        except requests.exceptions.HTTPError as e:
            self.logger.error("HTTP error: %s", e)
            raise UpstreamUnavailable(f"Whisper ASR error: {e}") from e
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
        self.tts_url = self._get_config(TTS_URL_NAMES, required=True)
        self.auth_key = self._get_config(UPSTREAM_API_KEY_NAMES, required=True)

        log_level = self._get_config(LOG_LEVEL_NAMES, default="INFO").upper()
        self.logger = self._setup_logging(log_level)

        self.session = requests.Session()
        self.session.verify = True
        self.logger.info("TTSClient initialized (stub) – endpoint: %s", self.tts_url)

    def _get_config(self, names, required: bool = False, default: str = None) -> str:
        """Resolve a configuration value from an ordered list of names."""
        if isinstance(names, str):
            names = (names,)
        for name in names:
            value = self.config.get(name) or get_secret(name) or os.getenv(name)
            if value:
                return value
        if required:
            raise SpeechServiceError(
                "Missing required configuration: " + ", ".join(names)
            )
        return default

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
        logger.propagate = False
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
    """
    Client for the GigaChat API.

    Retries with exponential backoff, and manages the access token. Images
    can be attached to a request; the type travels with the file, because
    the upload API refuses a file it cannot identify.
    """

    VALID_SCOPES = {"GIGACHAT_API_PERS", "GIGACHAT_API_B2B", "GIGACHAT_API_CORP"}

    def __init__(
        self,
        credentials: str,
        ca_bundle_file: Optional[str] = None,
        scope: Optional[str] = "GIGACHAT_API_PERS",
        model: Optional[str] = None,
    ):
        self.credentials = credentials
        self.ca_bundle_file = ca_bundle_file
        self.scope = scope
        self.model = model or GIGACHAT_MODEL
        self.logger = logging.getLogger("GigaChatClient")
        self._client = None
        if scope and scope not in self.VALID_SCOPES:
            self.logger.warning("Unknown GigaChat scope: %r. Valid: %s. Proceeding anyway.", scope, self.VALID_SCOPES)

    def _get_client(self) -> GigaChat:
        """Get or create the GigaChat client instance."""
        if self._client is None:
            client_kwargs = {
                "credentials": self.credentials,
                "timeout": 30.0,
                "model": self.model,
            }
            if self.scope is not None:
                client_kwargs["scope"] = self.scope
            if self.ca_bundle_file and os.path.exists(self.ca_bundle_file):
                client_kwargs["ca_bundle_file"] = self.ca_bundle_file
                client_kwargs["verify_ssl_certs"] = True
            else:
                self.logger.warning("Using GigaChat without SSL certificate verification. This is not recommended for production.")
                client_kwargs["verify_ssl_certs"] = False
            self._client = GigaChat(**client_kwargs)
        return self._client
    
    def check_auth(self) -> float:
        """
        Request a real access token from GigaChat to verify credentials,
        CA bundle and network connectivity.

        :return: elapsed seconds
        :raises Exception: if authentication fails
        """
        start = time.perf_counter()
        self._get_client().get_token()
        return time.perf_counter() - start

    def generate_response(
        self,
        text: str,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        attachments: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
    ) -> str:
        """
        Generate a response from GigaChat based on the input text.
        Implements retry logic with exponential backoff.

        :param text: Input text to send to GigaChat
        :param system_prompt: The system message
        :param attachments: Files to attach to the user message. Each is a
            dict with a `filename`, a `content` and a `media_type`: the type
            has to travel with the content, because the upload API refuses
            a file it cannot identify.
        :param model: Model to use for this request. The image-capable models
            reject image input, so a request that carries an image names a
            model that can read it.
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
            attachment_ids = []
            for attachment in attachments or []:
                uploaded = client.upload_file(
                    (
                        attachment["filename"],
                        attachment["content"],
                        attachment["media_type"],
                    ),
                    purpose="general",
                )
                attachment_ids.append(uploaded.id_)
            messages = [
                Messages(
                    role=MessagesRole.SYSTEM,
                    content=system_prompt
                ),
                Messages(
                    role=MessagesRole.USER,
                    content=text,
                    attachments=attachment_ids or None,
                    )
            ]
            chat_kwargs = {"messages": messages}
            if model:
                chat_kwargs["model"] = model
            chat = Chat(**chat_kwargs)
            response = client.chat(chat)
            return response.choices[0].message.content

        try:
            # The prompt and the reply are the visitor's own words, so they
            # are only written to the log when verbose logging is enabled.
            if VERBOSE_LOGGING:
                self.logger.info(f"Generating GigaChat response for: {text[:50]}...")
            result = _call()
            if VERBOSE_LOGGING:
                self.logger.info(f"GigaChat generated: {result[:50]}...")
            return result
        except Exception as e:
            self.logger.error(f"Error generating GigaChat response: {e}")
            raise SpeechServiceError(f"GigaChat generation failed: {e}") from e

# Load environment (if .env exists) — the import is at the top of the
# module; this second call picks up a file that appeared since.
load_dotenv()

# Setup logging for FastAPI (optional, but good for production).
# The root logger is given a handler so that loggers without one of their
# own still produce output. Loggers that configure their own handlers
# disable propagation, so a record is not emitted twice.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# -----------------------------FAST API starts here--------------------------------------------------

# ------------------------------------------------------------
#  Startup self-tests
#  Run once on application startup and log the results, so that a
#  broken deployment is visible in the logs immediately instead of
#  surfacing as 503s on the first user request.
# ------------------------------------------------------------

def _selftest_config() -> bool:
    """Report every configuration value by name. A failure here is fatal."""
    # The values the service cannot work without. The TTS URL is not among
    # them: TTS is a stub and answers 501 whatever the URL is.
    required = {
        "inbound API key",
        "upstream API key",
        "upstream URL",
        "GigaChat credentials",
    }
    resolved = [
        ("inbound API key", INBOUND_API_KEY, INBOUND_API_KEY_NAME),
        ("upstream API key", UPSTREAM_API_KEY, UPSTREAM_API_KEY_NAME),
        ("upstream URL", ASR_URL, ASR_URL_NAME),
        ("TTS URL", TTS_URL, TTS_URL_NAME),
        ("GigaChat credentials", GIGACHAT_CREDENTIALS, GIGACHAT_CREDENTIALS_NAME),
        ("GigaChat scope", SCOPE, SCOPE_NAME),
        ("CA bundle", CA_BUNDLE_PATH, CA_BUNDLE_PATH_NAME),
    ]
    missing = []
    for label, value, name in resolved:
        if value:
            # Say where the value came from: a variable, or the built-in
            # default. Printing "None" would suggest something is missing.
            source = name if name else "the built-in default"
            logger.info("SELF-TEST [config]: %-22s resolved from %s", label, source)
        else:
            logger.warning("SELF-TEST [config]: %-22s not set", label)
            if label in required:
                missing.append(label)

    if missing:
        logger.error(
            "SELF-TEST [config]: FAIL – missing required configuration: %s", ", ".join(missing)
        )
        return False
    return True


def _selftest_gigachat_config() -> bool:
    """Check GigaChat configuration presence. A failure here is fatal."""
    if not GIGACHAT_CREDENTIALS:
        logger.error("SELF-TEST [gigachat/config]: FAIL – GIGACHAT_CREDENTIALS is not set")
        return False
    ca_path = CA_BUNDLE_PATH
    if ca_path and not os.path.exists(ca_path):
        logger.error("SELF-TEST [gigachat/config]: FAIL – CA bundle not found at %s", ca_path)
        return False
    logger.info(
        "SELF-TEST [gigachat/config]: PASS – credentials present, CA bundle: %s",
        ca_path or "<not set – SSL verification will be disabled>",
    )
    return True


def _selftest_gigachat_auth() -> bool:
    """Initialize GigaChatClient and obtain a real access token."""
    global giga_client
    used_scope = SCOPE or "GIGACHAT_API_PERS"
    used_model = GIGACHAT_MODEL
    try:
        giga_client = GigaChatClient(
            credentials=GIGACHAT_CREDENTIALS,
            ca_bundle_file=CA_BUNDLE_PATH,
            scope=used_scope,
            model=used_model,
        )
        duration = giga_client.check_auth()
        logger.info("SELF-TEST [gigachat/auth]: PASS – access token received in %.2fs (scope=%s, model=%s)", duration, used_scope, used_model)
        return True
    except Exception as e:
        logger.error("SELF-TEST [gigachat/auth]: FAIL – %s (scope=%s, model=%s)", e, used_scope, used_model)
        return False


def _selftest_gigachat_smoke() -> None:
    """Optional end-to-end chat ping. Enable with GIGACHAT_SMOKE_TEST=true."""
    if os.getenv("GIGACHAT_SMOKE_TEST", "").lower() not in ("1", "true", "yes"):
        logger.info("SELF-TEST [gigachat/smoke]: SKIP – set GIGACHAT_SMOKE_TEST=true to enable")
        return
    try:
        start = time.perf_counter()
        reply = giga_client.generate_response("Ответь одним словом: работает?")
        logger.info(
            "SELF-TEST [gigachat/smoke]: PASS – reply=%r (%.2fs)",
            str(reply)[:50], time.perf_counter() - start,
        )
    except Exception as e:
        logger.error("SELF-TEST [gigachat/smoke]: FAIL – %s", e)


def _selftest_asr_credential() -> bool:
    """
    Check that the upstream speech service accepts the configured credential.

    A GET on the transcriptions path has no side effect: an authenticated GET
    is answered with 405, an unauthenticated one with 401. That makes it
    possible to tell a *rejected credential* apart from a *reachable service*,
    which a bare reachability check cannot do — it used to score the 401 of a
    broken deployment as "reachable".
    """
    url = ASR_URL
    if not url:
        logger.warning("SELF-TEST [asr/credential]: SKIP – ASR_URL is not set")
        return False
    if not UPSTREAM_API_KEY:
        logger.error("SELF-TEST [asr/credential]: FAIL – no upstream credential is configured")
        return False
    try:
        start = time.perf_counter()
        resp = requests.get(
            url,
            headers={"Authorization": f"Basic {UPSTREAM_API_KEY}"},
            timeout=10,
        )
        elapsed = time.perf_counter() - start
        if resp.status_code in (401, 403):
            logger.error(
                "SELF-TEST [asr/credential]: FAIL – the upstream service rejected "
                "the credential from %s (HTTP %d)",
                UPSTREAM_API_KEY_NAME, resp.status_code,
            )
            return False
        logger.info(
            "SELF-TEST [asr/credential]: PASS – the upstream service accepted the "
            "credential from %s (HTTP %d in %.2fs)",
            UPSTREAM_API_KEY_NAME, resp.status_code, elapsed,
        )
        return True
    except requests.RequestException as e:
        logger.warning(
            "SELF-TEST [asr/credential]: WARN – the upstream service is unreachable (%s)", e
        )
        return False


def run_startup_selftests() -> None:
    """
    Run all self-tests. Missing/invalid configuration is fatal (the container
    will crash-loop with a clear reason in the logs); network failures only
    degrade the service (503 on the affected endpoint).
    """
    logger.info("=" * 60)
    logger.info("STARTUP SELF-TESTS")
    logger.info("=" * 60)

    config_ok = _selftest_config()
    if not config_ok:
        raise RuntimeError(
            "Service configuration is invalid – see SELF-TEST [config] above for "
            "the missing values"
        )

    gigachat_config_ok = _selftest_gigachat_config()
    if not gigachat_config_ok:
        raise RuntimeError(
            "GigaChat configuration is invalid – "
            "check GIGACHAT_CREDENTIALS and CA_BUNDLE_PATH"
        )

    auth_ok = _selftest_gigachat_auth()
    if auth_ok:
        _selftest_gigachat_smoke()

    asr_ok = _selftest_asr_credential()
    logger.warning("SELF-TEST [tts]: SKIP – TTS service is a stub (not implemented)")

    passed = sum([config_ok, gigachat_config_ok, auth_ok, asr_ok])
    if config_ok and gigachat_config_ok and auth_ok:
        logger.info("STARTUP SELF-TESTS FINISHED: %d/4 checks passed", passed)
    else:
        logger.warning(
            "STARTUP SELF-TESTS FINISHED: %d/4 checks passed – "
            "the affected endpoints may return 503 until the dependency recovers",
            passed,
        )
    logger.info("=" * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    The application's startup and shutdown.

    On startup the configuration and the dependencies are checked, so a
    misconfigured deployment says so in its log before it serves a single
    request. On shutdown the shared clients are closed.
    """
    # ----- startup -----
    run_startup_selftests()
    yield
    # ----- shutdown -----
    for client in (_asr_client, _tts_client):
        if client:
            client.close()


app = FastAPI(
    title="ASR TTS API",
    description="ASR and Text-to-Speech with self-hosted Whisper and Kokoro",
    version="1.1",
    lifespan=lifespan,
)

#-------------------------Logging------------------------------------
# ---- 1. Configure rotating file logger ----
# The log directory is configurable so that it can be placed outside the
# source tree. A file logger inside the tree is watched by the development
# reloader, which then reports every request as a change.
LOG_DIR = os.environ.get("LOG_DIR", "logs")
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR, exist_ok=True)  # create directory if missing
LOG_FILE = os.path.join(LOG_DIR, "app.log")

# Create a logger for our application
logger = logging.getLogger("app")
logger.setLevel(logging.INFO)

# This logger has handlers of its own, so it must not also propagate to the
# root logger: basicConfig() below gives the root a handler, and a
# propagated record would be emitted by that handler as well — every line
# would appear twice in the console.
logger.propagate = False

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
    """
    Records how long every request took.

    The method, path, status and duration are logged for every request;
    that record is operational, so it is written whatever the verbosity
    setting says. The duration is also returned in the X-Process-Time
    header, which makes a slow request easy to see from the outside.
    """

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
    """
    The shared client for the upstream speech service.

    Created on first use and kept afterwards, so the HTTP session and its
    connection pool are reused across requests.
    """
    global _asr_client
    if _asr_client is None:
        _asr_client = ASRClient()
    return _asr_client


def get_tts_client() -> TTSClient:
    """
    The shared client for the speech synthesis service.

    Created on first use and kept afterwards, like the ASR client. The
    service it talks to is not implemented yet, so calls raise.
    """
    global _tts_client
    if _tts_client is None:
        _tts_client = TTSClient()
    return _tts_client


# ------------------------------------------------------------
#  Authentication
# ------------------------------------------------------------
# The API key authenticates callers on the endpoints that forward a request
# onward to another service. Endpoints that answer locally — health,
# readiness, the questionnaire pages and the static assets — need no key.

def verify_api_key(request: Request):
    """
    Reject a request that does not carry the configured API key.

    Three cases are kept apart, because they need different answers:
    - no key configured at all -> the deployment is broken (503)
    - a key configured, but the caller's does not match -> 401
    - the key matches -> the request proceeds
    """
    configured, configured_name = resolve_inbound_api_key()
    if not configured:
        logger.error(
            "Authentication is unavailable: no inbound API key is configured "
            "(looked for %s)", ", ".join(INBOUND_API_KEY_NAMES),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service is not configured: no API key is available for authentication",
        )

    provided = request.headers.get("X-API-Key")
    if not provided or not hmac.compare_digest(provided, configured):
        logger.warning(
            "Rejected a request to %s: missing or invalid API key", request.url.path
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key",
        )
    return True


# ------------------------------------------------------------
#  Health Check
# ------------------------------------------------------------

@app.get("/health")
async def health():
    """Liveness: reports only that the process is running."""
    return {"status": "ok", "service": "speech"}


@app.get("/ready")
def readiness():
    """
    Readiness: reports whether the dependencies needed to serve requests are
    usable. Unlike /health, this reaches out to the upstream services, so it
    answers with 503 while a dependency is unavailable.
    """
    checks = {}

    # Configuration: every value the service needs must be present.
    missing = [
        label
        for label, value in (
            ("inbound API key", INBOUND_API_KEY),
            ("upstream API key", UPSTREAM_API_KEY),
            ("upstream URL", ASR_URL),
            ("GigaChat credentials", GIGACHAT_CREDENTIALS),
        )
        if not value
    ]
    checks["config"] = {
        "ok": not missing,
        "missing": missing,
    }

    # Upstream speech service: reachable, and the credential accepted.
    upstream_ok = False
    upstream_detail = None
    if ASR_URL and UPSTREAM_API_KEY:
        try:
            resp = requests.get(
                ASR_URL,
                headers={"Authorization": f"Basic {UPSTREAM_API_KEY}"},
                timeout=10,
            )
            if resp.status_code in (401, 403):
                upstream_detail = "the upstream service rejected the credential"
            else:
                upstream_ok = True
        except requests.RequestException as e:
            upstream_detail = f"the upstream service is unreachable: {e}"
    else:
        upstream_detail = "the upstream service is not configured"
    checks["upstream_asr"] = {"ok": upstream_ok, "detail": upstream_detail}

    # GigaChat: reachable and the credentials accepted.
    gigachat_ok = False
    gigachat_detail = None
    if giga_client is not None:
        try:
            giga_client.check_auth()
            gigachat_ok = True
        except Exception as e:
            gigachat_detail = f"GigaChat is not usable: {e}"
    else:
        gigachat_detail = "GigaChat is not initialized"
    checks["gigachat"] = {"ok": gigachat_ok, "detail": gigachat_detail}

    ready = all(check["ok"] for check in checks.values())
    body = {
        "status": "ready" if ready else "not_ready",
        "service": "speech",
        "checks": checks,
    }
    if not ready:
        unavailable = [name for name, check in checks.items() if not check["ok"]]
        body["unavailable"] = unavailable
    return JSONResponse(status_code=200 if ready else 503, content=body)


# ------------------------------------------------------------
#  ASR Endpoint
# ------------------------------------------------------------

@app.post("/asr", dependencies=[Depends(verify_api_key)])
def speech_to_text(
    file: UploadFile = File(...),
    language: str = Form("ru-RU"),
    model: str = Form("large-v3-turbo"),
):
    """
    Accept an audio file and return the transcribed text.
    Uses the local Whisper service via ASRClient.

    Declared as a plain `def` on purpose: the upstream call blocks, and a
    blocking call in an `async def` would stall the event loop for every
    other request. A sync handler runs in the thread pool instead.
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
    except UpstreamRequestRejected as e:
        # The upstream refused the request itself: the caller's upload or
        # parameters are the problem.
        raise HTTPException(status_code=400, detail=str(e))
    except UpstreamCredentialRejected as e:
        # Our credential was refused: a deployment fault, not the caller's.
        raise HTTPException(status_code=502, detail=str(e))
    except UpstreamUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except SpeechServiceError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


# ------------------------------------------------------------
#  TTS Endpoint (stub)
# ------------------------------------------------------------

@app.post("/tts", dependencies=[Depends(verify_api_key)])
def text_to_speech(
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
        # Raises until the TTS service is implemented.
        client.synthesize_text(text, voice=voice, audio_format=audio_format)
        # Once implemented, return the audio with the proper content type
        # (Response would have to be imported from fastapi.responses):
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
#  Optional: documentation
#  NOTE: no JSON handler for "/" here – the HTML questionnaire below
#  must be the first (and only) GET "/" route, otherwise FastAPI's
#  registration-order matching shadows it.
# ------------------------------------------------------------

#--------------------------Front-end----------------------------------------------------
# Mount static files (CSS, JS)
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

# Set up Jinja2 templates
templates = Jinja2Templates(directory="frontend/templates")

# Multi-step intro
@app.get("/", response_class=HTMLResponse)
async def get_index(request: Request):
    """Serve the new multi-step questionnaire."""
    return templates.TemplateResponse(request, "index.html", {"request": request})

#--------------------------Back-end------------------------------------------------------

# --- Questionnaire prompt assembly ---
#
# The prompt is built from the answers the visitor actually gave. A value
# that was selected is used verbatim; a question that was not answered is
# left out of the prompt entirely, rather than filled in with a default
# that reads as if the visitor had chosen it.

OTHER_CHOICE = "другое"


def _selected(value: Optional[str], other: Optional[str]) -> Optional[str]:
    """
    The value the visitor chose for a question that offers an "other" option.

    Returns None when the question was not answered, so the caller can omit
    the clause. When "другое" was chosen, the visitor's own text is used.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value == OTHER_CHOICE:
        other = (other or "").strip()
        return other or None
    return value


def _clause(text: str) -> str:
    """One sentence of the prompt, or nothing when there is nothing to say."""
    text = (text or "").strip()
    return f" {text}" if text else ""


def _goal_clause(goal: Optional[str], goal_other: Optional[str]) -> str:
    """
    The sentence fragment naming the goal the visitor chose.

    Empty when no goal was given, so the prompt does not assert a goal the
    visitor never picked.
    """
    chosen = _selected(goal, goal_other)
    return _clause(f"c целью {chosen}.") if chosen else ""


def _build_past_prompt(data: Dict[str, Any], gender, age) -> str:
    """
    The prompt for the "past" track — a memory the visitor wants to relive.

    Only the answers that were given become sentences; a question the
    visitor skipped leaves no trace in the prompt, so the model is never
    told about a memory that was not reported.
    """
    event_name = (data.get("event_name") or "").strip()
    age_at_moment = (data.get("age_at_moment") or "").strip()
    place_action = (data.get("place_action") or "").strip()
    light_weather = _selected(
        data.get("light_weather"), data.get("light_weather_other")
    )

    opening = "Я хочу заново пережить опыт из прошлого"
    if event_name:
        opening = f"Я хочу заново пережить опыт {event_name} из прошлого"

    parts = [opening + _goal_clause(data.get("goal"), data.get("goal_other"))]
    if place_action:
        parts.append(f"Это было, когда я {place_action}.")
    if age_at_moment:
        parts.append(f"В тот момент мне было {age_at_moment}.")
    if light_weather:
        parts.append(f"Помню, что тогда было {light_weather}.")
    parts.append(f"Сейчас мне {age} лет, я {gender}.")
    return " ".join(parts)


def _build_present_prompt(data: Dict[str, Any], gender, age) -> str:
    """
    The prompt for the "present" track — looking at oneself from outside.

    As in the other tracks, only the answers that were given become
    sentences.
    """
    present_role = _selected(data.get("present_role"), data.get("present_role_other"))
    present_place = (data.get("present_place") or "").strip()
    time_day = _selected(data.get("time_day"), data.get("time_day_other"))
    one_thing = (data.get("one_thing") or "").strip()
    light_weather = _selected(
        data.get("light_weather"), data.get("light_weather_other")
    )

    parts = ["Я хочу посмотреть на себя со стороны" + _goal_clause(
        data.get("goal"), data.get("goal_other")
    )]
    parts.append(f"Мне {age} лет, я {gender}.")
    if present_place:
        parts.append(f"Я сейчас нахожусь в {present_place}.")
    if present_role:
        parts.append(f"Там я играю роль {present_role}.")
    if one_thing:
        parts.append(f"Всегда при мне {one_thing}.")
    if time_day:
        parts.append(f"Это время суток, которое отражает мой ритм: {time_day}.")
    if light_weather:
        parts.append(f"Свет и погода в этой сцене: {light_weather}.")
    parts.append("Смоделируй ситуацию, в которой я смотрю на себя со стороны.")
    return " ".join(parts)


def _build_future_prompt(data: Dict[str, Any], gender, age) -> str:
    """
    The prompt for the "future" track — a probable life to picture.

    As in the other tracks, only the answers that were given become
    sentences.
    """
    future_date = _selected(data.get("future_date"), data.get("future_date_other"))
    future_role = (data.get("future_role") or "").strip()
    future_place = (data.get("future_place") or "").strip()
    future_action = (data.get("future_action") or "").strip()
    light_weather = _selected(
        data.get("light_weather"), data.get("light_weather_other")
    )

    parts = ["Я хочу визуализировать свое вероятное будущее" + _goal_clause(
        data.get("goal"), data.get("goal_other")
    )]
    parts.append(f"Сейчас мне {age} лет, я {gender}.")
    if future_date:
        parts.append(f"Это происходит {future_date}.")
    if future_place:
        parts.append(f"Я нахожусь в {future_place}.")
    if future_role:
        parts.append(f"Там я играю роль {future_role}.")
    if future_action:
        parts.append(f"Я точно делаю {future_action}.")
    if light_weather:
        parts.append(f"Свет и погода в этой сцене: {light_weather}.")
    parts.append("Смоделируй ситуацию, которая происходит в этом будущем.")
    return " ".join(parts)


_PROMPT_BUILDERS = {
    "past": _build_past_prompt,
    "present": _build_present_prompt,
    "future": _build_future_prompt,
}


# --- Uploaded file handling ---
#
# The questionnaire offers an upload: "you can upload a file with your
# memory, it will help me a lot". A file that is accepted and then ignored
# is worse than no upload at all, because the visitor believes it was used.
# So an upload is either carried into the generation, or refused by name.

IMAGE_MEDIA_TYPES = ("image/",)
TEXT_MEDIA_TYPES = ("text/plain", "text/markdown")
AUDIO_MEDIA_TYPES = ("audio/",)

# Text documents are read locally; a bound keeps a stray file from being
# pulled into the prompt.
MAX_TEXT_BYTES = 100_000


class UnsupportedUpload(Exception):
    """The uploaded file type cannot be used."""


def _read_upload(filename: str, content_type: str, content: bytes) -> Dict[str, Any]:
    """
    Work out what to do with an uploaded file.

    Returns a dict with the kind of handling and, where applicable, the
    content to use. Raises UnsupportedUpload for a type the service cannot
    use, so the visitor is told rather than left guessing.
    """
    media_type = (content_type or "").split(";")[0].strip().lower()
    if not media_type:
        guessed, _ = mimetypes.guess_type(filename or "")
        media_type = (guessed or "").lower()

    if media_type.startswith(IMAGE_MEDIA_TYPES):
        return {"kind": "image", "content": content, "media_type": media_type}

    if media_type.startswith(TEXT_MEDIA_TYPES) or filename.lower().endswith(
        (".txt", ".md")
    ):
        if len(content) > MAX_TEXT_BYTES:
            raise UnsupportedUpload(
                f"The text file is too large to read (limit {MAX_TEXT_BYTES} bytes)"
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as e:
            raise UnsupportedUpload("The text file is not valid UTF-8 text") from e
        return {"kind": "text", "content": text, "media_type": media_type}

    if media_type.startswith(AUDIO_MEDIA_TYPES):
        return {"kind": "audio", "content": content, "media_type": media_type}

    raise UnsupportedUpload(
        f"Files of type '{media_type or filename or 'unknown'}' cannot be used. "
        "Please upload an image, an audio file, or a text document."
    )


def _transcribe_upload(filename: str, content: bytes) -> str:
    """
    Transcribe an uploaded audio file with the service's own speech service.
    """
    client = get_asr_client()
    return client.recognize_audio(
        io.BytesIO(content),
        filename=filename,
        language=UPLOAD_ASR_LANGUAGE,
        model=UPLOAD_ASR_MODEL,
    )


# --- Questionnaire submission endpoint ---

@app.post("/submit_questionnaire")
async def submit_questionnaire(
    request: Request,
    gender: str = Form(...),
    age: str = Form(...),
    track: str = Form(...),
    goal: str = Form(...),
    goal_other: Optional[str] = Form(None),
    # Track-specific fields are read from the parsed form, so the endpoint
    # stays flexible as the questionnaire grows.
):
    """
    Receive all questionnaire data, enrich context, and call GigaChat.
    """
    if giga_client is None:
        raise HTTPException(status_code=503, detail="GigaChat service not available")

    form_data = await request.form()
    data = {k: v for k, v in form_data.items()}

    gender = data.get("gender")
    age = data.get("age")
    track = data.get("track")

    builder = _PROMPT_BUILDERS.get(track)
    if builder is None:
        raise HTTPException(status_code=400, detail="Invalid track")

    user_message = builder(data, gender, age)

    # --- The uploaded file, if any ---
    attachments: List[Dict[str, Any]] = []
    upload_note = ""
    upload = form_data.get("memory_file")
    if upload is not None and getattr(upload, "filename", ""):
        content = await upload.read()
        try:
            handled = _read_upload(upload.filename, upload.content_type or "", content)
        except UnsupportedUpload as e:
            raise HTTPException(status_code=415, detail=str(e))

        if handled["kind"] == "image":
            attachments.append(
                {
                    "filename": upload.filename,
                    "content": handled["content"],
                    "media_type": handled["media_type"],
                }
            )
            upload_note = "К воспоминанию приложено изображение."
        elif handled["kind"] == "text":
            upload_note = f"Приложенный текст:\n{handled['content']}"
        elif handled["kind"] == "audio":
            try:
                transcript = _transcribe_upload(upload.filename, handled["content"])
            except SpeechServiceError as e:
                raise HTTPException(
                    status_code=503,
                    detail=f"The uploaded audio could not be transcribed: {e}",
                )
            upload_note = f"Приложенная запись расшифрована:\n{transcript}"

    if upload_note:
        user_message = f"{user_message} {upload_note}"

    # --- 2. TODO: Enrich context (your logic here) ---
    enriched_prompt = user_message  # Placeholder – replace with your enrichment

    # --- Log the enriched prompt (truncated) ---
    # The visitor's own words are personal data. They are logged only when
    # verbose logging has been explicitly enabled.
    if VERBOSE_LOGGING:
        logger.info(f"TRACK: {track}; ENRICHED_PROMPT: \n{enriched_prompt}")

    # --- 3. Call GigaChat ---
    director_prompt = """
    Ты - сценарист-психолог.
    Твоя задача - сгенерировать сценарий видеоролика, который впоследствии будет использован нейросетью для генерации видео.
    Четко опиши каждую сцену и дай указания нейросети, как сгенерировать видео.
    Не используй имен собственных, если пользователь их в явном виде не сообщил.
    Помни, что нейросетевые видео ограничены по времени.
    """
    start = time.perf_counter()

    # An image needs a model that can read it; the default text model
    # answers "Model does not support image".
    request_model = VISION_MODEL if attachments else None
    try:
        response_text = giga_client.generate_response(
            enriched_prompt,
            system_prompt=director_prompt,
            attachments=attachments or None,
            model=request_model,
        )
    except Exception as e:
        duration = time.perf_counter() - start
        logger.error(f"GigaChat error after {duration:.3f}s: {e}")
        raise HTTPException(status_code=503, detail=f"GigaChat error: {str(e)}")
    duration = time.perf_counter() - start
    logger.info(f"LLM call completed in {duration:.3f}s")
    if VERBOSE_LOGGING:
        logger.info(f"LLM response:{response_text}")

    # --- 4. Return the response ---
    return {"response": response_text}


# A2A Endpoint To be added later------------------------