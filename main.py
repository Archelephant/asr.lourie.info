import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Request, File, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
import uuid
import time
from datetime import datetime, timedelta
from typing import Optional, BinaryIO
import requests
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import mimetypes

#Helper function to determine the MIME media type
def get_audio_mime_type_old(filename: str) -> str:
    """Return the correct Content-Type header for SaluteSpeech API based on file extension."""
    ext = os.path.splitext(filename)[1].lower()
    # Explicit mapping for SaluteSpeech requirements
    mime_map = {
        '.wav': 'audio/x-wav',
        '.mp3': 'audio/mpeg',
        '.ogg': 'audio/ogg',
        '.flac': 'audio/flac',
        '.opus': 'audio/ogg;codecs=opus',
        '.pcm': 'audio/x-pcm;bit=16;rate=16000',
    }
    if ext in mime_map:
        return mime_map[ext]
    
    # Fallback for other types
    mime = mimetypes.guess_type(filename)[0]
    if mime and mime.startswith('audio/'):
        return mime
    return 'application/octet-stream'

def get_audio_mime_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    mime_map = {
        '.wav': 'audio/x-wav',
        '.mp3': 'audio/mpeg',
        '.ogg': 'audio/ogg',
        '.flac': 'audio/flac',
        '.opus': 'audio/ogg;codecs=opus',
        '.pcm': 'audio/x-pcm;bit=16;rate=16000',
    }
    return mime_map.get(ext, 'application/octet-stream')

def get_secret(env_var_name):
    """Retrieve a secret either from a standard env var or from a file."""
    file_path = os.environ.get(f"{env_var_name}_FILE")
    if file_path and os.path.exists(file_path):
        with open(file_path, 'r') as f:
            return f.read().strip()
    return os.environ.get(env_var_name)

# Load environment variables from .env (if present)
load_dotenv()
SALUTE_SPEECH_API_URL = get_secret("SALUTE_SPEECH_API_URL")
TTS_URL = get_secret("TTS_URL")
SCOPE = get_secret("SCOPE")
AUTH_KEY = get_secret("AUTH_KEY")

class SaluteSpeechError(Exception):
    """Base exception for SaluteSpeech API errors."""
    pass

