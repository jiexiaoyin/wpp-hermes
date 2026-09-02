"""WPP STT skill loader."""
import sys
from pathlib import Path

_SKILLS_DIR = Path("/root/.hermes/skills")
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))

from wpp_stt import transcribe, _siliconflow_stt, decode_silk_to_pcm, build_wav_buffer, STTResult

__all__ = [
    "transcribe", "_siliconflow_stt", "decode_silk_to_pcm",
    "build_wav_buffer", "STTResult",
]