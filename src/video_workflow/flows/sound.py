"""Sound: narration per shot, the music track, and the extra-language versions of a finished video."""

from __future__ import annotations

import copy
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import quality
from ..models import ContentItem, Version, VoiceClip
from ..video import probe
from ..video.base import download
from ..voice import text_hash
from .common import AUDIO_EXT, audio_ext

log = logging.getLogger(__name__)


class SoundFlow:
    # ---- narration and music --------------------------------------------------------------
    def synthesize_voice(self, item: ContentItem) -> list[VoiceClip]:
        if self.voice is None or item.script is None:
            return []
        out_dir = self.store.item_dir(item.id) / "voice"
        out_dir.mkdir(parents=True, exist_ok=True)
        cached = {v.shot_index: v for v in item.voice_clips}
        vc = self.config.video.voice

        def one(shot) -> VoiceClip | None:
            text = shot.voiceover.strip()
            if not text:
                return None
            digest = text_hash(self.voice.name, vc.voice, text)
            prev = cached.get(shot.index)
            if prev and prev.text_hash == digest and Path(prev.path).exists():
                return prev
            path = self.voice.synthesize(text, out_dir / f"shot_{shot.index:02d}.{self.voice.ext}")
            if self.voice.name == "higgsfield":
                # Spoken by the offline fallback: mark it so the next render asks Higgsfield again.
                digest = text_hash("fallback", vc.voice, text)
            return VoiceClip(shot_index=shot.index, path=str(path), text_hash=digest,
                             duration=probe(path)["duration"])

        with ThreadPoolExecutor(max_workers=4) as pool:
            clips = [c for c in pool.map(one, item.script.shots) if c is not None]
        item.voice_clips = clips
        return clips

    def fallback_voice_shots(self, item: ContentItem) -> list[int]:
        """Shots whose narration was spoken by the offline fallback, not Higgsfield."""
        lines = {s.index: s.voiceover.strip() for s in (item.script.shots if item.script else [])}
        voice_id = self.config.video.voice.voice
        return [v.shot_index for v in item.voice_clips
                if v.text_hash == text_hash("fallback", voice_id, lines.get(v.shot_index, ""))]

    def add_audio(self, item: ContentItem, kind: str, url: str | None = None, shot: int | None = None,
                  job: str | None = None, path: str | Path | None = None) -> ContentItem:
        """Register a narration line (kind="voice", per shot) or the video's music track (kind="music")."""
        audio_dir = self.store.item_dir(item.id) / "audio"
        if kind == "voice":
            line = next((s for s in (item.script.shots if item.script else []) if s.index == shot), None)
            if line is None or not line.voiceover.strip():
                raise ValueError(f"Item {item.id} has no narration for shot {shot}")
            dest = Path(path) if path else download(url, audio_dir / f"voice_{shot:02d}{audio_ext(url)}", self.http)
            digest = text_hash("higgsfield", self.config.video.voice.voice, line.voiceover.strip())
            item.voice_clips = [v for v in item.voice_clips if v.shot_index != shot] + [
                VoiceClip(shot_index=shot, path=str(dest), text_hash=digest, duration=probe(dest)["duration"])]
            item.voice_clips.sort(key=lambda v: v.shot_index)
        elif kind == "music":
            dest = Path(path) if path else download(url, audio_dir / f"music{audio_ext(url)}", self.http)
            item.music_generated = str(dest)
        else:
            raise ValueError(f"Unknown audio kind {kind!r} (voice | music)")
        self.tracer.event(f"audio.{kind}", "ok", item.id, f"{kind}{'' if shot is None else f' shot {shot + 1}'}"
                          f" · {probe(dest)['duration']:.1f}s" + (f" · job {job}" if job else ""))
        return self.store.save(item)

    def pick_music(self, item: ContentItem) -> Path | None:
        music = self.config_for(item).video.music
        if not music.enabled:
            return None
        if item.music_generated and Path(item.music_generated).exists():
            return Path(item.music_generated)   # the original track made for this video
        folder = music.dir
        if not folder or not Path(folder).is_dir():
            return None
        tracks = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in AUDIO_EXT)
        if not tracks:
            return None

        def words(text: str) -> set[str]:
            return set(text.lower().replace("-", " ").replace("_", " ").split())

        mood = words(item.script.music_mood if item.script else "")
        best = sorted((t for t in tracks if mood & words(t.stem)), key=lambda t: -len(mood & words(t.stem)))
        if best:
            return best[0]
        return tracks[int(hashlib.sha1(item.id.encode()).hexdigest(), 16) % len(tracks)]

    # ---- subtitle tracks -----------------------------------------------------------------------
    def upload_captions(self, item: ContentItem) -> list[str]:
        """The narration as YouTube subtitles: the channel's language plus every language version. Each
        track once; a failure is logged and never undoes the publish. Returns the languages uploaded."""
        from ..kit import srt

        upload = getattr(self.publishers.get("youtube"), "upload_caption", None)
        video_id = item.post_id("youtube")
        if not (self.config.publish.youtube.captions and upload and video_id and item.script and item.kind == "short"):
            return []
        durations = [s.duration_s for s in item.script.shots]
        tracks = [(self.config.channel.language, [s.voiceover or s.on_screen_text for s in item.script.shots])]
        tracks += [(v.language, [s.voiceover or s.on_screen_text for s in v.localization.shots]) for v in item.versions]
        done = []
        for language, lines in tracks:
            if language in item.caption_tracks or not any(line.strip() for line in lines):
                continue
            try:
                upload(video_id, language, f"{language} · narration", srt(lines, durations))
                item.caption_tracks.append(language)
                done.append(language)
            except Exception as exc:
                self.tracer.event("publish.captions", "warn", item.id, f"{language}: {exc}"[:300])
        if done:
            self.store.save(item)
            self.tracer.event("publish.captions", "ok", item.id, f"subtitles on YouTube: {', '.join(done)}")
        return done

    # ---- languages ------------------------------------------------------------------------
    def _voice_for(self, item: ContentItem, language: str, texts: list[str]) -> list[VoiceClip]:
        if self.voice is None or (self.voice.name == "flite" and not language.startswith("en")):
            return []  # flite only speaks English: the version keeps captions + clip audio + music
        provider = copy.copy(self.voice)
        override = self.config.video.voice.voices.get(language)
        if override and hasattr(provider, "voice"):
            provider.voice = override
        out_dir = self.store.item_dir(item.id) / "voice" / language
        out_dir.mkdir(parents=True, exist_ok=True)

        def one(pair) -> VoiceClip | None:
            index, text = pair
            if not text.strip():
                return None
            path = provider.synthesize(text.strip(), out_dir / f"shot_{index:02d}.{provider.ext}")
            return VoiceClip(shot_index=index, path=str(path), text_hash=text_hash(provider.name, language, text),
                             duration=probe(path)["duration"])

        with ThreadPoolExecutor(max_workers=4) as pool:
            return [c for c in pool.map(one, enumerate(texts)) if c is not None]

    def localize(self, item: ContentItem, music: Path | None = None) -> list[Version]:
        """The same video in every extra language: translated narration, captions and posting copy."""
        languages = [lang for lang in self.config.content.languages
                     if lang and lang != self.config.channel.language]
        if not languages or not (item.script and item.packaging and self.editor):
            return []
        versions = []
        for lang in languages:
            with self.tracer.span(f"localize.{lang}", item.id) as span:
                try:
                    loc = self.translator.localize(item.script, item.packaging, lang)
                    clips = self._voice_for(item, lang, [s.voiceover for s in loc.shots])
                    final, thumb = self._editor_for(item).assemble(
                        item, self.store.item_dir(item.id), None, music, phase="final", variant=lang,
                        texts=[(s.on_screen_text, s.voiceover) for s in loc.shots], voice_clips=clips)
                    variant = item.model_copy(deep=True)
                    for shot, loc_shot in zip(variant.script.shots, loc.shots, strict=True):
                        shot.voiceover, shot.on_screen_text = loc_shot.voiceover, loc_shot.on_screen_text
                    variant.voice_clips = clips
                    report = quality.check_video(variant, self.config, final, self._final_size(),
                                                 captions=final.parent / f"edit_{lang}" / "captions.ass")
                    versions.append(Version(language=lang, localization=loc, voice_clips=clips,
                                            final_video=str(final), thumbnail=str(thumb), quality=report))
                    span.detail = (f"{loc.title[:60]} · {len(clips)} narration clip(s) · "
                                   f"quality {report.score:.0f}/100")
                except Exception as exc:  # one language failing must not block the video
                    log.exception("localization %s failed for %s", lang, item.id)
                    span.status, span.detail = "error", f"{type(exc).__name__}: {exc}"[:500]
        item.versions = versions
        self.store.save(item)
        return versions