class SaluteSpeechClient:
    """
    A robust client for SaluteSpeech TTS API with token management,
    automatic retries, and logging.
    """

    def __init__(self, config: Optional[dict] = None):
        """
        Initialize client from environment variables or passed config dict.
        
        :param config: Optional dictionary with keys:
            - api_url: OAuth token endpoint
            - tts_url: TTS synthesis endpoint
            - scope: OAuth scope
            - auth_key: Basic auth key
            - ca_bundle: Path to CA certificate file
            - log_level: Logging level (e.g., 'INFO')
        """
        self.config = config or {}
        self.api_url = self._get_config('SALUTE_SPEECH_API_URL', required=True)
        self.tts_url = self._get_config('TTS_URL', required=True)
        self.scope = self._get_config('SCOPE', required=True)
        self.auth_key = self._get_config('AUTH_KEY', required=True)
        self.ca_bundle = self._get_config('CA_BUNDLE_PATH', default='russiantrustedca.pem')
        
        # Token management
        self._token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        
        # Setup logging
        log_level = self._get_config('LOG_LEVEL', default='INFO').upper()
        self.logger = self._setup_logging(log_level)
        
        # Session for connection reuse
        self.session = requests.Session()
        self.session.verify = self.ca_bundle
        
        self.logger.info("SaluteSpeechClient initialized")

    def _get_config(self, key: str, required: bool = False, default: str = None) -> str:
        """Retrieve configuration from passed dict, then from secret (via get_secret), then default."""
        value = self.config.get(key) or get_secret(key) or os.getenv(key, default)
        if required and not value:
            raise SaluteSpeechError(f"Missing required configuration: {key}")
        return value

    def _setup_logging(self, level: str) -> logging.Logger:
        """Configure structured logging suitable for a web server."""
        logger = logging.getLogger('SaluteSpeechClient')
        logger.setLevel(getattr(logging, level))
        
        # Avoid adding handlers multiple times
        if not logger.handlers:
            # Console handler (for Docker / systemd)
            console = logging.StreamHandler()
            console.setLevel(logging.DEBUG)
            console.setFormatter(logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            ))
            logger.addHandler(console)
            
            # Optional: Rotating file handler – uncomment if needed
            # from logging.handlers import RotatingFileHandler
            # file_handler = RotatingFileHandler('/var/log/salutespeech.log', maxBytes=10*1024*1024, backupCount=5)
            # file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
            # logger.addHandler(file_handler)
        
        return logger

    def _is_token_valid(self) -> bool:
        """Check if current token exists and is not expired."""
        if not self._token or not self._token_expires_at:
            return False
        # Add a 30-second safety margin
        return datetime.utcnow() < (self._token_expires_at - timedelta(seconds=30))

    def _fetch_new_token(self) -> str:
        """Obtain a new OAuth token from SaluteSpeech."""
        self.logger.info("Fetching new OAuth token")
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
            'RqUID': str(uuid.uuid4()),
            'Authorization': f'Basic {self.auth_key}'
        }
        data = {'scope': self.scope}
        
        try:
            resp = requests.post(
                self.api_url,
                headers=headers,
                data=data,
                verify=self.ca_bundle,
                timeout=30
            )
            resp.raise_for_status()
            token_data = resp.json()
            token = token_data['access_token']
            # Assume token valid for 30 minutes (1800 seconds)
            expires_in = token_data.get('expires_in', 1800)
            self._token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
            self._token = token
            self.logger.info(f"Token obtained, expires at {self._token_expires_at}")
            return token
        except Exception as e:
            self.logger.error(f"Failed to obtain token: {e}")
            raise SaluteSpeechError(f"Token acquisition failed: {e}") from e

    def _get_valid_token(self) -> str:
        """Return a valid token, refreshing if necessary."""
        if not self._is_token_valid():
            self._fetch_new_token()
        return self._token

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True
    )
    def _request_with_retries(self, method: str, url: str, **kwargs) -> requests.Response:
        """Perform HTTP request with automatic retries on network errors."""
        self.logger.debug(f"Request: {method} {url}")
        resp = self.session.request(method, url, **kwargs)
        # If 401 Unauthorized, token may be expired – try one more time with new token
        if resp.status_code == 401:
            self.logger.warning("Received 401, token might be invalid. Fetching new token and retrying once.")
            self._fetch_new_token()
            # Update Authorization header
            kwargs['headers']['Authorization'] = f'Bearer {self._token}'
            resp = self.session.request(method, url, **kwargs)
        return resp

    def synthesize_text(
        self,
        text: str,
        voice: str = "Ost_24000",
        audio_format: str = "opus",
        max_retries: int = 2
    ) -> bytes:
        """
        Convert text to speech.

        :param text: Text to synthesize (max 4000 chars)
        :param voice: Voice model (e.g., Ost_24000, Bys_24000, Nec_24000)
        :param audio_format: One of 'opus', 'wav16', 'pcm16', 'alaw', 'g729'
        :param max_retries: How many times to retry on recoverable errors
        :return: Raw audio bytes (Opus in Ogg container for format='opus')
        :raises SaluteSpeechError: on unrecoverable errors
        """
        if len(text) > 4000:
            raise SaluteSpeechError("Text exceeds 4000 character limit")

        token = self._get_valid_token()
        params = {'format': audio_format, 'voice': voice}
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/text'
        }

        # Use tenacity for retries on transient errors (5xx, connection issues)
        @retry(
            stop=stop_after_attempt(max_retries + 1),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type((
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.RetryError
            )),
            reraise=True
        )
        def _call():
            resp = self._request_with_retries(
                'POST',
                self.tts_url,
                headers=headers,
                params=params,
                data=text.encode('utf-8'),
                stream=True,
                timeout=(10, 30)  # connect timeout, read timeout
            )
            # Raise for any 4xx/5xx (except 401 which is already handled)
            resp.raise_for_status()
            return resp.content

        try:
            self.logger.info(f"Synthesizing text of length {len(text)} with voice {voice}")
            audio_bytes = _call()
            self.logger.info(f"Synthesis successful, got {len(audio_bytes)} bytes")
            return audio_bytes
        except requests.exceptions.HTTPError as e:
            self.logger.error(f"HTTP error during synthesis: {e.response.status_code} - {e.response.text}")
            raise SaluteSpeechError(f"TTS API error: {e}") from e
        except Exception as e:
            self.logger.error(f"Unexpected error during synthesis: {e}")
            raise SaluteSpeechError(f"Synthesis failed: {e}") from e
        
    def recognize_audio(self, file: BinaryIO, filename: str, language: str = "ru-RU") -> str:
        """
        Recognize speech from an audio file using SaluteSpeech ASR API.

        :param file: An open binary file object containing the audio.
        :param language: Language code for recognition (ru-RU, en-US, kk-KZ), 
        :return: Transcribed text as a string.
        :raises SaluteSpeechError: on unrecoverable errors.
        """
        #NB! Please look up SaluteSpeech documentation for actual language support!!!
        #https://developers.sber.ru/docs/ru/salutespeech/rest/post-speech-recognition

        # Get a valid OAuth token
        token = self._get_valid_token()

        # Construct the ASR endpoint URL (adjust if your URL is different)
        # In production, this should be read from a secret, similar to TTS_URL.
        asr_url = get_secret("ASR_URL")
        if not asr_url:
            # Fallback for local development
            asr_url = "https://smartspeech.sber.ru/rest/v1/speech:recognize"
            
        # Get the correct Content-Type
        content_type = get_audio_mime_type(filename)
        
        #headers = {
        #    'Authorization': f'Bearer {token}',
        #}
        
        headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': content_type,  # Crucial addition
        }

        # Use tenacity for retries on network and server errors
        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_exception_type((
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.RetryError
            )),
            reraise=True
        )
        def _call():
            # The file is sent as the request body, with appropriate headers
            # 'Content-Type' is set to the actual MIME type of the file.
            # For example, 'audio/wav' for WAV files or 'audio/mpeg' for MP3.
            # If unknown, we can try 'application/octet-stream' as a fallback.
            # The exact file type can be derived from the 'file' object's name.
            # However, for simplicity, we'll rely on the 'file' being correctly opened.
            # Advanced: You can use python-magic to detect MIME type.
            # For now, we'll let requests set it to 'application/octet-stream'.
            # See README for more details on how to enhance this.
            resp = self._request_with_retries(
                'POST',
                asr_url,
                headers=headers,
                data=file.read(),  # Read the entire file into memory
                timeout=(10, 60)  # Longer read timeout for audio processing
            )
            resp.raise_for_status()
            return resp.json()

        try:
            self.logger.info(f"Recognizing audio file of size {self._get_file_size(file)} bytes")
            result = _call()
            text = result.get('result', '')
            # According to the library docs, the response may contain a 'text' field
            # or a nested structure. We'll handle both.
            if not text and 'text' in result:
                text = result['text']
            if not text and 'segments' in result:
                text = ' '.join(segment.get('text', '') for segment in result['segments'])
            self.logger.info(f"Recognition successful: {text[:50]}...")
            return text
        except requests.exceptions.HTTPError as e:
            self.logger.error(f"HTTP error during recognition: {e.response.status_code} - {e.response.text}")
            raise SaluteSpeechError(f"ASR API error: {e}") from e
        except Exception as e:
            self.logger.error(f"Unexpected error during recognition: {e}")
            raise SaluteSpeechError(f"Recognition failed: {e}") from e

    def _get_file_size(self, file: BinaryIO) -> int:
        """Get the size of the file object (moves file pointer)."""
        current_pos = file.tell()
        file.seek(0, os.SEEK_END)
        size = file.tell()
        file.seek(current_pos)
        return size

    def close(self):
        """Clean up session."""
        self.session.close()
        self.logger.info("Client closed")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

