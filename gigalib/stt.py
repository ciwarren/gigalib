"""Local speech-to-text via faster-whisper (CTranslate2).

Free, offline, cross-vendor. Runs on CPU by default (real-time for short
chat-input clips on any modern box) and can use CUDA if the CTranslate2
CUDA wheel + cuDNN are available. Model files auto-download from HuggingFace
into ``instance/whisper/`` on first use.

Override defaults with env vars:
    WHISPER_MODEL         model size (tiny|base|small|medium|large-v3) - default "base"
    WHISPER_DEVICE        cpu|cuda|auto  - default "cpu"
    WHISPER_COMPUTE_TYPE  int8|int8_float16|float16|float32 - default "int8"
    WHISPER_CACHE_DIR     override model cache directory
"""

from __future__ import annotations

import io
import os
import threading


STT_MODEL_DEFAULT = os.getenv("WHISPER_MODEL", "base")
_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

_DEFAULT_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance", "whisper"
)
_CACHE_DIR = os.getenv("WHISPER_CACHE_DIR") or _DEFAULT_CACHE_DIR

_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MiB — plenty for a minute of chat

_MODEL = None
_MODEL_ERROR: str | None = None
_MODEL_LOCK = threading.Lock()


class STTError(RuntimeError):
    pass


def is_configured() -> bool:
    """Whether faster-whisper is importable. Model files download on first use."""
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _get_model():
    global _MODEL, _MODEL_ERROR
    if _MODEL is not None:
        return _MODEL
    if _MODEL_ERROR is not None:
        raise STTError(_MODEL_ERROR)
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        try:
            from faster_whisper import WhisperModel

            os.makedirs(_CACHE_DIR, exist_ok=True)
            _MODEL = WhisperModel(
                STT_MODEL_DEFAULT,
                device=_DEVICE,
                compute_type=_COMPUTE_TYPE,
                download_root=_CACHE_DIR,
            )
        except Exception as exc:  # noqa: BLE001
            _MODEL_ERROR = f"failed to load faster-whisper: {exc}"
            raise STTError(_MODEL_ERROR) from exc
    return _MODEL


def transcribe(audio_bytes: bytes, mime_type: str | None = None,
               model: str = STT_MODEL_DEFAULT) -> str:
    """Return a transcript of ``audio_bytes``.

    ``mime_type`` is accepted for API compatibility; faster-whisper (via its
    bundled ffmpeg decoder) sniffs the container itself.
    """
    if not audio_bytes:
        raise STTError("empty audio payload")
    if len(audio_bytes) > _MAX_AUDIO_BYTES:
        raise STTError(f"audio too large ({len(audio_bytes)} > {_MAX_AUDIO_BYTES} bytes)")

    whisper = _get_model()

    audio_stream = io.BytesIO(audio_bytes)
    try:
        # WhisperModel isn't safe for concurrent .transcribe() calls;
        # serialize them the same way we do for Kokoro.
        with _MODEL_LOCK:
            segments, _info = whisper.transcribe(
                audio_stream,
                language="en",     # chat UI is English; drop if you want auto-detect
                vad_filter=True,   # skip silences → less hallucinated filler
                beam_size=1,       # greedy is plenty for short clips and much faster
            )
            text = " ".join(seg.text.strip() for seg in segments if seg.text)
    except Exception as exc:  # noqa: BLE001
        raise STTError(f"whisper failed: {exc}") from exc

    text = text.strip()
    if not text:
        raise STTError("no transcript returned")
    return text
