"""Speech in (mic + voice activity detection + local Whisper) and speech out (Piper neural TTS, OpenAI,
ElevenLabs or espeak) with mouth movement driven by the actual loudness of the audio being played."""

import collections
import io
import json
import queue
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from config import resolve

try:
    import sounddevice as sd
except Exception:  # PortAudio missing: run without sound (typed questions, silent speech)
    sd = None


def _find_device(name, kind):
    if not sd or not name:
        return None
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d["name"].lower() and d[f"max_{kind}_channels"] > 0:
            return i
    return None


# --------------------------------------------------------------------------------------------- mic

class Microphone:
    """Continuously reads 30 ms frames into a queue. Frames are dropped while the pumpkin is talking, unless
    barge-in is listening for someone talking over him (which needs the mic array's echo cancellation)."""

    FRAME_S = 0.03

    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.rate = int(cfg["rate"])
        self.frames = queue.Queue(maxsize=400)
        self.muted_until = 0.0
        self.user_muted = False
        self.through_speech = False   # true while barge-in listens during speech: speaking doesn't mute
        self.heard_level = 0.0        # loudness of the pumpkin's voice coming out of the speaker right now
        self.recording = None         # a list while the echo test records
        self.levels = collections.deque(maxlen=40)   # (time, loudness) of recent frames, for the level meter
        self.raw = collections.deque(maxlen=400)     # (time, every channel) of the last ~12 s, for wakeclips/
        self.name = ""
        self.ok = False
        if not sd:
            log("mic: sounddevice/PortAudio not available; type questions in the debug panel")
            return
        dev = _find_device(cfg["device"], "input")
        if dev is None:
            dev = _find_device("xvf", "input")   # XMOS arrays sometimes enumerate without "ReSpeaker"
        try:
            info = sd.query_devices(dev, "input")
            want = cfg["channel"]
            if want == "auto":
                # XVF3800 USB firmware: channel 1 is tuned for speech recognition (channel 0 is the
                # "conference" mix). ReSpeaker v2.0 and plain mics: channel 0.
                want = 1 if "xvf3800" in info["name"].lower() else 0
            channels = max(1, min(int(info["max_input_channels"]), int(want) + 1))
            self.channel = min(int(want), channels - 1)
            self.stream = sd.InputStream(device=dev, samplerate=self.rate, channels=channels, dtype="int16",
                                         blocksize=int(self.rate * self.FRAME_S), callback=self._callback)
            self.stream.start()
            self.ok = True
            self.name = info["name"]
            log(f"mic: {info['name']} (channel {self.channel})")
        except Exception as exc:
            log(f"mic: could not open ({exc}); type questions in the debug panel")

    def _callback(self, indata, frames, t, status):
        if self.recording is not None:          # echo test: every channel, never muted
            self.recording.append((time.time(), indata.copy()))
        self.raw.append((time.time(), indata.copy()))
        frame = indata[:, self.channel]
        self.levels.append((time.time(), float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))))
        if self.user_muted or time.time() < self.muted_until:
            return
        try:
            self.frames.put_nowait(frame.copy())
        except queue.Full:
            pass

    def level(self, window=0.5):
        """The loudest the mic heard in the last half second (same scale as the wake threshold)."""
        now = time.time()
        return max((lv for t, lv in list(self.levels) if now - t <= window), default=0.0)

    @property
    def is_array(self):
        """A reSpeaker/XVF mic array, whose firmware cancels the echo of whatever it plays itself."""
        return any(k in self.name.lower() for k in ("xvf", "respeaker"))

    def mute_for(self, seconds):
        self.muted_until = max(self.muted_until, time.time() + seconds)

    def flush(self):
        while not self.frames.empty():
            self.frames.get_nowait()


_VAD = None


def speech_ms(audio, rate=16000):
    """How much of this sound is a human voice, in ms, by Silero VAD (it comes with faster-whisper): typing,
    clicks, bumps and clatter score 0. None if the detector isn't available."""
    spans = speech_spans(audio, rate)
    return None if spans is None else sum(b - a for a, b in spans) * 1000


def speech_spans(audio, rate=16000):
    """Where the voice is, as (start s, end s) pairs, by Silero VAD. None if the detector isn't available."""
    global _VAD
    if _VAD is None:
        try:
            from faster_whisper.vad import VadOptions, get_speech_timestamps
            _VAD = (get_speech_timestamps, VadOptions(threshold=0.5, min_speech_duration_ms=200))
        except Exception:
            _VAD = False
    if not _VAD:
        return None
    if rate != 16000:
        n = int(len(audio) * 16000 / rate)
        audio = np.interp(np.linspace(0, len(audio), n, endpoint=False), np.arange(len(audio)), audio)
    try:
        spans = _VAD[0](np.asarray(audio, np.float32), _VAD[1])
    except Exception:
        return None
    return [(sp["start"] / 16000, sp["end"] / 16000) for sp in spans]


def _thump(frame, rate=16000):
    """A knock on a door is a low thump; typing and clicks are mostly high-pitched."""
    spec = np.abs(np.fft.rfft(frame.astype(np.float32))) ** 2
    hz = np.fft.rfftfreq(len(frame), 1 / rate)
    total = spec[hz > 40].sum()
    return total > 0 and spec[(hz > 40) & (hz < 500)].sum() / total > 0.35


