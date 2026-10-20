"""
Tests for the questionnaire prompt.

The prompt must reflect exactly what the visitor submitted: a selected
value verbatim, their own text when they choose "other", and nothing at all
for a question they did not answer.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(app_module):
    return TestClient(app_module.app, raise_server_exceptions=False)


@pytest.fixture
def captured_prompt(app_module, monkeypatch):
    """Capture the prompt that would be sent to the language model."""
    captured = {}

    class StubGiga:
        def generate_response(self, text, system_prompt=None, attachments=None, model=None):
            captured["prompt"] = text
            captured["attachments"] = attachments
            captured["model"] = model
            return "generated"

    monkeypatch.setattr(app_module, "giga_client", StubGiga())
    return captured


def submit(client, **fields):
    payload = {
        "gender": "мужской",
        "age": "31–45",
        "track": "past",
        "goal": "отпустить",
    }
    payload.update(fields)
    return client.post("/submit_questionnaire", data=payload)


class TestSelectedAnswersReachThePrompt:
    def test_a_listed_goal_is_used_verbatim(self, client, captured_prompt):
        submit(client, goal="отпустить", event_name="Выпускной")
        assert "отпустить" in captured_prompt["prompt"]

    def test_the_generic_goal_substitute_is_gone(self, client, captured_prompt):
        submit(client, goal="отпустить", event_name="Выпускной")
        assert "просто вспомнить" not in captured_prompt["prompt"]

    def test_a_listed_light_and_weather_is_used_verbatim(self, client, captured_prompt):
        submit(
            client,
            goal="отпустить",
            event_name="Выпускной",
            light_weather="солнце",
        )
        assert "солнце" in captured_prompt["prompt"]
        assert "не помню" not in captured_prompt["prompt"]

    def test_the_own_text_of_the_other_option_is_used(self, client, captured_prompt):
        submit(
            client,
            goal="другое",
            goal_other="отпустить по-настоящему",
            event_name="Выпускной",
        )
        assert "отпустить по-настоящему" in captured_prompt["prompt"]

    def test_a_choice_answer_reaches_the_prompt(self, client, captured_prompt):
        submit(
            client,
            track="present",
            goal="принять решение",
            present_role="Отец",
            present_place="кухня в 6 утра",
            time_day="утро",
            one_thing="наушники",
        )
        prompt = captured_prompt["prompt"]
        assert "Отец" in prompt
        assert "утро" in prompt
        assert "наушники" in prompt

    def test_the_own_text_of_a_choice_other_is_used(self, client, captured_prompt):
        submit(
            client,
            track="present",
            goal="принять решение",
            present_role="другое",
            present_role_other="капитан корабля",
            present_place="кухня",
        )
        assert "капитан корабля" in captured_prompt["prompt"]


class TestEveryAnswerIsCarried:
    def test_the_past_track_answers_are_present(self, client, captured_prompt):
        submit(
            client,
            track="past",
            goal="отпустить",
            event_name="Выпускной",
            age_at_moment="17",
            place_action="входил в пустой класс",
            light_weather="сумерки",
        )
        prompt = captured_prompt["prompt"]
        assert "Выпускной" in prompt
        assert "17" in prompt
        assert "входил в пустой класс" in prompt
        assert "сумерки" in prompt

    def test_the_object_always_carried_is_used(self, client, captured_prompt):
        """The old implementation read this answer and then dropped it."""
        submit(
            client,
            track="present",
            goal="принять решение",
            present_place="кухня",
            one_thing="кольцо с камнем",
        )
        assert "кольцо с камнем" in captured_prompt["prompt"]

    def test_the_future_track_answers_are_present(self, client, captured_prompt):
        submit(
            client,
            track="future",
            goal="мотивация",
            future_date="через три года",
            future_role="свободный художник",
            future_place="свой дом",
            future_action="открываю дверь",
        )
        prompt = captured_prompt["prompt"]
        assert "через три года" in prompt
        assert "свободный художник" in prompt
        assert "свой дом" in prompt
        assert "открываю дверь" in prompt


class TestUnansweredQuestionsAreNotAsserted:
    def test_an_unanswered_light_and_weather_is_absent(self, client, captured_prompt):
        submit(client, goal="отпустить", event_name="Выпускной")
        prompt = captured_prompt["prompt"]
        assert "не помню" not in prompt
        assert "Свет и погода" not in prompt

    def test_a_track_without_the_question_has_no_clause(self, client, captured_prompt):
        """The present track never asks about light and weather."""
        submit(
            client,
            track="present",
            goal="принять решение",
            present_place="кухня",
        )
        assert "Свет и погода" not in captured_prompt["prompt"]

    def test_an_unanswered_goal_is_absent(self, client, captured_prompt):
        """
        The goal is a required form field, so the browser always sends one.
        The "other" option without a text is blocked in the browser too —
        but the endpoint must not invent a goal if it ever receives one.
        """
        response = submit(client, goal="", event_name="Выпускной")
        # The form contract rejects it outright...
        assert response.status_code == 422
        # ...and nothing was sent to the model.
        assert "prompt" not in captured_prompt

    def test_no_default_is_invented_for_any_track(self, client, captured_prompt):
        """The old defaults must not reappear anywhere."""
        for track, payload in (
            ("past", {"event_name": "Выпускной"}),
            ("present", {"present_place": "кухня"}),
            ("future", {"future_place": "дом"}),
        ):
            captured_prompt.clear()
            submit(client, track=track, goal="отпустить", **payload)
            prompt = captured_prompt["prompt"]
            for default in ("просто вспомнить", "не помню", "не важно",
                            "самоосмысления", "проектирования пути развития"):
                assert default not in prompt, f"{default} was invented for track {track}"

    def test_a_missing_answer_never_reaches_the_prompt_as_none(self, client, captured_prompt):
        submit(
            client,
            track="present",
            goal="принять решение",
            present_place="кухня",
        )
        prompt = captured_prompt["prompt"]
        assert "None" not in prompt
