"""Local text-to-speech via Kokoro-82M (ONNX Runtime).

Free, offline, cross-vendor. Runs on CPU (real-time on a modern box) or any
ONNX Runtime execution provider (CUDA, DirectML, etc.) if installed. Returns
24 kHz / 16-bit / mono PCM wrapped in a WAV container so the browser can play
it directly via ``new Audio(...)``.
"""

from __future__ import annotations

import io
import os
import struct
import threading
from collections import OrderedDict

import numpy as np


TTS_MODEL_DEFAULT = "kokoro-v1.0"
_SAMPLE_RATE = 24000

# Model files live under the workspace's instance/ folder by default. Override
# via KOKORO_MODEL_PATH / KOKORO_VOICES_PATH if you want to move them.
_DEFAULT_INSTANCE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance", "kokoro"
)
_MODEL_PATH = os.getenv("KOKORO_MODEL_PATH") or os.path.join(_DEFAULT_INSTANCE_DIR, "kokoro-v1.0.onnx")
_VOICES_PATH = os.getenv("KOKORO_VOICES_PATH") or os.path.join(_DEFAULT_INSTANCE_DIR, "voices-v1.0.bin")

# Curated voice catalog. Kokoro ships 54 voices across 9 languages; we surface
# the English ones by default (American + British) since the UI is English.
_VOICE_CATALOG = [
    # American English — female
    ("af_heart",    "en-us", "American female · warm"),
    ("af_bella",    "en-us", "American female · bright"),
    ("af_sarah",    "en-us", "American female · natural"),
    ("af_nicole",   "en-us", "American female · calm"),
    ("af_sky",      "en-us", "American female · airy"),
    ("af_nova",     "en-us", "American female · lively"),
    ("af_aoede",    "en-us", "American female · smooth"),
    ("af_jessica",  "en-us", "American female · relaxed"),
    ("af_kore",     "en-us", "American female · steady"),
    ("af_river",    "en-us", "American female · mellow"),
    ("af_alloy",    "en-us", "American female · clear"),
    # American English — male
    ("am_michael",  "en-us", "American male · warm"),
    ("am_adam",     "en-us", "American male · deep"),
    ("am_eric",     "en-us", "American male · confident"),
    ("am_liam",     "en-us", "American male · casual"),
    ("am_onyx",     "en-us", "American male · smooth"),
    ("am_puck",     "en-us", "American male · playful"),
    ("am_echo",     "en-us", "American male · resonant"),
    ("am_fenrir",   "en-us", "American male · gruff"),
    # British English — female
    ("bf_emma",     "en-gb", "British female · warm"),
    ("bf_isabella", "en-gb", "British female · refined"),
    ("bf_alice",    "en-gb", "British female · bright"),
    ("bf_lily",     "en-gb", "British female · gentle"),
    # British English — male
    ("bm_george",   "en-gb", "British male · classic"),
    ("bm_daniel",   "en-gb", "British male · steady"),
    ("bm_fable",    "en-gb", "British male · storyteller"),
    ("bm_lewis",    "en-gb", "British male · casual"),
]

_VOICE_LANGS = {name: lang for name, lang, _ in _VOICE_CATALOG}

_MAX_TEXT_CHARS = 5000  # generous cap; Kokoro handles long text fine

# In-process response cache keyed by (voice, speed, text). Small — enough to
# make replays and toggle-preview clicks instant without hoarding audio.
_CACHE_LOCK = threading.Lock()
_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_CACHE_MAX_ENTRIES = 32

# The Kokoro model is loaded lazily on first use. Loading takes ~1-2 s and
# ~350 MB of memory, so we don't want to pay that cost at Flask import time.
_MODEL_LOCK = threading.Lock()
_MODEL = None
_MODEL_ERROR: str | None = None


class TTSError(RuntimeError):
    pass


def _pcm_to_wav(pcm: bytes, sample_rate: int = _SAMPLE_RATE, channels: int = 1,
                sample_width: int = 2) -> bytes:
    """Wrap raw PCM in a minimal RIFF/WAVE container (no external deps)."""
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    data_size = len(pcm)
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + data_size))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, sample_width * 8))
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(pcm)
    return buf.getvalue()


def _cache_get(key):
    with _CACHE_LOCK:
        val = _CACHE.get(key)
        if val is not None:
            _CACHE.move_to_end(key)
        return val


def _cache_put(key, value: bytes):
    with _CACHE_LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX_ENTRIES:
            _CACHE.popitem(last=False)


def is_configured() -> bool:
    """True if the Kokoro model files are present and the package is importable."""
    if not (os.path.isfile(_MODEL_PATH) and os.path.isfile(_VOICES_PATH)):
        return False
    try:
        import kokoro_onnx  # noqa: F401
    except Exception:
        return False
    return True


def _get_model():
    """Lazy-load the Kokoro ONNX model. Thread-safe."""
    global _MODEL, _MODEL_ERROR
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        if not os.path.isfile(_MODEL_PATH):
            _MODEL_ERROR = f"Kokoro model file missing: {_MODEL_PATH}"
            raise TTSError(_MODEL_ERROR)
        if not os.path.isfile(_VOICES_PATH):
            _MODEL_ERROR = f"Kokoro voices file missing: {_VOICES_PATH}"
            raise TTSError(_MODEL_ERROR)
        try:
            from kokoro_onnx import Kokoro
        except Exception as exc:  # noqa: BLE001
            _MODEL_ERROR = f"kokoro_onnx not installed: {exc}"
            raise TTSError(_MODEL_ERROR) from exc
        try:
            _MODEL = Kokoro(_MODEL_PATH, _VOICES_PATH)
        except Exception as exc:  # noqa: BLE001
            _MODEL_ERROR = f"failed to load Kokoro: {exc}"
            raise TTSError(_MODEL_ERROR) from exc
    return _MODEL


def list_voices():
    """Voice catalog for the /api/tts/voices endpoint."""
    return [{"name": name, "description": desc, "lang": lang}
            for name, lang, desc in _VOICE_CATALOG]


def synthesize(text: str, voice: str = "af_heart", style: str | None = None,
               model: str = TTS_MODEL_DEFAULT) -> bytes:
    """Return WAV bytes for ``text`` spoken by ``voice``.

    ``style`` is accepted for API compatibility but ignored — Kokoro doesn't
    expose free-form style prompts. Raises :class:`TTSError` on failure.
    """
    text = (text or "").strip()
    if not text:
        raise TTSError("empty text")
    if voice not in _VOICE_LANGS:
        raise TTSError(f"unknown voice: {voice}")
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS]

    # Slightly slower than default — reduces consonant slurring on
    # tricky words (acronyms, proper nouns) without sounding sluggish.
    speed = 0.9
    cache_key = (model, voice, speed, text)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    kokoro = _get_model()
    lang = _VOICE_LANGS[voice]

    # kokoro-onnx isn't safe for concurrent .create() calls; serialize them.
    with _MODEL_LOCK:
        try:
            samples, sr = kokoro.create(text, voice=voice, speed=speed, lang=lang)
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"kokoro failed: {exc}") from exc

    if samples is None or len(samples) == 0:
        raise TTSError("empty audio payload")

    # Kokoro returns float32 in [-1, 1] — convert to signed 16-bit PCM.
    arr = np.asarray(samples, dtype=np.float32)
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767.0).astype(np.int16).tobytes()
    wav = _pcm_to_wav(pcm, sample_rate=int(sr) if sr else _SAMPLE_RATE)
    _cache_put(cache_key, wav)
    return wav
