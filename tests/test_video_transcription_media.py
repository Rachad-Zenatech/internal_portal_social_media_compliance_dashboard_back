"""The media, not its filename or approved script, must produce the transcript."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import numpy as np
import pytest

from services.compliance.media import local_speech as speech, transcription_service as service
from services.compliance.media import video_transcription_provider as provider
from services.compliance.evaluation import script_match


def test_local_speech_uses_decoded_audio_and_timestamped_words():
    recognizer = MagicMock(return_value={"text": "We inspect the field", "chunks": [
        {"text": "We inspect the field", "timestamp": (0., 1.5)}]})
    samples = np.ones(32000, dtype=np.float32)
    with patch.object(speech, "audio_samples", return_value=samples), patch.object(speech, "_pipeline", recognizer):
        segments = speech.speech_segments("actual-video.mp4")
    assert segments == [dict(start_sec=0., end_sec=1.5, text="We inspect the field", source="speech")]
    assert recognizer.call_args.args[0]["raw"] is samples
    assert "script" not in str(recognizer.call_args.kwargs)


def test_silent_audio_does_not_hallucinate_speech_or_load_a_model():
    with patch.object(speech, "audio_samples", return_value=np.zeros(16000, dtype=np.float32)), \
         patch.object(speech, "_pipeline", None):
        assert speech.speech_segments("silent.mp4") == []
        assert speech._pipeline is None


def test_audio_decode_failure_is_not_a_successful_empty_transcript():
    with patch.object(speech, "_ffmpeg", return_value="ffmpeg"), \
         patch.object(speech.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr=b"corrupt audio")):
        with pytest.raises(RuntimeError, match="decode"):
            speech.audio_samples("corrupt.mp4")


def test_provider_combines_actual_local_speech_and_observed_overlay():
    spoken = dict(start_sec=0., end_sec=2., text="We inspect the field", source="speech")
    overlay = dict(start_sec=1., end_sec=1., text="Inspection", source="on_screen")
    response = SimpleNamespace(text=json.dumps([overlay]))
    with patch.object(provider.local_speech, "speech_segments", return_value=[spoken]), \
         patch.object(provider, "is_bedrock_enabled", return_value=True), \
         patch.object(provider, "sample_parts", return_value=["frame-image"]), \
         patch.object(provider.image_text_service, "_generate", AsyncMock(return_value=response)) as generate:
        segments = asyncio.run(provider.transcribe("actual.mp4", "video/mp4", service.PROMPT, service.RESPONSE_SCHEMA))
    assert segments == [spoken, overlay]
    assert generate.call_args.args[0][0] == ["frame-image"]
    assert generate.call_args.kwargs["feature"] == "TRANSCRIPTION"
    assert script_match.assess_video("We inspect the field", spoken["text"], {"transcript_status": "COMPLETED"})["status"] == "MATCH"


def test_overlay_provider_cannot_invent_speech():
    response = SimpleNamespace(text=json.dumps([dict(source="speech", text="invented")]))
    with patch.object(provider.local_speech, "speech_segments", return_value=[]), \
         patch.object(provider, "is_bedrock_enabled", return_value=True), \
         patch.object(provider, "sample_parts", return_value=["frame-image"]), \
         patch.object(provider.image_text_service, "_generate", AsyncMock(return_value=response)):
        with pytest.raises(provider.PartialTranscriptError, match="on-screen text read failed"):
            asyncio.run(provider.transcribe("actual.mp4", "video/mp4", "", service.RESPONSE_SCHEMA))


def test_worker_saves_real_segments_then_triggers_comparison(tmp_path):
    (tmp_path / "actual.mp4").write_bytes(b"test")
    segments = [dict(start_sec=0., end_sec=1., text="Actual spoken words", source="speech")]
    file_id = uuid4()
    with patch.object(service, "STORAGE_ROOT", tmp_path), \
         patch.object(service, "_claim", AsyncMock(return_value=dict(storage_path="actual.mp4", mime_type="video/mp4", original_name="actual.mp4"))), \
         patch.object(provider, "transcribe", AsyncMock(return_value=segments)), \
         patch.object(service, "_save", AsyncMock()) as save, \
         patch("services.compliance.submissions.submission_service.recheck_after_transcript", AsyncMock()) as recheck, \
         patch("services.compliance.analytics.sentiment_service.on_transcript_finished", AsyncMock()):
        asyncio.run(service.transcribe_file(file_id))
    save.assert_awaited_once_with(file_id, "COMPLETED", transcript=segments)
    recheck.assert_awaited_once_with(file_id)


@pytest.mark.parametrize("retry_failed", [True, False])
def test_only_explicit_reruns_retry_failed_transcription(retry_failed):
    file_id, submission_id = uuid4(), uuid4()
    conn = SimpleNamespace(fetch=AsyncMock(side_effect=[
        [dict(id=file_id, submission_id=submission_id)] if retry_failed else [], [dict(id=file_id)]]))
    @asynccontextmanager
    async def acquire():
        yield conn
    pool = SimpleNamespace(acquire=acquire)
    with patch.object(service, "get_pool", return_value=pool), \
         patch("services.ai.observability.ai_usage_service.reserve", AsyncMock()), \
         patch.object(service, "schedule") as schedule:
        asyncio.run(service.schedule_missing([file_id], retry_failed=retry_failed))
    assert conn.fetch.call_args_list[0].args[2] is retry_failed
    assert schedule.called is retry_failed


def test_comparison_uses_spoken_words_not_the_approved_script_as_a_transcript():
    match = script_match.assess_video("We deliver medical supplies safely every day",
        "Thank you for visiting our booth at the conference", {"transcript_status": "COMPLETED"})
    assert match["status"] == "MISMATCH"
    assert match["similarity"] < 50


def test_failed_overlay_read_keeps_independently_transcribed_speech():
    spoken = [dict(start_sec=0., end_sec=1., text="Actual spoken words", source="speech")]
    with patch.object(provider.local_speech, "speech_segments", return_value=spoken), \
         patch.object(provider, "is_bedrock_enabled", return_value=True), \
         patch.object(provider, "sample_parts", side_effect=ValueError("unsupported decoder option")):
        with pytest.raises(provider.PartialTranscriptError) as error:
            asyncio.run(provider.transcribe("actual.mp4", "video/mp4", "", service.RESPONSE_SCHEMA))
    assert error.value.segments == spoken
