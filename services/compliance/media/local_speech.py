"""Transcribe actual local audio; never send private speech to a text-only provider."""
import os
import subprocess
import threading
from pathlib import Path

import numpy as np
from services.compliance.media.canva_mock_render import _ffmpeg

_SAMPLE_RATE = 16000
_MAX_SECONDS = 1800
_lock = threading.Lock()
_pipeline = None


def audio_samples(path):
    result = subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path),
                             "-vn", "-map", "0:a:0?", "-ac", "1", "-ar", str(_SAMPLE_RATE),
                             "-t", str(_MAX_SECONDS + 1), "-f", "f32le", "pipe:1"],
                            capture_output=True, timeout=90, check=False)
    if result.returncode:
        error = result.stderr.decode(errors="replace")
        if "does not contain any stream" in error:
            return np.array([], dtype=np.float32)
        raise RuntimeError("Could not decode the video's audio track")
    samples = np.frombuffer(result.stdout, dtype=np.float32).copy()
    if len(samples) > _MAX_SECONDS * _SAMPLE_RATE:
        raise ValueError("Video exceeds the 30-minute local transcription limit")
    return samples


def speech_segments(path):
    """Run a cached public Whisper model on CPU. Approved script text is never an input."""
    global _pipeline
    samples = audio_samples(path)
    if not len(samples) or np.max(np.abs(samples)) < 1e-5:
        return []
    with _lock:
        if _pipeline is None:
            import torch
            from transformers import pipeline
            torch.set_num_threads(max(1, int(os.getenv("SPEECH_CPU_THREADS", "4"))))
            cache = Path(os.getenv("SPEECH_MODEL_CACHE", ".data/speech_models")).resolve()
            cache.mkdir(parents=True, exist_ok=True)
            _pipeline = pipeline("automatic-speech-recognition", device=-1,
                model=os.getenv("SPEECH_MODEL", "openai/whisper-base"),
                model_kwargs={"cache_dir": str(cache), "use_safetensors": True}, trust_remote_code=False)
        response = _pipeline({"raw": samples, "sampling_rate": _SAMPLE_RATE}, return_timestamps=True,
                             chunk_length_s=30, stride_length_s=3,
                             generate_kwargs={"task": "transcribe", "condition_on_prev_tokens": False})
    duration = len(samples) / _SAMPLE_RATE
    segments = []
    for chunk in response.get("chunks", []):
        text = chunk.get("text", "").strip()
        start, end = chunk.get("timestamp", (None, None))
        if text and start is not None:
            start = min(duration, max(0., float(start)))
            end = max(start, min(duration, float(end) if end is not None else duration))
            segments.append(dict(start_sec=round(start, 2), end_sec=round(end, 2), text=text, source="speech"))
    if response.get("text", "").strip() and not segments:
        raise ValueError("Speech recognition returned text without usable timestamps")
    return segments