class Listener:
    """Waits for one utterance using an adaptive energy threshold, then transcribes it."""

    def __init__(self, mic, cfg, stt):
        self.mic, self.cfg, self.stt = mic, cfg, stt
        self.noise = 150.0
        self.voice_hint = None         # optional: the mic array's own "someone is speaking" flag
        self.interrupted = threading.Event()
        self._carry = []               # audio of whoever interrupted, picked up by the next listen()
        self._watcher = None
        self.echo_gain, self._echo_frames = 0.0, 0
        self.timing = {}
        self.on_busy = None            # called with True/False around speech recognition (pauses the camera)
        self.log = None                # optional: for a line every 10 s about hearing over the idle music
        self.wake_bar = None           # hearing threshold for waking (mic level); None = automatic (just above the room and music level, >= 50)
        self.wake_flag = True          # also need the mic array's voice light (off if the test finds it misses voices)
        self.need = None               # how loud a voice must be to wake him right now (for the level meter)
        self.test = None               # the hearing test: {"until", "frames", "flags", "done"} while it records

    # ---- barge-in: someone talks over the pumpkin -------------------------------------------------
    def start_watch(self):
        """Listen while the pumpkin talks. Sets `interrupted` when someone clearly talks over him, and keeps
        their words so the next listen() hears the whole sentence. Only sensible with echo cancellation
        (speaker plugged into the mic array), or he interrupts himself."""
        self.stop_watch()
        if not self.mic.ok or self.stt is None:
            return
        self.interrupted.clear()
        self._carry = []
        self.mic.flush()
        self.mic.through_speech = True
        self._watching = True
        self._watcher = threading.Thread(target=self._watch, daemon=True, name="barge-in")
        self._watcher.start()

    def reset(self):
        """Forget an interruption that nobody listened to (a new visit starts)."""
        self.stop_watch()
        self.interrupted.clear()
        self._carry = []

    def stop_watch(self):
        self._watching = False
        if self._watcher:
            self._watcher.join(timeout=0.5)
            self._watcher = None
        self.mic.through_speech = False

    def _watch(self):
        """Talking over him = loud for most of a short window, louder than the room's background noise and
        clearly louder than the echo of his own voice. The echo is predicted from what the speaker is
        playing right now times how strongly the speaker reaches the mic (learned while he talks). With
        echo cancellation that echo is tiny; with a separate speaker it is large, so kids have to talk up."""
        cfg, frame_s = self.cfg, Microphone.FRAME_S
        grace_until = time.time() + float(cfg["barge_grace_s"])
        window = max(2, int(round(float(cfg["barge_window_s"]) / frame_s)))
        need = max(2, int(round(float(cfg["barge_min_s"]) / frame_s)))
        ratio, margin = float(cfg["barge_ratio"]), float(cfg["barge_echo_margin"])
        recent, refs, loud_flags = [], [], []
        while self._watching:
            try:
                frame = self.mic.frames.get(timeout=0.1)
            except queue.Empty:
                continue
            if Speaker.paused:                                # paused: nobody is talking over him
                recent, loud_flags = [], []
                continue
            recent = (recent + [frame])[-(window + 10):]     # what they said, plus 300 ms before
            refs = (refs + [self.mic.heard_level])[-12:]      # what he played in the last ~0.35 s
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            ref = max(refs)
            steady = refs[-4:]
            if len(steady) == 4 and min(steady) > 200:      # steady playback: the echo level is clear
                self._learn_echo(rms / (sum(steady) / 4))
            if time.time() < grace_until or self._echo_frames < 30:
                continue                                      # still settling / not yet learned
            echo = self.echo_gain * ref
            limit = max(self.noise * float(cfg["start_ratio"]), float(cfg["min_rms"])) * ratio
            loud_flags = (loud_flags + [rms > max(limit, echo * margin)])[-window:]
            if sum(loud_flags) >= need and (self.voice_hint is None or self.voice_hint()):
                # He finishes his sentence anyway, so there's time to make sure it's a voice: listen ~1 s more
                # and ask Silero (a bump, a cough or a door creak isn't someone talking to him).
                heard = list(recent)
                while self._watching and len(heard) * frame_s < 1.0:
                    try:
                        heard.append(self.mic.frames.get(timeout=0.1))
                    except queue.Empty:
                        pass
                if not self._watching:
                    return
                voice_ms = speech_ms(np.concatenate(heard).astype(np.float32) / 32768.0, self.mic.rate)
                if voice_ms is not None and voice_ms < 300:
                    loud_flags = []
                    if self.log:
                        self.log(f"(talked over? no: {voice_ms:.0f} ms of voice)", "info")
                    continue
                self._carry = heard
                self.interrupted.set()
                # He may finish his sentence first: keep their words coming in meanwhile for the next listen()
                while self._watching and len(self._carry) < int(10 / frame_s):
                    try:
                        self._carry.append(self.mic.frames.get(timeout=0.1))
                    except queue.Empty:
                        pass
                return

    def _learn_echo(self, r):
        """How loud the mic hears the speaker, relative to what is played. Quick to go down, slow to go up,
        so a child talking over him doesn't teach it that his echo is loud."""
        if self._echo_frames < 30:
            self.echo_gain = r if self._echo_frames == 0 else max(self.echo_gain, r) * 0.9 + r * 0.1
        else:
            self.echo_gain += (r - self.echo_gain) * (0.15 if r < self.echo_gain else 0.01)
        self._echo_frames += 1

    # ---- waiting for a visitor (sound-triggered visits) -------------------------------------------------
    def wait_for_visitor(self, should_stop, is_wake, stt=None, knock=True, knock_ratio=8.0, knocks=2,
                         music=lambda: False, voice=None):
        """Between visits: listen for a short greeting ("trick or treat", "hello") or a knock on the door.
        Short voiced bursts are transcribed with `stt` (the Pi's own whisper, so nothing leaves the porch
        before a visit) and checked with is_wake(text). Returns ("words", text), ("knock", None), or None
        when should_stop() says so. While music() is true (his idle video's soundtrack is playing), speech and
        knocks must also be clearly louder than what's left of the music after echo cancelling (learned the
        same way barge-in learns his voice's echo). voice(), when given, is the mic array's own "someone is
        speaking" light: it ignores the speaker and clicks like typing, so speech only counts while it's on
        (and then the music's echo doesn't need to be beaten)."""
        stt = stt or self.stt
        frame_s = Microphone.FRAME_S
        margin = float(self.cfg["barge_echo_margin"])
        self.mic.flush()
        pre_roll, voiced, chunks, silent, speech = [], [], [], 0, False
        burst, hits, clock = 0, [], 0.0   # knock: how long the current loud burst is; times of short bursts
        prev, refs, loudest, report, peak = self.noise, [], 0.0, 10.0, 0.0
        flagged, flag_max = 0, 0.0        # frames the mic array called voice, and the loudest of them (log)
        recent = collections.deque(maxlen=330)   # ~10 s of frame levels: the room's floor is a low percentile
        while not should_stop():
            if Speaker.paused:              # nothing wakes him while paused
                self.mic.flush()
                pre_roll, voiced, chunks, speech, hits, burst = [], [], [], False, [], 0
                time.sleep(0.1)
                continue
            if not self.mic.ok or stt is None:
                time.sleep(0.1)
                continue
            try:
                frame = self.mic.frames.get(timeout=0.1)
            except queue.Empty:
                continue
            test = self.test
            if test:                       # the hearing test: just record (nothing wakes him meanwhile)
                test["frames"].append(frame)
                test["flags"].append(bool(voice()) if voice else True)
                if time.time() >= test["until"]:
                    self.test = None
                    threading.Thread(target=test["done"], args=(test["frames"], test["flags"]), daemon=True).start()
                    pre_roll, voiced, chunks, speech = [], [], [], False
                continue
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            clock += frame_s               # audio time (frames can arrive in bursts)
            now = clock
            recent.append(rms)
            if len(recent) >= 30 and len(recent) % 15 == 0:
                self.noise = max(30.0, float(np.percentile(recent, 20)))
            echo = 0.0
            if music():
                refs = (refs + [self.mic.heard_level])[-12:]   # what the speaker played in the last ~0.35 s
                steady = refs[-4:]
                if len(steady) == 4 and min(steady) > 200:
                    self._learn_echo(rms / (sum(steady) / 4))
                echo = self.echo_gain * max(refs) if self._echo_frames >= 30 else float("inf")
                loudest = max(loudest, rms)
                if clock >= report and self.log:
                    report = clock + 10
                    need = ((self.wake_bar if self.wake_bar else max(self.noise * 1.15, 50.0))
                            if voice else
                            max(self.noise * float(self.cfg['start_ratio']), float(self.cfg['min_rms']), echo * margin))
                    self.log(f"(over the music: room noise {self.noise:.0f}, music echo {echo:.0f}, loudest {loudest:.0f}; "
                             f"speech needs {need:.0f}; voice light on {flagged * frame_s:.1f} s, loudest then "
                             f"{flag_max:.0f})", "info")
                    loudest, flagged, flag_max = 0.0, 0, 0.0
            else:
                refs = []
            if knock:
                # A knock is a sharp, short thump: a jump far above the room noise (and the music's echo) that's
                # over in <= 90 ms.
                floor = max(self.noise * knock_ratio, float(self.cfg["min_rms"]) * 3, echo * margin * 1.5)
                sharp = rms > floor and rms > prev * 3 and _thump(frame) and not (voice and voice())
                if burst:
                    if rms > max(self.noise * knock_ratio * 0.4, float(self.cfg["min_rms"]), echo * margin):
                        burst += 1
                    else:
                        if burst <= 3:
                            if self.log:
                                self.log(f"(knock? peak {peak:.0f}, needs {floor:.0f})", "info")
                            hits = [t for t in hits if now - t < 1.5] + [now]
                            if len(hits) >= knocks and hits[-1] - hits[0] >= 0.12:
                                return ("knock", None)
                        burst = 0
                elif sharp:
                    burst, peak = 1, rms
                prev = rms
            self.need = None
            if voice:
                # The mic array says someone's talking: a low bar (its echo-cancelled channel turns voices down
                # a lot while it plays music), and Silero VAD below throws out typing and clicks.
                flag = voice()
                # the slider / hearing test value is used as is; automatic sits just above the room (and music)
                # level, since the voice light already ignores the speaker and Silero throws out the rest
                basic = self.wake_bar if self.wake_bar else max(self.noise * 1.15, 50.0)
                loud = rms > basic and (flag or not self.wake_flag)
                self.need = basic
                if flag:
                    flagged, flag_max = flagged + 1, max(flag_max, rms)
            else:
                basic = max(self.noise * float(self.cfg["start_ratio"]), float(self.cfg["min_rms"]))
                loud = rms > max(basic, echo * margin)
                self.need = max(basic, echo * margin)
            if not speech:
                pre_roll = (pre_roll + [frame])[-10:]
                voiced = (voiced + [loud])[-8:]
                if sum(voiced) >= 4:   # loud for half of a quarter second: knocks, clatter and typing aren't words
                    speech, silent, chunks = True, 0, list(pre_roll)
            else:
                chunks.append(frame)
                # once someone's talking, keep going on level alone: the voice light flickers between words
                # (a lower bar than starting: over loud music the quiet ends of words fall under it and the
                # clip got cut to "Up." before "trick or treat" was over)
                silent = 0 if (rms > basic * 0.6 or (voice and voice()) if voice else loud) else silent + 1
                if len(chunks) * frame_s > 6:     # a long conversation going past, not a greeting
                    speech, pre_roll, voiced = False, [], []
                    continue
                if silent * frame_s >= max(0.8, float(self.cfg["silence_end_s"])):
                    audio = np.concatenate(chunks).astype(np.float32) / 32768.0
                    speech, pre_roll, voiced = False, [], []
                    voice_ms = speech_ms(audio, self.mic.rate)
                    if voice_ms is not None and voice_ms < 250:   # typing, clicks, bumps: not a person talking
                        if self.log:
                            self.log(f"(not speech: {voice_ms:.0f} ms of voice in {len(audio) / self.mic.rate:.1f} s of sound)",
                                     "info")
                        continue
                    self._keep_clip(audio)
                    other = self._other_channel(len(audio) / self.mic.rate)
                    try:
                        text = stt.transcribe(audio, self.mic.rate)
                    except Exception:
                        text = ""
                    if not (text and is_wake(text)) and other is not None and (voice_ms is None or voice_ms >= 400):
                        # A clear voice but no wake word. Over loud music the echo-cancelled channel can garble
                        # voices ("context:", "Kids."), while the array's speech-recognition channel (music and
                        # all) still gets "trick or treat": give that one a listen too.
                        try:
                            again = stt.transcribe(other, self.mic.rate)
                        except Exception:
                            again = ""
                        if self.log:
                            self.log(f"(second listen, other mic channel: {again!r}; first heard {text!r})", "info")
                        text = again or text
                    self.mic.flush()   # what came in while transcribing is stale
                    if text and is_wake(text):
                        return ("words", text)
        return None

    def _other_channel(self, seconds):
        """The same moment from the mic array's other channel (the XVF3800's speech-recognition one when
        listening on the echo-cancelled one), as floats; None on a single-channel mic."""
        cut = time.time() - seconds - 0.3
        raw = [d for t, d in list(self.mic.raw) if t >= cut]
        if not raw or raw[0].shape[1] < 2:
            return None
        ch = 1 if self.mic.channel == 0 else 0
        return np.concatenate(raw)[:, ch].astype(np.float32) / 32768.0

    def _keep_clip(self, audio, keep=12):
        """The last few sounds he tried to understand between visits, as wav files in wakeclips/ (to check
        by ear what he heard, e.g. if "trick or treat" is cut short)."""
        try:
            import wave
            folder = resolve("wakeclips")
            folder.mkdir(exist_ok=True)
            name = folder / time.strftime("%H%M%S.wav")
            with wave.open(str(name), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.mic.rate)
                w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
            # and every mic channel over the same moment (to compare which one recognises voices best)
            cut = time.time() - len(audio) / self.mic.rate - 1.0
            raw = [d for t, d in list(self.mic.raw) if t >= cut]
            if raw:
                allch = np.concatenate(raw)
                with wave.open(str(name.with_suffix(".all.wav")), "wb") as w:
                    w.setnchannels(allch.shape[1])
                    w.setsampwidth(2)
                    w.setframerate(self.mic.rate)
                    w.writeframes(allch.astype(np.int16).tobytes())
            for old in sorted(folder.glob("*.wav"))[:-keep * 2]:
                old.unlink()
        except Exception:
            pass

    # ---- one utterance ------------------------------------------------------------------------------
    def listen(self, timeout_s, typed, should_stop, on_speech=None):
        """Returns heard text, a typed question, or None after timeout_s of nobody speaking."""
        carry, self._carry = self._carry, []
        if not carry:
            self.mic.flush()
        deadline = time.time() + timeout_s
        pre_roll, voiced, silent, speech = [], [], 0, False
        frame_s = Microphone.FRAME_S
        if carry:                       # someone interrupted: they are mid-sentence already
            speech, chunks = True, list(carry)
            if on_speech:
                on_speech()
        while True:
            if should_stop():
                return None
            try:
                return typed.get_nowait()
            except queue.Empty:
                pass
            if Speaker.paused:
                self.mic.flush()
                deadline = max(deadline, time.time() + 2)   # after Resume, a moment to start talking again
                time.sleep(0.1)
                continue
            if not speech and time.time() > deadline:
                return None
            if not self.mic.ok or self.stt is None:
                time.sleep(0.05)
                continue
            try:
                frame = self.mic.frames.get(timeout=0.1)
            except queue.Empty:
                continue
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            loud = rms > max(self.noise * float(self.cfg["start_ratio"]), float(self.cfg["min_rms"]))
            if not speech:
                pre_roll = (pre_roll + [frame])[-10:]          # keep 300 ms before speech starts
                voiced = voiced + [frame] if loud else []
                if not loud:
                    self.noise = 0.95 * self.noise + 0.05 * rms  # track background noise
                if len(voiced) >= 3:
                    speech, silent = True, 0
                    chunks = pre_roll
                    if on_speech:
                        on_speech()
            else:
                chunks.append(frame)
                silent = 0 if loud else silent + 1
                too_long = len(chunks) * frame_s > float(self.cfg["max_utterance_s"])
                if silent * frame_s >= float(self.cfg["silence_end_s"]) or too_long:
                    audio = np.concatenate(chunks).astype(np.float32) / 32768.0
                    voice_ms = speech_ms(audio, self.mic.rate)
                    if voice_ms is not None and voice_ms < 200:   # a click or bump, not someone talking
                        speech, pre_roll, voiced = False, [], []
                        continue
                    stt_from = time.time()
                    pause = self.on_busy if getattr(self.stt, "local", True) else None
                    if pause:
                        pause(True)
                    try:
                        text = self.stt.transcribe(audio, self.mic.rate)
                    finally:
                        if pause:
                            pause(False)
                    # for the response-time log: when they stopped talking, and how long recognition took
                    self.timing = {"stopped": stt_from - silent * frame_s, "wait": silent * frame_s,
                                   "stt": time.time() - stt_from}
                    if text:
                        return text
                    # Nothing intelligible: keep listening until the deadline.
                    speech, pre_roll, voiced = False, [], []
                    deadline = max(deadline, time.time() + 2)