# Load environment (if .env exists)
from dotenv import load_dotenv
load_dotenv()

# Setup logging for FastAPI (optional, but good for production)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("asr-api")

# Global client instance (lazy initialization or on startup)
client: Optional[SaluteSpeechClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create client
    global client
    try:
        client = SaluteSpeechClient()
        logger.info("SaluteSpeechClient initialized")
    except Exception as e:
        logger.error(f"Failed to initialize SaluteSpeechClient: {e}")
        raise
    yield
    # Shutdown: clean up
    if client:
        client.close()
        logger.info("SaluteSpeechClient closed")

app = FastAPI(
    title="ASR TTS API",
    description="Text-to-Speech using SaluteSpeech API",
    version="1.0",
    lifespan=lifespan
)

# ---- Authentication Dependency ----
API_KEY = get_secret("ASR_API_KEY")
if not API_KEY:
    logger.warning("ASR_API_KEY not set in environment. API will be unprotected!")

def verify_api_key(request: Request):
    provided_key = request.headers.get("X-API-Key")
    if not API_KEY:
        # If no key configured, allow all (not recommended)
        return True
    if not provided_key or provided_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")
    return True

# ---- Request/Response Models ----
class TTSRequest(BaseModel):
    text: str = Field(..., max_length=4000, description="Text to synthesize")
    voice: str = Field("Ost_24000", description="Voice code (e.g., Ost_24000, Bys_24000)")
    format: str = Field("opus", description="Audio format: opus, wav16, pcm16, alaw, g729")

class TTSResponse(BaseModel):
    # Not used for audio response, but for error details
    detail: str

# ---- Synthesize Speech Endpoint ----
@app.post("/asr/synthesize", dependencies=[Depends(verify_api_key)])
async def synthesize(tts_req: TTSRequest):
    """
    Synthesize speech from text and return audio bytes.
    """
    global client
    if client is None:
        raise HTTPException(status_code=503, detail="TTS service not initialized")

    try:
        audio_bytes = client.synthesize_text(
            text=tts_req.text,
            voice=tts_req.voice,
            audio_format=tts_req.format
        )
        # Determine content type based on format
        content_type = "audio/ogg" if tts_req.format == "opus" else f"audio/{tts_req.format}"
        return Response(content=audio_bytes, media_type=content_type)
    except SaluteSpeechError as e:
        logger.error(f"TTS synthesis error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.exception("Unexpected error during synthesis")
        raise HTTPException(status_code=500, detail="Internal server error")
    
@app.post("/asr/asr", dependencies=[Depends(verify_api_key)])
async def asr(
    request: Request,
    file: UploadFile = File(...),
    language: str = "ru-RU"
):
    """
    Recognize speech from an audio file and return text.

    - **file**: Audio file (WAV, MP3, etc.). Max size determined by nginx (30 MB).
    - **language**: Language code (`ru-RU`, `en-US`, `kk-KZ`). Defaults to `ru-RU`.
    """
    global client
    if client is None:
        raise HTTPException(status_code=503, detail="ASR service not initialized")

    # Validate file content type (basic check)
    allowed_types = ['audio/wav', 'audio/x-wav', 'audio/mpeg', 'audio/mp3', 'audio/ogg', 'application/octet-stream']
    if file.content_type not in allowed_types:
        # Optionally, you can try to detect MIME type using python-magic
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {file.content_type}")

    # Validate language parameter
    if language not in ['ru-RU', 'en-US', 'kk-KZ']:
        raise HTTPException(status_code=400, detail=f"Unsupported language: {language}. Supported: ru-RU, en-US, kk-KZ")

    
    try:
        contents = await file.read()
        
        # Check file size (2 MB limit for SaluteSpeech)
        if len(contents) > 2 * 1024 * 1024:  # 2 MB
            raise HTTPException(
                status_code=400, 
                detail="File size exceeds 2 MB limit for synchronous recognition"
            )
        
        from io import BytesIO
        audio_io = BytesIO(contents)
        
        # Pass the original filename for MIME detection
        text = client.recognize_audio(audio_io, file.filename, language=language)
        return {"text": text}
    except Exception as e:
        logger.exception("Unexpected error during ASR processing")
        raise HTTPException(status_code=500, detail="Internal server error")