"""Voice input/output module.

Provides speech-to-text and text-to-speech capabilities with support
for multiple providers.

Modules:
    - SpeechToText: STT handler (can be used standalone)
    - TextToSpeech: TTS handler (can be used standalone)

Quick Start:
    >>> from voice import SpeechToText, TextToSpeech

Advanced Usage:
    >>> # Mix providers: OpenAI STT + custom TTS
    >>> stt = SpeechToText(provider="openai")
    >>> tts = TextToSpeech(provider="openai", voice="nova")
"""

from voice.stt import SpeechToText
from voice.tts import TextToSpeech

__all__ = ["SpeechToText", "TextToSpeech"]