# --------------------------------------------------------------------------------------------- STT

_HALLUCINATIONS = {"", "you", "thank you.", "thanks for watching!", "thank you for watching.", ".", "bye."}
STT_PROMPT = "Kids trick-or-treating on Halloween talking to a jack-o'-lantern."


def _clean_transcript(text):
    """Drop what isn't words someone said: sound tags like [knock] or (laughs), and the recognizer echoing
    its own prompt back on noise."""
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    words = " ".join(re.findall(r"[a-z]+", text.lower()))
    prompt = " ".join(re.findall(r"[a-z]+", STT_PROMPT.lower()))
    if words and ((len(words.split()) >= 3 and f" {words} " in f" {prompt} ") or f"{prompt} ".startswith(words + " ")):
        return ""   # words straight out of the prompt ("Kids."): an echo, not a kid
    if re.fullmatch(r"[\s\[\](){}*\"']*\w+\s*:[\s\[\](){}*\"']*", text):
        return ""   # a lone "context:" or "transcript:": the recognizer's formatting, not a kid
    if text.lower() in _HALLUCINATIONS or re.fullmatch(r"[\W_]*", text):
        return ""
    letters = re.findall(r"[^\W\d_]", text)
    if any(not ("a" <= c.lower() <= "z") and c not in "éÉ" for c in letters):
        return ""   # letters English doesn't use ("Za celý?"): noise misheard as another language
    return text


