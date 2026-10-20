"""
Tests for the handling of an uploaded memory file.

An upload is either carried into the generation, or refused by name. It is
never accepted and then quietly ignored.
"""


import pytest
from fastapi.testclient import TestClient

# A 1x1 red PNG.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001od0a2db40000000049454e44ae"
    "426082".replace("od", "0d")
)


@pytest.fixture
def client(app_module):
    return TestClient(app_module.app, raise_server_exceptions=False)


@pytest.fixture
def captured(app_module, monkeypatch):
    """Capture what the language model was asked, and with what."""
    captured = {}

    class StubGiga:
        def generate_response(self, text, system_prompt=None, attachments=None, model=None):
            captured["prompt"] = text
            captured["attachments"] = attachments
            captured["model"] = model
            return "generated"

    monkeypatch.setattr(app_module, "giga_client", StubGiga())
    return captured


BASE = {
    "gender": "мужской",
    "age": "30",
    "track": "past",
    "goal": "отпустить",
    "event_name": "Выпускной",
}


class TestAnImageIsAttached:
    def test_the_image_reaches_the_model(self, client, captured):
        response = client.post(
            "/submit_questionnaire",
            data=BASE,
            files={"memory_file": ("photo.png", PNG_BYTES, "image/png")},
        )
        assert response.status_code == 200
        assert captured["attachments"], "the image was not attached"
        attachment = captured["attachments"][0]
        assert attachment["content"] == PNG_BYTES
        assert attachment["media_type"] == "image/png"
        assert attachment["filename"] == "photo.png"

    def test_an_image_request_names_an_image_capable_model(self, client, captured, app_module):
        """The default text model answers 'Model does not support image'."""
        client.post(
            "/submit_questionnaire",
            data=BASE,
            files={"memory_file": ("photo.png", PNG_BYTES, "image/png")},
        )
        assert captured["model"] in app_module.IMAGE_CAPABLE_MODELS

    def test_a_request_without_an_image_keeps_the_default_model(self, client, captured):
        client.post("/submit_questionnaire", data=BASE)
        assert captured["attachments"] is None
        assert captured["model"] is None

    def test_the_prompt_mentions_the_attachment(self, client, captured):
        client.post(
            "/submit_questionnaire",
            data=BASE,
            files={"memory_file": ("photo.png", PNG_BYTES, "image/png")},
        )
        assert "изображение" in captured["prompt"].lower()

    def test_the_media_type_travels_with_the_file(self, client, captured):
        """
        The upload API refuses a file it cannot identify: without the type
        it answers "File format application/octet-stream is not supported".
        """
        client.post(
            "/submit_questionnaire",
            data=BASE,
            files={"memory_file": ("photo.png", PNG_BYTES, "image/png")},
        )
        attachment = captured["attachments"][0]
        assert attachment["media_type"] == "image/png"
        assert attachment["filename"] == "photo.png"


class TestAnAudioFileIsTranscribed:
    def test_the_transcript_reaches_the_prompt(self, client, captured, app_module, monkeypatch):
        class StubASR:
            def recognize_audio(self, file, filename, language, model):
                return "Привет, это моя запись"

            def close(self):
                pass

        monkeypatch.setattr(app_module, "get_asr_client", lambda: StubASR())

        response = client.post(
            "/submit_questionnaire",
            data=BASE,
            files={"memory_file": ("voice.wav", b"RIFFfake", "audio/wav")},
        )
        assert response.status_code == 200
        assert "Привет, это моя запись" in captured["prompt"]

    def test_a_failed_transcription_is_reported(self, client, captured, app_module, monkeypatch):
        class FailingASR:
            def recognize_audio(self, file, filename, language, model):
                raise app_module.SpeechServiceError("the upstream service is down")

            def close(self):
                pass

        monkeypatch.setattr(app_module, "get_asr_client", lambda: FailingASR())

        response = client.post(
            "/submit_questionnaire",
            data=BASE,
            files={"memory_file": ("voice.wav", b"RIFFfake", "audio/wav")},
        )
        assert response.status_code == 503
        assert "transcribed" in response.json()["detail"].lower()


class TestATextDocumentIsRead:
    def test_the_content_reaches_the_prompt(self, client, captured):
        response = client.post(
            "/submit_questionnaire",
            data=BASE,
            files={"memory_file": ("note.txt", "Моё воспоминание о поездке".encode("utf-8"), "text/plain")},
        )
        assert response.status_code == 200
        assert "Моё воспоминание о поездке" in captured["prompt"]


class TestAnUnsupportedFileIsRefused:
    def test_the_type_is_named_in_the_error(self, client, captured):
        response = client.post(
            "/submit_questionnaire",
            data=BASE,
            files={"memory_file": ("doc.bin", b"\x00\x01", "application/octet-stream")},
        )
        assert response.status_code == 415
        assert "application/octet-stream" in response.json()["detail"]

    def test_nothing_was_sent_to_the_model(self, client, captured):
        client.post(
            "/submit_questionnaire",
            data=BASE,
            files={"memory_file": ("doc.bin", b"\x00\x01", "application/octet-stream")},
        )
        assert "prompt" not in captured

    def test_a_video_file_is_refused(self, client, captured):
        response = client.post(
            "/submit_questionnaire",
            data=BASE,
            files={"memory_file": ("clip.mp4", b"\x00\x00", "video/mp4")},
        )
        assert response.status_code == 415
        assert "video/mp4" in response.json()["detail"]


class TestAnUploadIsNeverSilentlyDropped:
    def test_a_file_is_either_used_or_refused(self, client, captured):
        """Every accepted upload has to show up in the generation."""
        for filename, content, content_type in (
            ("photo.png", PNG_BYTES, "image/png"),
            ("note.txt", b"some text", "text/plain"),
            ("clip.bin", b"\x00", "application/octet-stream"),
        ):
            captured.clear()
            response = client.post(
                "/submit_questionnaire",
                data=BASE,
                files={"memory_file": (filename, content, content_type)},
            )
            if response.status_code == 200:
                assert (
                    captured.get("attachments")
                    or "some text" in captured.get("prompt", "")
                ), f"{filename} was accepted and then ignored"
            else:
                assert response.status_code == 415
                assert "prompt" not in captured
