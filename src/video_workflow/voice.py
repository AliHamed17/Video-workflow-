"""Text-to-speech for narration. Every provider writes one audio file per shot.

* ``flite``      — free and offline (ffmpeg's built-in libflite). Robotic, but good for drafts/tests.
* ``openai``     — OpenAI speech API (e.g. gpt-4o-mini-tts), with delivery ``instructions``.
* ``elevenlabs`` — ElevenLabs voices (most natural; voice cloning).
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

import httpx

from . import net
from .config import Secrets, VoiceConfig


class VoiceError(RuntimeError):
    pass


class VoiceProvider(Protocol):
    name: str
    ext: str

    def synthesize(self, text: str, dest: Path) -> Path: ...


def text_hash(provider: str, voice: str, text: str) -> str:
    return hashlib.sha256(f"{provider}|{voice}|{text}".encode()).hexdigest()[:16]


class FliteVoice:
    name, ext = "flite", "wav"

    def __init__(self, voice: str = "slt"):
        self.voice = voice or "slt"

    def synthesize(self, text: str, dest: Path) -> Path:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
            fh.write(text)
            text_file = fh.name
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                 f"flite=textfile={text_file}:voice={self.voice}", "-ar", "48000", "-ac", "2", str(dest)],
                capture_output=True, text=True)
        finally:
            Path(text_file).unlink(missing_ok=True)
        if proc.returncode != 0:
            raise VoiceError(f"flite failed: {proc.stderr[-400:]}")
        return dest


class OpenAIVoice:
    name, ext = "openai", "wav"
    URL = "https://api.openai.com/v1/audio/speech"

    def __init__(self, api_key: str, voice: str = "", model: str = "", instructions: str = "",
                 http: httpx.Client | None = None):
        self.api_key, self.voice = api_key, voice or "alloy"
        self.model, self.instructions = model or "gpt-4o-mini-tts", instructions
        self.http = http or net.default_client()

    def synthesize(self, text: str, dest: Path) -> Path:
        body = {"model": self.model, "voice": self.voice, "input": text, "response_format": "wav"}
        if self.instructions:
            body["instructions"] = self.instructions
        resp = net.send(self.http, "POST", self.URL, json=body,
                        headers={"Authorization": f"Bearer {self.api_key}"})
        if resp.status_code >= 400:
            raise VoiceError(f"OpenAI TTS HTTP {resp.status_code}: {resp.text[:300]}")
        dest.write_bytes(resp.content)
        return dest


class ElevenLabsVoice:
    name, ext = "elevenlabs", "mp3"

    def __init__(self, api_key: str, voice: str, model: str = "", http: httpx.Client | None = None):
        if not voice:
            raise VoiceError("Set video.voice.voice to an ElevenLabs voice_id")
        self.api_key, self.voice = api_key, voice
        self.model = model or "eleven_multilingual_v2"
        self.http = http or net.default_client()

    def synthesize(self, text: str, dest: Path) -> Path:
        resp = net.send(
            self.http, "POST", f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice}",
            params={"output_format": "mp3_44100_128"}, headers={"xi-api-key": self.api_key},
            json={"text": text, "model_id": self.model})
        if resp.status_code >= 400:
            raise VoiceError(f"ElevenLabs HTTP {resp.status_code}: {resp.text[:300]}")
        dest.write_bytes(resp.content)
        return dest


def build_voice(config: VoiceConfig, secrets: Secrets, http: httpx.Client | None = None) -> VoiceProvider | None:
    if config.provider == "none":
        return None
    if config.provider == "flite":
        return FliteVoice(config.voice)
    if config.provider == "openai":
        if not secrets.openai_api_key:
            raise VoiceError("video.voice.provider=openai needs OPENAI_API_KEY")
        return OpenAIVoice(secrets.openai_api_key.get_secret_value(), config.voice, config.model,
                           config.instructions, http)
    if config.provider == "elevenlabs":
        if not secrets.elevenlabs_api_key:
            raise VoiceError("video.voice.provider=elevenlabs needs ELEVENLABS_API_KEY")
        return ElevenLabsVoice(secrets.elevenlabs_api_key.get_secret_value(), config.voice, config.model, http)
    raise VoiceError(f"Unknown voice provider {config.provider!r}")