class WhisperSTT:
    """Local speech recognition with faster-whisper; no audio leaves the device."""

    local = True

    def __init__(self, cfg, log):
        from faster_whisper import WhisperModel
        self.log = log
        started = time.time()
        self.model = WhisperModel(cfg["model"], device="cpu", compute_type=cfg["compute_type"],
                                  cpu_threads=int(cfg["threads"]), download_root=str(resolve("models/whisper")))
        log(f"speech recognition: whisper {cfg['model']} loaded in {time.time() - started:.1f}s")

    def transcribe(self, audio, rate):
        if rate != 16000:
            n = int(len(audio) * 16000 / rate)
            audio = np.interp(np.linspace(0, len(audio), n, endpoint=False), np.arange(len(audio)), audio).astype(np.float32)
        started = time.time()
        segments, _ = self.model.transcribe(
            audio, language="en", beam_size=1, condition_on_previous_text=False,
            initial_prompt=STT_PROMPT)
        parts = [s.text for s in segments if s.no_speech_prob < 0.6 and s.avg_logprob > -1.2]
        text = _clean_transcript(" ".join(p.strip() for p in parts))
        self.log(f"(heard in {time.time() - started:.1f}s: {text!r})")
        return text


class OpenAISTT:
    """Cloud speech recognition (OpenAI). Faster and better with kids' voices than whisper on the Pi, but each
    recording goes to OpenAI. Falls back to local whisper (if it loaded) when the cloud call fails."""

    local = False

    def __init__(self, cfg, log, backup=None):
        import os
        self.key = os.environ.get("OPENAI_API_KEY", "")
        if not self.key:
            raise RuntimeError("put OPENAI_API_KEY=... in .env")
        self.log, self.backup = log, backup
        self.model = cfg.get("openai_model", "gpt-4o-transcribe")
        self.timeout = float(cfg.get("openai_timeout_s", 4.0))
        self.warned = 0
        try:
            import httpx   # comes with the anthropic package
            self.http = httpx.Client(http2=False)
        except ImportError:
            self.http = None
        log(f"speech recognition: OpenAI {self.model}" + (" (whisper as backup)" if backup else ""))

    def transcribe(self, audio, rate):
        started = time.time()
        try:
            text = self._cloud(audio, rate)
        except Exception as exc:
            if not self.backup:
                raise
            if time.time() - self.warned > 60:
                detail = exc.read().decode(errors="replace")[:200] if hasattr(exc, "read") else ""
                self.log(f"speech recognition: OpenAI failed ({exc} {detail}); using whisper")
                self.warned = time.time()
            return self.backup.transcribe(audio, rate)
        text = _clean_transcript(text)
        self.log(f"(heard in {time.time() - started:.1f}s: {text!r})")
        return text

    def _cloud(self, audio, rate):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(rate))
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        fields = {"model": self.model, "language": "en", "response_format": "json",
                  "prompt": STT_PROMPT}
        headers = {"Authorization": f"Bearer {self.key}"}
        if self.http:   # keeps the connection open between turns (saves ~0.3 s a call)
            r = self.http.post("https://api.openai.com/v1/audio/transcriptions", headers=headers, data=fields,
                               files={"file": ("speech.wav", buf.getvalue(), "audio/wav")}, timeout=self.timeout)
            r.raise_for_status()
            return r.json().get("text", "").strip()
        boundary = f"----pumpkin{random.getrandbits(64):x}"
        body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                        for k, v in fields.items())
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
                 f"Content-Type: audio/wav\r\n\r\n").encode() + buf.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request("https://api.openai.com/v1/audio/transcriptions", data=body,
                                     headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read()).get("text", "").strip()

    def warm_up(self):
        """Open the connection ahead of the first question (someone just walked up)."""
        if self.http:
            try:
                self.http.get("https://api.openai.com/v1/models/" + self.model,
                              headers={"Authorization": f"Bearer {self.key}"}, timeout=self.timeout)
            except Exception:
                pass


# --------------------------------------------------------------------------------------------- TTS

def _shift_pitch(samples, semitones):
    """Lower/raise pitch by resampling. The caller pre-compensates speed so the duration stays natural."""
    if not semitones:
        return samples
    ratio = 2 ** (semitones / 12)
    n = int(len(samples) / ratio)
    return np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples.astype(np.float32)).astype(np.int16)


_PIPER_MODELS = {}   # voice file -> loaded model, shared by the pumpkin and its assistants


def piper_voice_path(name):
    """A Piper voice by name ("en_GB-alan-medium") or path; names live in models/voices/."""
    return resolve(name if ("/" in name or "\\" in name or name.endswith(".onnx")) else f"models/voices/{name}.onnx")


class PiperTTS:
    def __init__(self, cfg, log):
        from piper import PiperVoice, SynthesisConfig
        self.SynthesisConfig = SynthesisConfig
        path = str(piper_voice_path(cfg["piper_voice"]))
        if path not in _PIPER_MODELS:
            _PIPER_MODELS[path] = PiperVoice.load(path)
        self.voice = _PIPER_MODELS[path]
        self.length_scale = float(cfg["piper_length_scale"])
        self.semitones = float(cfg["pitch_semitones"])
        # chorus: extra copies of the voice at these pitch offsets, mixed in (a crowd speaking together)
        self.chorus = [float(v) for v in cfg.get("chorus", [])]
        log(f"voice: piper {Path(path).stem}, pitch {self.semitones:+g}" + (" (chorus)" if self.chorus else ""))

    def _one(self, text, semitones):
        ratio = 2 ** (semitones / 12)
        # Speak faster by `ratio`, then resample down: lower pitch at the original pace.
        syn = self.SynthesisConfig(length_scale=self.length_scale * ratio)
        chunks = list(self.voice.synthesize(text, syn))
        if not chunks:
            return np.zeros(0, np.int16), 22050
        audio = np.concatenate([np.frombuffer(c.audio_int16_bytes, np.int16) for c in chunks])
        return _shift_pitch(audio, semitones), chunks[0].sample_rate

    def synth(self, text):
        audio, rate = self._one(text, self.semitones)
        if not self.chorus or not len(audio):
            return audio, rate
        mix = audio.astype(np.float32)
        for i, offset in enumerate(self.chorus):
            extra, _ = self._one(text, self.semitones + offset)
            start = int(rate * 0.035 * (i + 1))          # slightly out of step, like several voices
            extra = extra[:max(0, len(mix) - start)].astype(np.float32)
            mix[start:start + len(extra)] += extra * 0.7
        mix *= min(1.0, 32000 / max(np.abs(mix).max(), 1))
        return mix.astype(np.int16), rate


class ElevenLabsTTS:
    """Cloud character voices. Needs ELEVENLABS_API_KEY and a voice id from your ElevenLabs voice library."""

    cloud = True

    RATE = 22050

    def __init__(self, cfg, log):
        import os
        self.key = os.environ.get("ELEVENLABS_API_KEY", "")
        self.voice_id = cfg["elevenlabs_voice_id"]
        self.model = cfg["elevenlabs_model"]
        if not self.key or not self.voice_id:
            raise RuntimeError("set ELEVENLABS_API_KEY and tts.elevenlabs_voice_id")
        log(f"voice: ElevenLabs {self.voice_id}")

    def synth(self, text):
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}?output_format=pcm_{self.RATE}",
            data=json.dumps({"text": text, "model_id": self.model}).encode(),
            headers={"xi-api-key": self.key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return np.frombuffer(r.read(), np.int16), self.RATE


class OpenAITTS:
    """OpenAI's natural voices (the same family as ChatGPT's). Needs OPENAI_API_KEY from platform.openai.com;
    a ChatGPT subscription doesn't include API use."""

    cloud = True
    RATE = 24000   # response_format "pcm" is 24 kHz, 16-bit, mono

    def __init__(self, cfg, log):
        import os
        self.key = os.environ.get("OPENAI_API_KEY", "")
        if not self.key:
            raise RuntimeError("put OPENAI_API_KEY=... in .env")
        self.voice = cfg["openai_voice"]
        self.model = cfg["openai_model"]
        self.instructions = cfg["openai_instructions"]
        self.speed = float(cfg.get("openai_speed", 1.0))
        log(f"voice: OpenAI {self.model} {self.voice}")

    def synth(self, text):
        with urllib.request.urlopen(self._request(text), timeout=30) as r:
            data = r.read()
        return np.frombuffer(data[:len(data) // 2 * 2], np.int16), self.RATE

    def _request(self, text):
        body = {"model": self.model, "voice": self.voice, "input": text, "response_format": "pcm"}
        if self.instructions and not self.model.startswith("tts-1"):
            body["instructions"] = self.instructions
        if self.speed != 1.0:
            body["speed"] = self.speed
        return urllib.request.Request(
            "https://api.openai.com/v1/audio/speech", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})

    def stream(self, text):
        """The voice as it arrives (int16 chunks at RATE), so playback can start before the whole sentence
        is made."""
        with urllib.request.urlopen(self._request(text), timeout=30) as r:
            odd = b""
            while True:
                data = r.read1(4800) if hasattr(r, "read1") else r.read(4800)
                if not data:
                    break
                data, odd = odd + data, b""
                if len(data) % 2:
                    data, odd = data[:-1], data[-1:]
                yield np.frombuffer(data, np.int16)


class EspeakTTS:
    """Robotic but zero-setup fallback (sudo apt install espeak-ng)."""

    def __init__(self, cfg, log):
        self.exe = shutil.which("espeak-ng") or shutil.which("espeak")
        if not self.exe:
            raise RuntimeError("espeak-ng not installed")
        self.voice = cfg["espeak_voice"]
        self.pitch = int(np.clip(50 + float(cfg["pitch_semitones"]) * 4, 0, 99))
        log(f"voice: espeak {self.voice}")

    def synth(self, text):
        wav = subprocess.run([self.exe, "-v", self.voice, "-p", str(self.pitch), "-s", "155", "--stdout", text],
                             capture_output=True, check=True).stdout
        with wave.open(io.BytesIO(wav)) as w:
            return np.frombuffer(w.readframes(w.getnframes()), np.int16), w.getframerate()


class _WithBackup:
    """Uses the cloud voice, and the local one for any sentence the cloud voice fails on."""

    cloud = True

    def __init__(self, main, backup, name, log):
        self.main, self.backup, self.name, self.log = main, backup, name, log
        self.warned = 0

    def synth(self, text):
        try:
            return self.main.synth(text)
        except Exception as exc:
            if time.time() - self.warned > 60:
                detail = exc.read().decode(errors="replace")[:200] if hasattr(exc, "read") else ""
                self.log(f"voice: {self.name} failed ({exc} {detail}); using the backup voice")
                self.warned = time.time()
            return self.backup.synth(text)

    @property
    def rate(self):
        return getattr(self.main, "RATE", None)

    def stream(self, text):
        """Stream the cloud voice; if it fails before any sound arrives, the whole sentence from the backup."""
        got = False
        try:
            for chunk in self.main.stream(text):
                got = True
                yield chunk, self.main.RATE
        except Exception as exc:
            if got:
                self.log(f"voice: {self.name} stream broke off ({exc})")
                return
            audio, rate = self.synth(text)
            yield audio, rate


def make_tts(cfg, log):
    engines = {"piper": PiperTTS, "openai": OpenAITTS, "elevenlabs": ElevenLabsTTS, "espeak": EspeakTTS}
    order = [cfg["engine"]] + [e for e in ("piper", "espeak") if e != cfg["engine"]]
    if cfg["engine"] == "none" or not sd:
        log("voice: no audio output; the face will mouth the words silently")
        return None
    for i, name in enumerate(order):
        try:
            tts = engines[name](cfg, log)
        except Exception as exc:
            log(f"voice: {name} unavailable ({exc})")
            continue
        if name in ("openai", "elevenlabs"):   # cloud voice: keep a local one for when it fails (no credit, no internet)
            for backup in order[i + 1:]:
                try:
                    return _WithBackup(tts, engines[backup](cfg, log), name, log)
                except Exception:
                    pass
        return tts
    log("voice: no TTS engine available; the face will mouth the words silently")
    return None


# --------------------------------------------------------------------------------------------- speaker

def _resample(audio, rate, new_rate):
    x = audio.astype(np.float32)
    if new_rate < rate and len(x) > 64:
        # Going down (e.g. 24 kHz voice to the mic array's 16 kHz): low-pass first so the highs don't fold
        # back as hiss.
        cut = 0.45 * new_rate / rate
        k = np.arange(-32, 33)
        taps = 2 * cut * np.sinc(2 * cut * k) * np.hamming(len(k))
        x = np.convolve(x, taps / taps.sum(), mode="same")
    n = int(len(x) * new_rate / rate)
    out = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(out, -32768, 32767).astype(np.int16)


class _StreamResampler:
    """_resample for audio that arrives in pieces: same low-pass and interpolation, no clicks at the joins."""

    HALF = 32

    def __init__(self, rate, new_rate):
        self.rate, self.new_rate = rate, new_rate
        self.raw = np.zeros(self.HALF, np.float32)         # input so far (with a little silence in front)
        self.filtered = np.zeros(0, np.float32)
        self.out_n = 0
        cut = 0.45 * min(1.0, new_rate / rate)
        k = np.arange(-self.HALF, self.HALF + 1)
        taps = 2 * cut * np.sinc(2 * cut * k) * np.hamming(len(k))
        self.taps = (taps / taps.sum()).astype(np.float32)

    def feed(self, chunk, last=False):
        if self.rate == self.new_rate:
            return chunk
        self.raw = np.concatenate([self.raw, chunk.astype(np.float32)] + ([np.zeros(self.HALF, np.float32)] if last else []))
        done = len(self.filtered)
        # filtered[i] is centred on raw[i + HALF]
        if len(self.raw) - done >= len(self.taps):     # (shorter input would make "valid" swap its arguments)
            new = np.convolve(self.raw[done:], self.taps, mode="valid")
            self.filtered = np.concatenate([self.filtered, new])
        step = self.rate / self.new_rate
        n_total = int((len(self.filtered) - 1) / step) + 1 if len(self.filtered) else 0
        if n_total <= self.out_n:
            return np.zeros(0, np.int16)
        pos = np.arange(self.out_n, n_total) * step
        self.out_n = n_total
        out = np.interp(pos, np.arange(len(self.filtered)), self.filtered)
        return np.clip(out, -32768, 32767).astype(np.int16)


def _trim(audio, rate, threshold=300, keep_s=0.04):
    """Cut the silence a voice engine leaves before and after a sentence; the pause settings decide the gaps."""
    loud = np.flatnonzero(np.abs(audio.astype(np.int32)) > threshold)
    if not len(loud):
        return audio
    keep = int(rate * keep_s)
    return audio[max(0, loud[0] - keep): loud[-1] + keep]


_LAUGH = re.compile(r"^\W*((ha|ho|hee|he|heh|hah|mwa|mwah)\W*)+$", re.I)


def _is_laugh(sentence):
    return bool(_LAUGH.match(sentence))


def _load_wav(path):
    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2:
            raise ValueError("needs 16-bit audio")
        audio = np.frombuffer(w.readframes(w.getnframes()), np.int16)
        if w.getnchannels() > 1:
            audio = audio.reshape(-1, w.getnchannels()).mean(axis=1).astype(np.int16)
        return audio, w.getframerate()


def _sentences(text):
    return [s.strip() for s in re.findall(r"[^.!?]+[.!?]*", text) if s.strip()] or [text]


class _Streamed:
    """A sentence whose voice is played as it arrives from the cloud."""

    def __init__(self, tts, text, pause):
        self.tts, self.text, self.pause = tts, text, pause

    def chunks(self):
        if isinstance(self.tts, _WithBackup):
            yield from self.tts.stream(self.text)
        else:
            for chunk in self.tts.stream(self.text):
                yield chunk, self.tts.RATE


class Speaker:
    """Plays speech sentence by sentence (the next sentence is synthesized while the current one plays) and
    reports a 0..1 mouth-opening level from the loudness of the audio as it is heard."""

    BLOCK_S = 1 / 60
    volume = 1.0   # shared by the pumpkin and his assistants; set from the debug panel (0 .. 1.5)
    paused = False # the Pause button: every sound holds where it is until it's cleared
    # One sound at a time: a speaker opened directly (like the mic array's jack, hw:2,0) can't play two
    # streams at once, so overlapping speech (a face demo during an echo test, say) waits its turn.
    _output_lock = threading.RLock()

    def __init__(self, tts, cfg, mic, log):
        self.tts, self.mic, self.log = tts, mic, log
        self.channels = 1
        self.device = _find_device(cfg["output_device"], "output")
        if cfg.get("output_device") and self.device is None:
            log(f"speaker: no output device matching {cfg['output_device']!r}; using the default")
        # Echo cancellation: the mic array removes its own playback from what it hears, so the pumpkin can
        # listen while he talks. That only works when he plays through the mic array (its 3.5 mm jack or
        # speaker connector), not through a separate USB speaker.
        self.aec = False
        if sd and mic.ok and mic.is_array:
            try:
                out_name = sd.query_devices(self.device, "output")["name"]
                self.aec = out_name == mic.name or any(k in out_name.lower() for k in ("xvf", "respeaker"))
            except Exception:
                pass
        self._formats = {}   # voice sample rate -> (rate, channels) the output device accepts
        # Output buffer in seconds. A small buffer underruns (choppy speech) whenever the camera, speech
        # recognition or the next sentence's synthesis keeps the CPU busy; the mouth timing allows for it.
        self.latency = float(cfg.get("output_latency", 0.25))
        # Each sentence is synthesized on its own, which trims the natural pause after it; add one back.
        self.pause = float(cfg.get("sentence_pause", 0.35))
        self.question_pause = float(cfg.get("question_pause", 1.0))   # longer beat after a question
        self.lead_in = float(cfg.get("lead_in", 0.3))
        # Extra delay for the mouth, for speakers that sound later than their buffer says (see the echo test).
        self.mouth_delay = float(cfg.get("mouth_delay", 0.0))
        if self.aec:   # played straight to the mic array: nothing to wake up, so hardly any lead-in
            self.lead_in = min(self.lead_in, float(cfg.get("array_lead_in", 0.05)))
        self.first_sound_at = None    # when the first word of the last reply was heard (response-time log)
        self.mouth_lead = float(cfg.get("mouth_lead", 0.1))
        self.stream_first = bool(cfg.get("stream_first", True))   # cloud voice: play sentence 1 as it arrives
        self._timeline, self._next_heard, self._mouth_on, self._mouth_thread = [], None, False, None
        self.laughs = []   # recorded laughs, played instead of a spoken "Ho ho ho!"
        folder = resolve(cfg.get("laugh_dir", "sounds/laughs"))
        for path in sorted(folder.glob("*.wav")) if folder.is_dir() else []:
            try:
                self.laughs.append(_load_wav(path))
            except Exception as exc:
                log(f"laugh {path.name} skipped ({exc})")
        if self.laughs:
            log(f"voice: {len(self.laughs)} recorded laughs")

    def speak(self, text, on_level, on_sentence=None, should_stop=lambda: False, stop_after=lambda: False):
        """should_stop cuts him off right away; stop_after lets him finish the sentence he's on first."""
        with Speaker._output_lock:
            self._start_mouth(on_level)
            try:
                self._speak(text, on_level, on_sentence, should_stop, stop_after)
            finally:
                self._stop_mouth(cut=should_stop())

    def _speak(self, text, on_level, on_sentence, should_stop, stop_after=lambda: False):
        self.first_sound_at = None
        if not isinstance(text, str):          # sentences still arriving (a reply being streamed in)
            return self._speak_source(iter(text), on_level, on_sentence, should_stop, stop_after)
        sentences = _sentences(text)
        if not self.laughs and getattr(self.tts, "cloud", False):
            # Spoken on its own, "Ho ho ho!" gets read out as words; said right after the joke, the voice laughs.
            merged = []
            for s in sentences:
                if merged and _is_laugh(s):
                    merged[-1] += " " + s
                else:
                    merged.append(s)
            sentences = merged
        return self._speak_source(iter(sentences), on_level, on_sentence, should_stop, stop_after)

    def _speak_source(self, source, on_level, on_sentence, should_stop, stop_after=lambda: False):
        """Speak sentences from an iterator, which may still be producing them (a reply streaming in)."""
        ready = queue.Queue()
        done = lambda: should_stop() or stop_after()   # checked between sentences

        def make(s):
            if done():
                return s, None, None
            try:
                if self.laughs and _is_laugh(s):
                    audio, rate = random.choice(self.laughs)
                else:
                    audio, rate = self.tts.synth(s) if self.tts else (None, None)
                if audio is not None and len(audio):
                    audio = _trim(audio, rate)
                    pause = self.question_pause if s.rstrip().endswith("?") else self.pause
                    if pause > 0:
                        audio = np.concatenate([audio, np.zeros(int(rate * pause), np.int16)])
            except Exception as exc:
                self.log(f"voice error: {exc}")
                audio, rate = None, None
            return s, audio, rate

        def producer():
            cloud = getattr(self.tts, "cloud", False)
            can_stream = cloud and self.stream_first and hasattr(getattr(self.tts, "main", self.tts), "stream")
            try:
                # Cloud voices take a second or more per request: ask for each sentence as soon as it is known
                # (they are played in order), so the next one is ready before the current one ends.
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for i, s in enumerate(source):
                        if done():
                            break
                        if i == 0 and can_stream and not (self.laughs and _is_laugh(s)):
                            # The first sentence plays while it is still arriving, so he starts talking sooner.
                            pause = self.question_pause if s.rstrip().endswith("?") else self.pause
                            ready.put((s, _Streamed(self.tts, s, pause), getattr(self.tts, "rate", None) or 24000))
                        elif cloud:
                            ready.put(pool.submit(make, s))
                        else:
                            ready.put(make(s))
            except Exception as exc:
                self.log(f"voice error: {exc}")
            ready.put(None)

        threading.Thread(target=producer, daemon=True).start()
        # One output stream for the whole reply. Opening a new one per sentence let the USB speaker (and
        # PipeWire, which suspends idle outputs) wake up late and swallow the first word of each sentence.
        stream, stream_rate = None, None
        try:
            while True:
                item = ready.get()
                if item is None or done():
                    break
                if hasattr(item, "result"):            # a sentence still being made
                    item = item.result()
                    if done():
                        break
                sentence, audio, rate = item
                if on_sentence:
                    on_sentence(sentence)
                streamed = isinstance(audio, _Streamed)
                if audio is None or (not streamed and len(audio) == 0):
                    self._mime(sentence, on_level, should_stop)
                    continue
                out_rate, channels = self._format(rate)
                if out_rate != rate and not streamed:
                    audio = _resample(audio, rate, out_rate)
                rate = out_rate
                if stream is None or rate != stream_rate:
                    if stream is not None:
                        self._close(stream)
                    stream, stream_rate = self._open(rate, channels), rate
                    # lead-in silence so the speaker is awake before the first word
                    stream.write(np.zeros((int(rate * self.lead_in), channels), np.int16))
                if streamed:
                    try:
                        self._play_stream(stream, audio.chunks(), rate, audio.pause, on_level, should_stop)
                    except Exception as exc:
                        self.log(f"voice error: {exc}")
                else:
                    self._play(stream, audio, rate, on_level, should_stop)
        finally:
            if stream is not None:
                self._close(stream, cut=should_stop())

    def _format(self, rate):
        """A sample rate and channel count the output device accepts. A speaker picked by name is opened
        directly (no PipeWire in between), and USB speakers often take only 48 kHz stereo."""
        if rate not in self._formats:
            found = (rate, 1)
            try:
                default = int(sd.query_devices(self.device, "output")["default_samplerate"])
            except Exception:
                default = 48000
            # The XVF3800 mic array plays only 16 kHz stereo.
            for r in dict.fromkeys((rate, default, 48000, 44100, 16000)):
                ok = None
                for ch in (1, 2):
                    try:
                        sd.check_output_settings(device=self.device, samplerate=r, channels=ch, dtype="int16")
                        ok = (r, ch)
                        break
                    except Exception:
                        pass
                if ok:
                    found = ok
                    break
            self._formats[rate] = found
            if found != (rate, 1):
                self.log(f"speaker: playing at {found[0]} Hz, {found[1]} channel(s)")
        return self._formats[rate]

    def play_clip(self, audio, rate, on_level=lambda level: None, should_stop=lambda: False):
        """Play ready-made audio (a song for the game) through the same speaker."""
        with Speaker._output_lock:
            self._start_mouth(on_level)
            try:
                self._play_clip(audio, rate, on_level, should_stop)
            finally:
                self._stop_mouth(cut=should_stop())

    def _play_clip(self, audio, rate, on_level, should_stop):
        if not sd or audio is None or not len(audio):
            return
        out_rate, channels = self._format(rate)
        if out_rate != rate:
            audio, rate = _resample(audio, rate, out_rate), out_rate
        stream = self._open(rate, channels)
        try:
            stream.write(np.zeros((int(rate * self.lead_in), channels), np.int16))
            self._play(stream, audio, rate, on_level, should_stop)
        finally:
            self._close(stream, cut=should_stop())

    def _open(self, rate, channels=1):
        self.channels = channels
        self._next_heard = None   # when the next block written will be heard
        stream = sd.OutputStream(samplerate=rate, channels=channels, dtype="int16", device=self.device,
                                 blocksize=max(1, int(rate * self.BLOCK_S)), latency=self.latency)
        stream.start()
        return stream

    def _close(self, stream, cut=False):
        try:
            if cut:                           # interrupted: go quiet now, drop what is still buffered
                stream.abort()
            else:
                time.sleep(stream.latency or 0)   # let the buffered tail play out
                stream.stop()
            stream.close()
            self.mic.heard_level = 0.0
        except Exception:
            pass

    def _play(self, stream, audio, rate, on_level, should_stop):
        block = max(1, int(rate * self.BLOCK_S))
        n_blocks = int(np.ceil(len(audio) / block))
        padded = np.zeros(n_blocks * block, np.int16)
        padded[:len(audio)] = audio
        blocks = padded.reshape(n_blocks, block)
        rms = np.sqrt(np.mean(blocks.astype(np.float32) ** 2, axis=1))
        ref = max(np.percentile(rms, 90), 1.0)
        levels = np.clip(rms / ref, 0, 1) ** 0.6
        self._write(stream, zip(blocks, levels, rms), on_level, should_stop)

    def _play_stream(self, stream, chunks, rate, pause, on_level, should_stop):
        """Play a sentence while it is still arriving: (int16 chunk, sample rate) pairs."""
        block = max(1, int(rate * self.BLOCK_S))
        resampler, buf, started, ref = None, np.zeros(0, np.int16), False, 1500.0
        keep = int(rate * 0.04)

        def blocks():
            nonlocal resampler, buf, started, ref
            for chunk, in_rate in chunks:
                if should_stop():
                    return
                if resampler is None:
                    resampler = _StreamResampler(in_rate, rate)
                piece = resampler.feed(chunk)
                buf = np.concatenate([buf, piece])
                if not started:                       # skip the silence the voice starts with
                    loud = np.flatnonzero(np.abs(buf.astype(np.int32)) > 300)
                    if not len(loud):
                        buf = buf[-keep:]
                        continue
                    buf, started = buf[max(0, loud[0] - keep):], True
                while len(buf) >= block:
                    out, buf = buf[:block], buf[block:]
                    r = float(np.sqrt(np.mean(out.astype(np.float32) ** 2)))
                    ref = max(ref * 0.999, r * 0.9)   # loudness so far stands in for the whole sentence's
                    yield out, min(1.0, r / ref) ** 0.6, r
            if resampler is not None:
                buf = np.concatenate([buf, resampler.feed(np.zeros(0, np.int16), last=True)])
            if started and len(buf):          # what's left (the resampler's tail can be more than a block)
                buf = np.pad(buf, (0, -len(buf) % block))
                for i in range(0, len(buf), block):
                    yield buf[i:i + block], 0.0, 0.0
            for _ in range(int(pause / self.BLOCK_S)):
                yield np.zeros(block, np.int16), 0.0, 0.0

        self._write(stream, blocks(), on_level, should_stop)

    def _write(self, stream, items, on_level, should_stop):
        """Write (block, mouth level, loudness) items. Each block's level goes on a timeline at the moment the
        block will actually be heard; a separate clock (_mouth_loop) moves the mouth from that timeline. Timing
        the mouth on when writes return made it jerky: the driver releases writes in bursts."""
        latency = max(0.0, (stream.latency or 0) + self.mouth_delay)
        for out, level, rms in items:
            if should_stop():
                break
            if Speaker.paused:                               # hold right here, mid-word if need be
                while Speaker.paused and not should_stop():
                    time.sleep(0.05)
                self._next_heard = None
                if should_stop():
                    break
            now = time.time()
            if self._next_heard is None or self._next_heard < now:      # start, or after a gap/underrun
                self._next_heard = now + latency
            if self.first_sound_at is None:
                self.first_sound_at = self._next_heard
            if not self.mic.through_speech:                  # don't listen to ourselves
                self.mic.mute_for(self._next_heard - now + self.BLOCK_S + 0.6)
            if Speaker.volume != 1.0:
                out = np.clip(out.astype(np.float32) * Speaker.volume, -32768, 32767).astype(np.int16)
            self._timeline.append((self._next_heard, float(level), float(rms) * Speaker.volume))
            self._next_heard += len(out) / stream.samplerate
            stream.write(out if self.channels == 1 else np.repeat(out[:, None], self.channels, axis=1))

    def _start_mouth(self, on_level):
        """Move the mouth (and tell barge-in what is being heard) by the clock until _stop_mouth."""
        self._timeline, self._next_heard = [], None
        self._mouth_on = True

        def loop():
            lead = self.mouth_lead
            while self._mouth_on or (self._timeline and self._timeline[-1][0] > time.time()):
                now = time.time()
                level = heard = None
                tl = self._timeline
                i = 0
                while i < len(tl) and tl[i][0] <= now + lead:     # the mouth runs a little early: the face
                    level = tl[i][1]                               # page and projector add their own delay
                    i += 1
                j = 0
                while j < len(tl) and tl[j][0] <= now:
                    heard = tl[j][2]
                    j += 1
                if j > 1:
                    del tl[:j - 1]
                if level is not None:
                    on_level(level)
                self.mic.heard_level = heard if heard is not None else 0.0
                time.sleep(0.01)
            on_level(0.0)
            self.mic.heard_level = 0.0

        self._mouth_thread = threading.Thread(target=loop, daemon=True, name="mouth")
        self._mouth_thread.start()

    def _stop_mouth(self, cut=False):
        if cut:
            self._timeline = []
        self._mouth_on = False
        if self._mouth_thread:
            self._mouth_thread.join(timeout=2)
            self._mouth_thread = None

    def _mime(self, sentence, on_level, should_stop):
        """No audio available: move the mouth at a speaking pace so the face still 'talks'."""
        end = time.time() + 0.33 * len(sentence.split()) + 0.2
        while time.time() < end and not should_stop():
            t = time.time()
            on_level(abs(np.sin(t * 10.5)) * (0.6 + 0.4 * np.sin(t * 3.1)))
            time.sleep(self.BLOCK_S)
        on_level(0.0)
