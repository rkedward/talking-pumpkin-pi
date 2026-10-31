#!/usr/bin/env python3
"""Talking pumpkin: the brain. Watches for trick-or-treaters, greets them with Claude, listens and answers,
moves the eyes, mouth and props. The face itself is drawn by static/index.html in a (kiosk) browser.

Run:  python3 main.py      then open http://localhost:8000 (the Pi installer does both at boot)
"""

import collections
import contextlib
import functools
import os
import subprocess
import json
import math
import queue
import random
import re
import socket
import sys
import threading
import time
import wave

import numpy as np

import brain
import tunes
import config
from audio import Listener, Microphone, OpenAISTT, Speaker, WhisperSTT, make_tts
from align import Aligner
from doa import ReSpeakerDOA
from home import Home
from props import Props
from sideshow import SideShow
from vision import Vision
from web import Hub, serve

FILLERS = [
    "Ooh! Who goes there?", "Well, well, well, what have we here?", "Hey! Come closer, let me see you!",
    "Ooh, visitors! Let me get a good look at you.", "Halloo there! Hold still, let me see that costume.",
]
WRAPUPS = [
    "I'd love to chat all night, but more trick-or-treaters are coming. Grab some candy and have a spook-tacular Halloween! Ho ho ho!",
    "Okay, my candle's flickering! Take some candy, and happy Halloween! Hee hee!",
]
SILLY_FACES = {"silly", "tongue", "crosseyed", "wink", "dizzy", "scared"}
FLASH_BEAT_S = 1.2   # a silly face shows this long before he starts talking (the face page flashes it 1.5 s)
NUDGES = ["Hello? Are you still there?", "Helloooo? Anybody still there?", "Did you sneak off with my candy? Are you still there?"]
GOODBYES = ["Bye bye! Happy Halloween! Ho ho ho!", "See you later, alligator! Happy haunting!", "Bye bye! Hee hee, that was fun!"]
BUTLER_LINES = [
    "Ding dong! Visitors, sir! Allow me to get the lights.",
    "Someone's at the door! I'll put the lights on and wake up the master pumpkin.",
    "Ah, guests! One moment while I fetch the lights. Master Jack, you have visitors!",
]
STATE_FILE = config.ROOT / "state.json"
NO_CALIB = {"x": 0.0, "y": 0.0, "scale": 1.0, "rot": 0.0, "mirror": False}


def lan_ip():
    """This Pi's address on the home network (what the window projectors open)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "this-pi's-address"


GOTCHAS = ["Hee hee hee! Gotcha! Don't worry, I'm just a big softie. Happy Halloween!",
           "Gotcha! Ha! Sorry, I couldn't resist. Grab some candy, brave one!",
           "Boo! Hee hee, did I get you? I'm a friendly pumpkin, I promise. Happy Halloween!"]


def _roar(rate=16000, seconds=1.4):
    """A monster roar: a growling sawtooth sliding down, a breathy noise rush and a little grit; loud but capped."""
    rng = np.random.default_rng()
    t = np.arange(int(rate * seconds)) / rate
    f0 = 165 * np.exp(-t * 0.9) * (1 + 0.06 * np.sin(2 * np.pi * 23 * t))   # growl wobble
    phase = 2 * np.cumsum(f0) / rate
    saw = (phase % 2) - 1
    sub = np.sin(np.pi * phase / 2)
    noise = rng.standard_normal(len(t))
    kernel = np.hanning(9)
    noise = np.convolve(noise, kernel / kernel.sum(), "same")
    voice = np.tanh(2.8 * (0.7 * saw + 0.5 * sub + 0.45 * noise * (0.6 + 0.4 * np.abs(saw))))
    env = np.minimum(1, t / 0.03) * np.exp(-np.maximum(0, t - 0.25) * 2.2)
    out = voice * env
    out = out / (np.abs(out).max() + 1e-9) * 0.8          # loud, but 80% of full scale at the normal volume
    return (out * 32767).astype(np.int16), rate


class Pumpkin:
    def __init__(self, cfg):
        self.cfg = cfg
        self.hub = Hub()
        self.state = "starting"
        self.awake = False
        self.speaking = False
        self.mouth = 0.0
        self.abort_visit = False
        self.force_greet = False
        self.typed = queue.Queue()
        self._scaring = False
        self._flash_at = 0.0
        self.visit_active = False
        self.history = collections.deque(maxlen=400)   # recent log lines, for the remote control (mcp_server.py)
        self.visit_session = ""
        self._scaring = False
        saved = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        self.gaze_flip = saved.get("gaze_flip", cfg["face"]["gaze_flip"])
        self.calib = saved.get("calib")
        self.face_warp = saved.get("face_warp")   # auto-alignment of the face projector (align.py)
        self.screens = config.screens(cfg["window"])
        wc = saved.get("window_calib") or {}
        if "x" in wc:                       # older single-window state.json
            wc = {next(iter(self.screens)): wc}
        self.window_calib = {n: wc.get(n) or dict(NO_CALIB) for n in self.screens}
        self.guide = False
        self.doorbell_pending = False
        self.hide_idle = bool(cfg["face"]["hide_when_idle"])
        self.look_window_until = 0.0
        self.look_gaze = [0.0, 0.0]
        self.summons = 0
        self.test_index = 0

        if self.calib:
            self.hub.publish({"t": "calib", "calib": self.calib})
        self.hub.publish({"t": "face_warp", "warp": self.face_warp})
        self.aligner = Aligner(self._get_warp, self._set_warp, self.hub, self.log)
        for n, c in self.window_calib.items():
            self.hub.publish({"t": "window_calib", "screen": n, "calib": c})
        port = cfg["web"]["port"]
        wifi_windows = [n for n, sc in self.screens.items() if not sc["hdmi"]] if cfg["window"]["enabled"] else []
        if (wifi_windows or cfg["home"]["enabled"]) and cfg["web"]["host"] in ("127.0.0.1", "localhost"):
            cfg["web"]["host"] = "0.0.0.0"   # the Wi-Fi projectors / smart home have to reach this Pi
        self.home = Home(cfg["home"], self.log)
        self.hub.publish({"t": "show", "on": not self.hide_idle})
        serve(cfg["web"], self.hub, self)
        self.log(f"face page on http://localhost:{port}")
        for n in wifi_windows:
            self.log(f"window '{n}': open http://{lan_ip()}:{port}/window.html?w={n} on its projector")
        self.log("brain: fake mode (canned replies)" if cfg["claude"]["fake"]
                 else f"brain: {cfg['claude']['provider']} {cfg['claude']['model']}")
        self.show = None   # set below; the web panel can ask for status while the rest is still loading
        self.props = Props(cfg["props"], self.log)
        self.vision = Vision(cfg["camera"], cfg["detector"], self.log)
        self.doa = ReSpeakerDOA(cfg["doa"], self.log) if cfg["doa"]["enabled"] else None
        self.mic = Microphone(cfg["mic"], self.log)
        stt, engine = None, cfg["stt"]["engine"]
        if engine in ("whisper", "openai"):
            try:
                stt = WhisperSTT(cfg["stt"], self.log)
            except Exception as exc:
                self.log(f"whisper off ({type(exc).__name__}: {exc})")
        if engine == "openai":
            try:
                stt = OpenAISTT(cfg["stt"], self.log, backup=stt)
            except Exception as exc:
                self.log(f"OpenAI speech recognition off ({exc}); using whisper")
        if stt is None:
            self.log("speech recognition off; type questions in the debug panel")
        self.listener = Listener(self.mic, cfg["mic"], stt)
        self.listener.log = self.log
        # Sound-triggered visits: wake words are checked by the Pi's own whisper, so nothing leaves the porch
        # before a visit starts.
        self.local_stt = stt if getattr(stt, "local", False) else getattr(stt, "backup", None) or stt
        self.sound = str(cfg["visit"]["trigger"]).lower() == "sound"
        self.last_activity = time.time()
        self._voice_seen = (None, 0.0)   # last direction a voice came from, and when
        if self.sound:
            self.vision.set_rates(0, 0)   # no tracking; the camera only looks when asked
            self.log("visits start by sound: hello / trick or treat"
                     + (", a knock" if cfg["visit"]["knock"] else "") + ", the doorbell or Greet; camera on demand"
                     + ("" if getattr(self.local_stt, "local", False) else
                        " (no local whisper: wake words go to the cloud speech-to-text)"))
        if cfg["stt"].get("pause_camera", True):   # give speech recognition the whole CPU
            self.listener.on_busy = self.vision.pause
        self.speaker = Speaker(make_tts(cfg["tts"], self.log), cfg["tts"], self.mic, self.log)
        self.barge_in = self._barge_in_setup(cfg["mic"])
        self._echo_lock = threading.Lock()
        Speaker.volume = float(saved.get("volume", 1.0))
        self.listener.wake_bar = saved.get("wake_bar") or None      # from the hearing test or the slider
        self.listener.wake_flag = bool(saved.get("wake_flag", True))
        self.show = None
        if cfg["window"]["enabled"]:
            self.show = SideShow(cfg["window"], cfg["tts"], self.mic, self.hub, self.log)
            brain.configure(cfg["claude"], self.show.creatures,
                            {n: sc["where"] for n, sc in self.screens.items()}, self.show.online)
        else:
            brain.configure(cfg["claude"])
        brain.set_look(self.sound)
        self._idle_setup(cfg["idle"])
        threading.Thread(target=self._face_loop, daemon=True, name="face").start()

    # ---- helpers -------------------------------------------------------------------------------
    def log(self, text, cls="info"):
        print(f"[{cls}] {text}", flush=True)
        self.history.append({"time": time.strftime("%H:%M:%S"), "kind": cls, "text": text})
        self.hub.publish({"t": "log", "cls": cls, "text": text})

    def set_state(self, s):
        self.state = s
        self.hub.publish({"t": "state", "state": s})

    def _barge_in_setup(self, mc):
        """Whether kids can talk over the pumpkin (see [mic] barge_in)."""
        mode = str(mc["barge_in"]).lower()
        if mode in ("false", "off", "no", "0") or not self.mic.ok or self.listener.stt is None:
            return False
        aec = self.speaker.aec
        if aec and str(self.cfg["mic"]["channel"]) == "auto" and "xvf3800" in self.mic.name.lower() \
                and self.mic.channel != 0:
            # Measured on the XVF3800 USB firmware: channel 0 (conference) has the pumpkin's voice cancelled,
            # channel 1 (speech recognition) still carries it at full volume. Listen on 0 when he plays
            # through the array.
            self.mic.channel = 0
            self.log("mic: listening on channel 0, which has the echo cancelled")
        if mode == "auto" and not aec:
            if self.mic.is_array:
                self.log("barge-in off: plug the speaker into the mic array and set [tts] output_device = "
                         "\"XVF3800\" so it can cancel the pumpkin's own voice (or set [mic] barge_in = true to try anyway)")
            return False
        vad = str(mc["barge_vad"]).lower()
        if (vad == "true" or (vad == "auto" and aec)) and self.doa and self.doa.available:
            self.listener.voice_hint = lambda: time.time() - self.doa.voice_at < 0.5
        self.log("barge-in on: talk over the pumpkin to interrupt him" +
                 (" (mic array cancels his voice)" if aec else " (no echo cancellation: talk up!)"))
        return True

    def _log_timing(self, timing, brain_s, heard_at):
        """One line per reply: where the time went between them stopping talking and him answering."""
        first = self.speaker.first_sound_at or time.time()
        voice = first - heard_at - brain_s
        if timing:
            total = first - timing["stopped"]
            self.log(f"⏱ {total:.1f}s from you stopping to him talking: end-of-speech wait {timing['wait']:.1f}, "
                     f"speech-to-text {timing['stt']:.1f}, brain {brain_s:.1f}, voice {voice:.1f}")
        else:
            self.log(f"⏱ brain {brain_s:.1f}s, voice {voice:.1f}s")

    def say(self, text, interruptible=False, on_first_sound=None):
        """Speak. With interruptible and barge-in on, stops as soon as someone talks over him and returns
        the part he got out (their words go to the next listen()); otherwise returns None."""
        if not text:
            return None
        beat = FLASH_BEAT_S - (time.time() - self._flash_at)
        if 0 < beat <= FLASH_BEAT_S:      # a silly face just flashed: hold it a beat before talking
            time.sleep(beat)
        self._flash_at = 0.0
        if isinstance(text, str):         # (a streamed reply is logged as each sentence starts)
            self.log(f"🎃 {text}", "said")
        before = self.state
        self.set_state("speaking")
        self.speaking = True
        watch = interruptible and self.barge_in
        spoken = []

        streamed = not isinstance(text, str)

        def on_sentence(sentence):
            spoken.append(sentence)
            if streamed:                  # a streamed reply is logged sentence by sentence as he says it
                self.log(f"🎃 {sentence}", "said")
            self._on_sentence(sentence)

        if watch:
            self.listener.start_watch()
        self.speaker.first_sound_at = None
        if on_first_sound:   # once the first word has been heard
            def wait_first():
                while self.speaking and self.speaker.first_sound_at is None:
                    time.sleep(0.02)
                if self.speaker.first_sound_at:
                    time.sleep(max(0.0, self.speaker.first_sound_at - time.time()))
                on_first_sound()
            threading.Thread(target=wait_first, daemon=True).start()
        try:
            # finish_sentence: someone talking over him stops him at the end of the sentence he's on
            cut = lambda: watch and self.listener.interrupted.is_set()
            soft = str(self.cfg["mic"]["barge_in_finish"]).lower() != "now"
            self.speaker.speak(text, on_level=self._set_mouth, on_sentence=on_sentence,
                               should_stop=lambda: self.abort_visit or (cut() and not soft),
                               stop_after=lambda: soft and cut())
        finally:
            if watch:
                self.listener.stop_watch()
        self.speaking = False
        self.mouth = 0.0
        self.props.expression("happy" if self.awake else "neutral")
        if before in ("idle", "cooldown"):
            self.set_state(before)
        if watch and self.listener.interrupted.is_set() and not self.abort_visit:
            self.log("✋ interrupted")
            return " ".join(spoken)
        return None

    def play_song(self, song_id):
        """The guessing game's tune; the mouth bobs along as if the pumpkin is humming it."""
        try:
            audio, rate = tunes.load(song_id)
        except Exception as exc:
            self.log(f"song {song_id}: {exc}")
            return
        self.log(f"🎵 {tunes.catalog()[song_id]['title']}", "said")
        self.speaking = True
        if self.barge_in:   # shouting out the answer mid-song stops the song
            self.listener.start_watch()
        try:
            self.speaker.play_clip(audio, rate, on_level=lambda level: self._set_mouth(level * 0.5),
                                   should_stop=lambda: self.abort_visit or self.listener.interrupted.is_set())
        finally:
            self.listener.stop_watch()
        self.speaking = False
        self.mouth = 0.0
        if self.barge_in and self.listener.interrupted.is_set():
            self.log("✋ interrupted the song")

    def mood(self, name):
        """The feeling of a whole reply, chosen by the brain; the projected face shows it."""
        name = name or "happy"
        if name != "neutral":
            self.log(f"🎭 {name}", "mood")
        if name in SILLY_FACES:   # the face flashes for a beat; say() waits for it before talking
            self._flash_at = time.time()
        self.hub.publish({"t": "mood", "name": name})

    def _on_sentence(self, sentence):
        expr = self.props.expression_for(sentence)
        self.props.expression(expr)
        accent = {"quizzical": "curious", "surprised": "excited", "sly": "sly"}.get(expr)
        if accent:   # this sentence's own look, on top of the reply's mood
            self.hub.publish({"t": "expr", "name": accent, "ms": int(300 + 330 * len(sentence.split()))})
        if expr == "laughing":   # the face squints and bounces while he laughs at his own joke
            self.hub.publish({"t": "laugh", "ms": int(400 + 280 * len(sentence.split()))})

    def sideshow(self, result):
        """Act on Claude's side-show choices: summon a creature, let it talk."""
        if not self.show:
            return
        name = result.get("summon")
        if name and self.summons < int(self.cfg["window"]["max_summons"]) and self.show.summon(name, result.get("window", "")):
            self.summons += 1
            self.look_gaze = self.screens[self.show.screen]["gaze"]
            self.look_window_until = time.time() + 3
            self.home.send("summon", creature=name, window=self.show.screen)
            self.props.expression("sly")
            time.sleep(1.2)                       # let the creature appear before it speaks
        if result.get("creature_says") and self.show.active():
            self.look_window_until = time.time() + 2
            self.show.speak(result["creature_says"], should_stop=lambda: self.abort_visit)
        if result.get("fly_to") and self.show.active() and not self.abort_visit:
            self.show.fly_to(result["fly_to"], on_leave=self._follow)

    def _follow(self, screen):
        """The pumpkin's eyes follow a creature flying to another window."""
        self.look_gaze = self.screens[screen]["gaze"]
        self.look_window_until = time.time() + 1.1 + self.show.fly_gap + 2.5

    def _set_mouth(self, level):
        self.mouth = level

    def present(self):
        _, _, _, last = self.vision.state()
        return last and time.time() - last < float(self.cfg["visit"]["gone_s"])

    def arrived(self):
        people, _, first, _ = self.vision.state()
        return bool(people) and time.time() - first >= float(self.cfg["visit"]["arrive_s"])

    # ---- eyes ----------------------------------------------------------------------------------
    def _face_loop(self):
        hfov = self.vision.hfov
        while True:
            people, target, _, _ = self.vision.state()
            if self.show:
                self.show.active()   # sends the creature away once its time is up
            gaze, look_at = None, None
            voice = self.doa.bearing() if self.doa and self.doa.available and not self.speaking else None
            if self.sound:   # no camera tracking: look where the last voice came from, for a while
                if voice is not None:
                    self._voice_seen = (voice, time.time())
                elif self._voice_seen[0] is not None and time.time() - self._voice_seen[1] < 8:
                    voice = self._voice_seen[0]
                people, target = [], None
            if voice is not None:
                near = min(people, key=lambda p: abs(self.vision.bearing(p) - voice), default=None)
                if near and abs(self.vision.bearing(near) - voice) <= float(self.cfg["doa"]["match_deg"]):
                    self.vision.prefer_bearing(voice)
                    look_at = near
                else:
                    gaze = (voice, 0.1)   # a voice from someone the camera can't see: look that way
            elif target:
                look_at = target
            if look_at:
                gaze = (self.vision.bearing(look_at), ((look_at["y"] + look_at["h"] * 0.12) - 0.35) * 2.2)
            ev = {"t": "face", "awake": self.awake, "speaking": self.speaking, "mouth": round(self.mouth, 3)}
            if time.time() < self.look_window_until:
                ev["gaze"] = list(self.look_gaze)   # glance at the creature's window
            elif gaze:
                # The camera faces the same way as the pumpkin: someone on the image's right is on the
                # pumpkin's right, which is the left side of the projected face. Hence the minus sign.
                gx = -gaze[0] / (hfov / 2) * 1.3 * (-1 if self.gaze_flip else 1)
                ev["gaze"] = [round(max(-1, min(1, gx)), 3), round(max(-1, min(1, gaze[1])), 3)]
            self.hub.publish(ev)
            # while the idle video shows, the face isn't drawn: fewer updates leave the CPU to the video
            time.sleep(0.25 if self._idle_on and not self.visit_active else 1 / 30)

    # ---- visits --------------------------------------------------------------------------------
    def reveal(self, on):
        """Light the pumpkin up (or let it go dark) when hide_when_idle is set."""
        if self.hide_idle:
            self.hub.publish({"t": "show", "on": on})

    def butler_intro(self):
        """The doorbell rang: the butler appears, the smart home turns on a light, then the pumpkin lights up."""
        h = self.cfg["home"]
        self.home.send("doorbell", butler=h["butler"])
        if self.show and self.show.find(h["butler"]) and self.show.summon(h["butler"], h["butler_window"]):
            self.summons += 1
            self.look_gaze = self.screens[self.show.screen]["gaze"]
            self.look_window_until = time.time() + 4
            time.sleep(1.2)
            self.show.speak(random.choice(BUTLER_LINES), should_stop=lambda: self.abort_visit)
        time.sleep(float(h["reveal_delay_s"]))

    def visit(self, forced, trigger=None, said=None):
        self.visit_active = True
        try:
            self._visit(forced, trigger, said)
        finally:
            self.visit_active = False
            self.force_greet = False

    def _visit(self, forced, trigger=None, said=None):
        self.idle_show(False)   # the singing pumpkins make way for the live face
        v = self.cfg["visit"]
        self.abort_visit = False
        self.summons = 0
        self.listener.reset()
        self.vision.conversation(True)
        doorbell, self.doorbell_pending = self.doorbell_pending, False
        if doorbell:
            self.set_state("doorbell")
            self.butler_intro()
        self.reveal(True)
        self.home.send("visit_start", doorbell=doorbell)
        self.awake = True
        self.hub.publish({"t": "surprise"})
        self.props.expression("surprised")
        self.props.arrival()
        self.set_state("greeting")
        result = {}

        def ask_claude():
            try:
                if self.sound:   # no tracking: one fresh look now
                    people = self.vision.look_now()
                else:
                    people, _, _, _ = self.vision.state()
                on_stage = self.show.active() if self.show else None
                note = (f"They rang the doorbell, and your butler {on_stage['name']} answered it and is still visible."
                        if doorbell and on_stage else "They rang the doorbell." if doorbell else "")
                if self.sound:
                    if trigger == "knock":
                        note += " They knocked on the door."
                    elif said:
                        note += f' You heard them say: "{said}". Answer that too.'
                    if not people:
                        note += (" Nobody is in view of your eyes yet (no picture): greet them warmly, ask them to"
                                 " step right in front of you so you can see their costumes, and set look to true."
                                 " Don't describe darkness or blur; you just can't see them yet.")
                image = self.vision.snapshot_b64() if people or not self.sound else ""
                result.update(brain.greet(image, len(people) if self.sound else max(1, len(people)), note))
            except Exception as exc:
                self.log(f"claude error: {type(exc).__name__}: {exc}")
                result.update({"session": "", "costumes": [], "say": brain.FALLBACK_SAY})

        worker = threading.Thread(target=ask_claude, daemon=True)
        worker.start()
        if hasattr(self.listener.stt, "warm_up"):   # connect to the cloud before they first speak
            threading.Thread(target=self.listener.stt.warm_up, daemon=True).start()
        self.say(random.choice(FILLERS))      # covers the time Claude takes to look
        worker.join()
        if result.get("costumes"):
            self.log(f"costumes: {'; '.join(result['costumes'])}")
        session = self.visit_session = result.get("session", "")
        self.mood(result.get("mood"))
        cut_off = self.say(result.get("say"), interruptible=True)
        if cut_off is None:
            self.sideshow(result)

        started, turns = time.time(), 0
        self._pause_debt = 0.0
        brain_thread = None
        max_turns, max_s, game_on = int(v["max_turns"]), float(v["max_visit_s"]), False
        pending_look, looks, nudged = self.sound and result.get("look"), 0, False
        self.last_activity = time.time()
        while not self.abort_visit:
            if self.sound:   # nobody to watch walk away: a quiet spell ends the visit (after one nudge)
                quiet = time.time() - self.last_activity
                if quiet >= float(v["quiet_end_s"]) and not pending_look:
                    if nudged:
                        self.say(random.choice(GOODBYES))
                        break
                    nudged = True
                    self.say(random.choice(NUDGES), interruptible=True)
                    self.last_activity = time.time() - float(v["quiet_end_s"]) / 2   # half as long to answer
                    continue
            elif not forced and not self.present():
                self.say(random.choice(GOODBYES))
                break
            started, self._pause_debt = started + self._pause_debt, 0.0   # time spent paused doesn't count
            if turns >= max_turns or time.time() - started > max_s:
                self.say(random.choice(WRAPUPS))
                break
            image, seen = None, 0
            if pending_look and looks < 1:   # he asked them to step into view: take one fresh look
                pending_look, looks = False, looks + 1
                self.set_state("thinking")
                seen, image = self.look_for_people()
                image = image if seen else None   # a picture of nobody only gets him describing the dark
                heard, timing = None, {}
                self.log(f"👀 looked: {seen} in view", "info")
            else:
                pending_look = False
                self.set_state("listening")
                wait = float(v["listen_s"])
                if self.sound:
                    wait = max(1.0, min(wait, float(v["quiet_end_s"]) - (time.time() - self.last_activity)))
                heard = self.listener.listen(wait, self.typed, lambda: self.abort_visit,
                                             on_speech=self._heard_voice)
                if self.abort_visit:
                    break
                if not heard:
                    cut_off = None   # whoever talked over him didn't say anything after all
                    if forced and not self.sound and not self.present():
                        forced = False
                    continue
                nudged = False
                self.log(f"👂 {heard}", "heard")
                timing, self.listener.timing = self.listener.timing, {}
                if self._bye_re.search(heard):   # they said goodbye: wave them off right away
                    self.say(random.choice(GOODBYES))
                    break
            heard_at = time.time()
            self.set_state("thinking")
            on_stage = self.show.active() if self.show else None
            if brain_thread:                      # an interrupted reply still finishing (it updates the history)
                brain_thread.join(timeout=10)
            # The reply streams in: he starts saying its first sentence while Claude is still writing the rest.
            sentences, reply = queue.Queue(), {}

            last = turns + 1 >= max_turns or time.time() - started > max_s - 10

            def think(heard=heard, cut_off=cut_off, on_stage=on_stage, image=image, seen=seen, last=last):
                try:
                    reply.update(brain.chat(session, heard,
                                            f"{on_stage['name']} (in {self.show.screen})" if on_stage else None,
                                            cut_off=cut_off, on_say=sentences.put, on_mood=self.mood,
                                            image_b64=image, people=seen, last=last))
                except Exception as exc:
                    self.log(f"claude error: {type(exc).__name__}: {exc}")
                    reply.update({"say": "Ooh, my pumpkin brain went fuzzy. Happy Halloween!"})
                finally:
                    sentences.put(None)

            brain_thread = threading.Thread(target=think, daemon=True, name="brain")
            brain_thread.start()
            first = sentences.get()
            if first is None:                     # nothing streamed (fake mode, OpenAI brain, an error)
                self.mood(reply.get("mood"))
                text = reply.get("say")
            else:
                def text_stream(first=first):
                    yield first
                    yield from iter(sentences.get, None)
                text = text_stream()
            brain_s = time.time() - heard_at
            cut_off = self.say(text, interruptible=True,
                               on_first_sound=lambda: self._log_timing(timing, brain_s, heard_at))
            self.last_activity = time.time()
            if cut_off is not None:   # they talked over him: skip the rest and hear them out
                turns += 1
                continue
            brain_thread.join(timeout=10)
            pending_look = self.sound and reply.get("look") and not last
            if last and not reply.get("play_song"):   # that reply was his goodbye
                break
            self.sideshow(reply)
            if reply.get("play_song") and not self.abort_visit:
                if not game_on:   # a game takes a few more turns than a chat
                    game_on = True
                    max_turns += int(v["game_extra_turns"])
                    max_s += float(v["game_extra_s"])
                self.play_song(reply["play_song"])
            turns += 1
        self.visit_session = ""
        self.props.visit_over()
        if self.show:
            self.show.dismiss()
        self.home.send("visit_end")
        self.reveal(False)
        self.vision.conversation(False)
        self.awake = False
        self.mood("neutral")
        self.props.expression("neutral")

    def run(self):
        if self.sound:
            return self._run_sound()
        last_visit_end = -1e9
        while True:
            self.awake = False
            self.set_state("idle")
            self.idle_show(True)
            # After a visit, don't re-greet the same kids until they step away (or regreet_s passes,
            # for when the next group steps up as the last one leaves).
            left = False
            while True:
                _, _, _, last = self.vision.state()
                people, _, _, _ = self.vision.state()
                if not people and time.time() - last > 1.5:
                    left = True
                if self.force_greet:
                    break
                ready = left or time.time() - last_visit_end > float(self.cfg["visit"]["regreet_s"])
                if ready and self.arrived():
                    break
                time.sleep(0.1)
            forced, self.force_greet = self.force_greet, False
            self.visit(forced)
            last_visit_end = time.time()
            self.set_state("cooldown")
            time.sleep(2.5)

    # ---- idle video (e.g. AtmosFX singing pumpkins) ----------------------------------------------------
    def _idle_setup(self, ic):
        """[idle] video: one file, or a list of them to play in turn."""
        self._idle_clips = []        # [{"path", "audio": (samples, rate) | None, "lyrics": [(start s, words)]}]
        self._idle_on, self._idle_thread = False, None
        self._idle_playing = False   # a soundtrack is coming out of the speaker right now
        self._idle_clip = None       # the clip playing now
        self._idle_started = 0.0     # when the current clip's soundtrack started sounding
        self._idle_cfg = ic
        videos = ic["video"] if isinstance(ic["video"], list) else [ic["video"]] if ic["video"] else []
        for v in videos:
            path = config.resolve(v)
            if path.is_file():
                self._idle_clips.append({"path": path, "audio": None, "lyrics": []})
            else:
                self.log(f"idle video: {path} not found")
        if not self._idle_clips:
            return
        self.log("idle video: " + ", then ".join(c["path"].name for c in self._idle_clips)
                 + " (the live face takes over when a visit starts)")
        if ic["sound"]:
            threading.Thread(target=self._idle_load_audio, daemon=True, name="idle-audio").start()

    def idle_video_file(self, i):
        """For the web server: the file of clip i."""
        try:
            return str(self._idle_clips[int(i)]["path"])
        except (ValueError, IndexError, TypeError):
            return None

    @property
    def idle_video_path(self):
        return self.idle_video_file(0)

    def _idle_load_audio(self):
        """Each clip's soundtrack, decoded once with ffmpeg (cached next to the video), then its words."""
        rate = 16000
        for clip in self._idle_clips:
            path = clip["path"]
            cache = path.with_suffix(f".{rate}.s16")
            try:
                if not cache.exists() or cache.stat().st_mtime < path.stat().st_mtime:
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", str(rate),
                                    "-f", "s16le", str(cache)], check=True, timeout=300)
                audio = np.fromfile(cache, np.int16)
                vol = float(self._idle_cfg["volume"])
                if vol != 1.0:
                    audio = np.clip(audio.astype(np.float32) * vol, -32768, 32767).astype(np.int16)
                clip["audio"] = (audio, rate)
                # skip the quiet seconds at each end, so one song runs straight into the next
                block = rate // 10
                lv = np.sqrt(np.mean(audio[:len(audio) // block * block].reshape(-1, block).astype(np.float32) ** 2, 1))
                on = np.flatnonzero(lv > max(200.0, lv.max() * 0.03))
                clip["span"] = ((max(0, on[0] - 3) * block, min(len(audio), (on[-1] + 4) * block))
                                if len(on) else (0, len(audio)))
                cut = (len(audio) - (clip["span"][1] - clip["span"][0])) / rate
                self.log(f"idle video: {path.name} soundtrack ready ({len(audio) / rate:.0f} s"
                         + (f", skipping {cut:.0f} s of quiet at the ends)" if cut >= 1 else ")"))
            except Exception as exc:
                self.log(f"idle video: no soundtrack for {path.name} ({type(exc).__name__}: {exc})")
        if self._idle_on and not self._idle_thread:   # it was already showing silently: start the sound
            self._idle_on = False
            self.idle_show(True)
        for clip in self._idle_clips:                 # (slow the first time; the music doesn't wait for it)
            if clip["audio"]:
                self._idle_lyrics(clip)

    LYRIC_CHUNK_S = 10

    def _idle_lyrics(self, clip):
        """Write down the song's words once with the Pi's whisper, 10 s at a time: whatever the song is singing
        right now, leaking through the echo cancelling, mustn't count as a visitor saying hello."""
        path, (audio, rate) = clip["path"], clip["audio"]
        cache = path.with_suffix(".lyrics.json")
        try:
            if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
                clip["lyrics"] = [tuple(x) for x in json.loads(cache.read_text())]
            elif self.local_stt is not None and getattr(self.local_stt, "local", False):
                step, out = rate * self.LYRIC_CHUNK_S, []
                for i in range(0, len(audio), step):
                    text = self.local_stt.transcribe(audio[i:i + step].astype(np.float32) / 32768.0, rate)
                    out.append((i / rate, text.lower()))
                clip["lyrics"] = out
                cache.write_text(json.dumps(out))
            if clip["lyrics"]:
                self.log(f"idle video: {path.name}: its words won't wake him while it sings them "
                         f"({sum(len(t.split()) for _, t in clip['lyrics'])} words)")
        except Exception as exc:
            self.log(f"idle video: couldn't write down the words of {path.name} ({exc})")

    def _lyrics_near(self, before=8.0, after=3.0):
        """The playing song's words around where it is now (what someone just 'said' may be the song)."""
        clip = self._idle_clip
        if not clip or not clip["lyrics"] or not clip["audio"]:
            return ""
        audio, rate = clip["audio"]
        pos = time.time() - self._idle_started
        return " ".join(t for st, t in clip["lyrics"] if st < pos + after and st + self.LYRIC_CHUNK_S > pos - before)

    def idle_show(self, on):
        """Show the idle video (with its soundtrack) or hand over to the live face."""
        if not self._idle_clips or on == self._idle_on:
            return
        self._idle_on = on
        fade = float(self._idle_cfg["fade_s"])
        if not on:
            self.hub.publish({"t": "idle", "on": False, "fade": fade})
            if self._idle_thread:
                self._idle_thread.join(timeout=2)
                self._idle_thread = None
            return
        if not any(c["audio"] for c in self._idle_clips):   # no sound (yet): the page loops the first clip
            self.hub.publish({"t": "idle", "on": True, "fade": fade, "start": None, "clip": 0})
            return
        self._idle_thread = threading.Thread(target=self._idle_loop, daemon=True, name="idle-video")
        self._idle_thread.start()

    def _idle_loop(self):
        fade = float(self._idle_cfg["fade_s"])
        aec = self.speaker.aec
        clips = [i for i, c in enumerate(self._idle_clips) if c["audio"]]
        n = getattr(self, "_idle_next", 0)
        while self._idle_on:
            i = clips[n % len(clips)]
            clip = self._idle_clips[i]
            audio, rate = clip["audio"]
            a, b = clip.get("span", (0, len(audio)))
            # the page starts the (muted) video at the moment the soundtrack is heard (from the same spot)
            start = time.time() + self.speaker.lead_in + (self.speaker.latency or 0.15) + 0.05 - a / rate
            self._idle_clip, self._idle_started = clip, start
            self.hub.publish({"t": "idle", "on": True, "fade": fade, "start": start, "clip": i})
            audio = audio[a:b]
            if aec:
                self.mic.through_speech = True   # keep hearing "trick or treat" over the music
            self._idle_playing = True
            try:
                self.speaker.play_clip(audio, rate, should_stop=lambda: not self._idle_on)
            finally:
                self._idle_playing = False
                if aec:
                    self.mic.through_speech = False
            if self._idle_on:          # finished: the next song (after a visit, the next one too)
                n += 1
        self._idle_next = n + 1

    HEARING_TEST_S = 8

    def hearing_test(self):
        """Record 8 s between visits while someone says "trick or treat" a few times, then set the hearing
        threshold between the loudest non-voice sound (music echo, room) and their voice."""
        if self.visit_active or not self.sound:
            return {"error": "the hearing test runs between visits (sound mode): end the visit first"}
        if self.listener.test:
            return {"error": "already testing"}
        self.log(f"🎤 Hearing test: say \"trick or treat\" 2 or 3 times now, as loud as a kid would "
                 f"({self.HEARING_TEST_S} s)")
        self.listener.test = {"until": time.time() + self.HEARING_TEST_S, "frames": [], "flags": [],
                              "done": self._hearing_result}
        return {"ok": True, "seconds": self.HEARING_TEST_S}

    def _hearing_result(self, frames, flags):
        from audio import speech_spans
        if not frames:
            self.log("🎤 Hearing test: no sound came in from the mic")
            return
        fs, rate = Microphone.FRAME_S, self.mic.rate
        audio = np.concatenate(frames).astype(np.float32) / 32768.0
        spans = speech_spans(audio, rate) or []
        rms = np.array([float(np.sqrt(np.mean(f.astype(np.float32) ** 2))) for f in frames])
        is_voice = np.zeros(len(frames), bool)
        for a, b in spans:
            is_voice[int(a / fs):int(b / fs) + 1] = True
        is_voice = is_voice[:len(frames)]
        if is_voice.sum() < int(0.3 / fs):
            self.log(f"🎤 Hearing test: I didn't hear a voice (loudest sound {rms.max():.0f}). Is the mic muted, "
                     "or were you too far away? Nothing changed.")
            return
        voice_lvl = float(np.percentile(rms[is_voice], 40))            # most of the words are louder than this
        other = rms[~is_voice]
        noise_lvl = float(np.percentile(other, 95)) if len(other) else float(self.listener.noise)
        flag_on = float(np.mean(np.array(flags)[is_voice]))
        try:
            heard = self.listener.stt.transcribe(audio, rate) if self.listener.stt else ""
        except Exception:
            heard = ""
        # between the two, leaning low (easy to wake: Silero and the wake words throw out the rest)
        bar = max(20.0, noise_lvl * 1.2, min(voice_lvl * 0.5, (voice_lvl * noise_lvl) ** 0.5))
        bar = min(bar, voice_lvl * 0.8)                                  # never above their own voice
        old = self.listener.wake_bar or max(self.listener.noise * 1.8, 50.0)
        self.listener.wake_bar = bar
        self.listener.wake_flag = flag_on >= 0.5
        self._save()
        self.log(f"🎤 Hearing test: your voice {voice_lvl:.0f}, other sounds up to {noise_lvl:.0f}, "
                 f"mic array's voice light on {flag_on:.0%} of the time; heard {heard!r}. "
                 f"Threshold {old:.0f} → {bar:.0f}"
                 + ("" if self.listener.wake_flag else " (and not waiting for the voice light, which missed you)")
                 + (". Your voice was barely above the other sounds: move closer or turn the music down."
                    if voice_lvl < noise_lvl * 1.3 else "."))

    def set_paused(self, on):
        """The Pause button: his voice, the songs and the video freeze where they are, nothing wakes him and
        no visit timer runs; Resume picks it all up from the same spot."""
        if on == Speaker.paused:
            return
        if on:
            self._paused_at = time.time()
            Speaker.paused = True
            self.hub.publish({"t": "pause", "on": True})
            self.log("⏸ paused")
            return
        held = time.time() - getattr(self, "_paused_at", time.time())
        self._pause_debt = getattr(self, "_pause_debt", 0.0) + held
        self.last_activity += held
        if self._idle_playing and self._idle_clip:   # the song picks up where it was: so does the picture
            self._idle_started += held
            self.hub.publish({"t": "idle", "on": True, "fade": 0, "start": self._idle_started,
                              "clip": self._idle_clips.index(self._idle_clip)})
        Speaker.paused = False
        self.hub.publish({"t": "pause", "on": False})
        self.log(f"▶ resumed after {held:.0f} s")

    @contextlib.contextmanager
    def idle_paused(self):
        """For remote-control lines and the jump scare between visits: the face comes out for them."""
        was = self._idle_on and not self.visit_active
        if was:
            self.idle_show(False)
            time.sleep(float(self._idle_cfg["fade_s"]))
        try:
            yield
        finally:
            if was and not self.visit_active:
                self.idle_show(True)

    @property
    def _bye_re(self):
        words = [w.lower() for w in self.cfg["visit"]["bye_words"]]
        return re.compile(r"\b(" + "|".join(re.escape(w).replace(r"\ ", r"[\s,.-]*") for w in words) + r")\b", re.I)

    def _run_sound(self):
        """Sound-triggered visits: wait for a "trick or treat" / hello, a knock, the doorbell or the remote."""
        v = self.cfg["visit"]
        words = [w.lower() for w in v["wake_words"]]
        wake_re = re.compile(r"\b(" + "|".join(re.escape(w).replace(r"\ ", r"[\s,.-]*") for w in words) + r")\b")

        wake_any = str(v["wake_on"]).lower() == "any"
        # Who writes down what's said before a visit: the cloud (accurate) or whisper on the Pi (private).
        wake_stt = self.listener.stt if str(v["wake_stt"]).lower() == "cloud" else self.local_stt
        self.log(f"wake-up: {'anyone talking' if wake_any else 'wake words'}"
                 f"{', heard by the mic array' if self._array_voice() else ''}, transcribed "
                 f"{'in the cloud' if not getattr(wake_stt, 'local', False) else 'on the Pi'}")
        lyric_word = re.compile(r"[a-z']+")

        def is_wake(text):
            if wake_any:   # any real words from someone near the pumpkin (unless it's the song leaking through)
                words = lyric_word.findall(text.lower())
                if not words:
                    return False
                if self._idle_playing:
                    near = set(lyric_word.findall(self._lyrics_near()))
                    if near and sum(w in near for w in words) >= max(1, 0.6 * len(words)):
                        self.log(f"(ignored, sounds like the song: {text!r})", "info")
                        return False
                return True
            found = [m.group(0) for m in wake_re.finditer(text.lower())]
            if self._idle_playing and found:   # the song leaking through doesn't count: what it's singing now
                near = self._lyrics_near()
                kept = [w for w in found if w not in near and "knock" not in w]
                if not kept:
                    self.log(f"(ignored, sounds like the song: {text!r})", "info")
                found = kept
            return bool(found)

        while True:
            self.awake = False
            self.set_state("idle")
            self.idle_show(True)
            got = self.listener.wait_for_visitor(lambda: self.force_greet or not self.typed.empty(), is_wake,
                                                 stt=wake_stt, knock=bool(v["knock"]),
                                                 knock_ratio=float(v["knock_ratio"]), knocks=int(v["knocks"]),
                                                 music=lambda: self._idle_playing, voice=self._array_voice(),
                                                 retry_stt=self.local_stt)
            trigger, said = got if got else ("remote", None)
            if trigger == "remote" and not self.force_greet and not self.typed.empty():
                trigger, said = "words", self.typed.get_nowait()
            self.log({"words": f"🔔 heard \"{said}\"", "knock": "🔔 knock knock",
                      "remote": "🔔 greet"}[trigger], "info")
            self.force_greet = False
            self.visit(True, trigger, said)
            self.set_state("cooldown")
            time.sleep(float(v["cooldown_s"]))   # 0 = listen for the next group right away

    def _array_voice(self):
        """The mic array's own voice-activity light, if it has one (it ignores the speaker and typing)."""
        if not (self.doa and self.doa.available) or not self.cfg["visit"].get("use_array_vad", True):
            return None
        return lambda: time.time() - self.doa.voice_at < 0.6

    def _heard_voice(self):
        self.last_activity = time.time()
        self.set_state("hearing")

    def look_for_people(self, wait_s=5.0):
        """A fresh look after he asked them to step into view: up to wait_s for someone to appear. Returns
        (people seen, jpeg base64)."""
        time.sleep(1.0)                      # give them a moment to move
        end = time.time() + wait_s
        people = self.vision.look_now()
        while not people and time.time() < end and not self.abort_visit:
            time.sleep(0.4)
            people = self.vision.look_now()
        return len(people), self.vision.snapshot_b64()

    # ---- web interface -------------------------------------------------------------------------
    def face_demo(self):
        """Show off each expression: the pumpkin names it while the face makes it."""
        was_awake, self.awake = self.awake, True
        try:
            for mood in brain.MOODS + ["listening", "laughing"]:
                if self.abort_visit:
                    break
                if mood in ("listening", "thinking"):
                    self.hub.publish({"t": "state", "state": mood})
                    self.say(mood.capitalize() + ".")
                    self.hub.publish({"t": "state", "state": mood})   # saying it switched to "speaking"
                    time.sleep(2)
                elif mood == "laughing":
                    self.hub.publish({"t": "laugh", "ms": 2500})
                    self.say("Laughing!")
                    time.sleep(1)
                else:
                    self.mood(mood)
                    self.say(mood.capitalize() + "!")
                    time.sleep(1.5)
        finally:
            self.hub.publish({"t": "state", "state": self.state})
            self.mood("neutral")
            self.awake = was_awake

    ECHO_TEST_TEXT = ("Testing, testing. This is the pumpkin checking whether the mic array can hear me. "
                      "If echo cancellation works, my own voice should almost vanish from the microphone. "
                      "One, two, three, four, five. Happy Halloween!")

    def echo_test(self):
        """Debug-panel button: play speech through the speaker while recording the mic array, and report how
        much of the pumpkin's own voice gets through (echo cancellation) and whether barge-in would have
        interrupted him. The recording is saved as static/echotest.wav (one channel per mic channel)."""
        if not self._echo_lock.acquire(blocking=False):
            self.log("echo test: already running")
            return
        try:
            self._echo_test()
        finally:
            self._echo_lock.release()

    def _echo_test(self):
        if self.awake or self.speaking or not self.mic.ok:
            self.log("echo test: " + ("no microphone" if not self.mic.ok else "busy, try again after the visit"))
            return
        fs = 0.03
        rate = self.mic.rate
        self.log(f"🔊 echo test: speaker {'is' if self.speaker.aec else 'is NOT'} the mic array "
                 f"({'echo cancellation can work' if self.speaker.aec else 'no echo cancellation possible'}); "
                 "stay quiet for a moment...")
        self.mic.recording, levels = [], []
        stop = threading.Event()

        def track():    # what the speaker is playing, sampled alongside the mic
            while not stop.is_set():
                levels.append((time.time(), self.mic.heard_level))
                time.sleep(fs / 2)

        threading.Thread(target=track, daemon=True).start()
        started = time.time()
        time.sleep(1.5)                                    # room noise
        barged_at = None
        if self.barge_in:
            self.listener.start_watch()
        self.speaking = True
        play_from = time.time()
        try:
            self.speaker.speak(self.ECHO_TEST_TEXT, on_level=self._set_mouth)
        finally:
            self.speaking = False
            self.mouth = 0.0
        if self.barge_in:
            if self.listener.interrupted.is_set():
                barged_at = "yes"
            self.listener.stop_watch()
            self.listener.reset()
        time.sleep(1.0)
        stop.set()
        rec, self.mic.recording = self.mic.recording, None
        if not rec:
            self.log("echo test: the mic recorded nothing")
            return
        audio = np.concatenate([b for _, b in rec])
        # When the first recorded sample was actually heard: its callback time, minus the block itself and
        # the input buffer.
        in_lat = float(getattr(getattr(self.mic, "stream", None), "latency", 0) or 0)
        ends = np.cumsum([len(b) for _, b in rec])                  # sample count at the end of each block
        cb_t = np.array([t_ for t_, _ in rec]) - in_lat
        clock = lambda samples: np.interp(samples, ends, cb_t)       # sample index -> when it was heard
        t0 = float(clock(0)) if len(rec) < 2 else float(cb_t[0] - ends[0] / rate)

        # Per 30 ms frame: mic loudness on each channel, and whether the speaker was playing then.
        block = int(rate * fs)
        n = len(audio) // block
        frames = audio[:n * block].reshape(n, block, -1).astype(np.float32)
        rms = np.sqrt(np.mean(frames ** 2, axis=1))                  # (frames, channels)
        t = clock(np.arange(n) * block + block / 2)
        lt = np.array([x[0] for x in levels]) if levels else np.zeros(1)
        lv = np.array([x[1] for x in levels]) if levels else np.zeros(1)
        playing = np.interp(t, lt, lv) > 200
        quiet = t < play_from
        db = lambda a, b: 20 * np.log10(max(a, 1.0) / max(b, 1.0))
        lines = []
        for ch in range(rms.shape[1]):
            noise = float(np.median(rms[quiet, ch])) if quiet.any() else 1.0
            echo = float(np.median(rms[playing, ch])) if playing.any() else 0.0
            loud = float(np.percentile(rms[playing, ch], 90)) if playing.any() else 0.0
            label = {0: "conference", 1: "speech recognition"}.get(ch, "") if rms.shape[1] <= 2 else ""
            mark = " (used)" if ch == self.mic.channel else ""
            lines.append(f"ch{ch} {label}{mark}: room {noise:.0f}, while talking {echo:.0f} "
                         f"(peaks {loud:.0f}) = {db(echo, noise):+.0f} dB over the room")
        ch = self.mic.channel
        noise = float(np.median(rms[quiet, ch])) if quiet.any() else 1.0
        per_s = []                                                    # does it improve as the AEC settles?
        first = int((play_from - started) / fs)
        for i in range(first, n, int(1 / fs)):
            sel = playing[i:i + int(1 / fs)]
            if sel.sum() > 5:
                per_s.append(f"{db(float(np.median(rms[i:i + int(1 / fs), ch][sel])), noise):+.0f}")
        for line in lines:
            self.log("🔊 " + line)
        if per_s:
            self.log("🔊 each second while talking, dB over the room: " + " ".join(per_s))
        over = db(float(np.median(rms[playing, ch])) if playing.any() else 0.0, noise)
        verdict = ("echo is well cancelled" if over < 6 else "echo is partly cancelled" if over < 15
                   else "echo is NOT being cancelled")
        if self.barge_in:
            verdict += "; barge-in " + ("WOULD have interrupted itself" if barged_at else "stayed quiet (good)")
        lag = self._mouth_lag(audio, rate, clock, lt, lv)
        if lag is not None:
            self.log(f"🔊 lip sync: the sound reaches the mic {lag * 1000:+.0f} ms after the mouth moves "
                     f"(mouth_delay is {self.speaker.mouth_delay:.2f}; try [tts] mouth_delay = "
                     f"{self.speaker.mouth_delay + lag:.2f}; negative = mouth earlier)")
        self.log(f"🔊 echo test: {verdict}. Recording: /echotest.wav")
        try:
            with wave.open(str(config.ROOT / "static" / "echotest.wav"), "wb") as w:
                w.setnchannels(audio.shape[1] if audio.ndim > 1 else 1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(audio.astype(np.int16).tobytes())
        except Exception as exc:
            self.log(f"echo test: couldn't save the recording ({exc})")

    @staticmethod
    def _mouth_lag(audio, rate, clock, lt, lv):
        """How much later the voice actually sounds than the mouth timeline says, in seconds: the lag that
        best lines up the loudest (least echo-cancelled) mic channel with what was played."""
        if len(lt) < 50:
            return None
        step = 0.01
        block = int(rate * step)
        n = len(audio) // block
        env = np.sqrt(np.mean(audio[:n * block].reshape(n, block, -1).astype(np.float32) ** 2, axis=1))
        env = env[:, int(np.argmax(env.max(axis=0)))]
        t = clock(np.arange(n) * block + block / 2)
        played = np.interp(t, lt, lv, left=0, right=0)
        if played.max() < 200 or env.max() < 50:
            return None
        a = np.log1p(played) - np.log1p(played).mean()
        b = np.log1p(env) - np.log1p(env).mean()
        best, best_lag = -1e9, 0
        for k in range(-20, 61):          # -200 .. +600 ms
            x, y = (a[:len(a) - k], b[k:]) if k >= 0 else (a[-k:], b[:len(b) + k])
            c = float(np.dot(x, y)) / max(len(x), 1)
            if c > best:
                best, best_lag = c, k
        return best_lag * step

    def command(self, name, data):
        if name == "echo_test":
            threading.Thread(target=self.echo_test, daemon=True, name="echo-test").start()
        elif name == "faces":
            threading.Thread(target=self.face_demo, daemon=True).start()
        elif name == "greet":
            if self.visit_active:   # otherwise it waits and starts a second greeting as soon as this visit ends
                self.log("manual greet (already in a visit)")
                return {"ok": True, "note": "already in a visit"}
            self.force_greet = True
            self.log("manual greet")
        elif name == "end":
            self.abort_visit = True
            self.log("visit ended")
        elif name == "flip":
            self.gaze_flip = not self.gaze_flip
            self._save()
            self.log(f"eye direction {'flipped' if self.gaze_flip else 'normal'}")
        elif name == "calib":
            calib = data.get("calib")
            if isinstance(calib, dict):
                self.calib = {k: float(v) for k, v in calib.items() if k in ("x", "y", "scale", "rot", "stretch")}
                self.hub.publish({"t": "calib", "calib": self.calib})
                self._save()
        elif name == "creature_done":
            if self.show:
                self.show.clip_finished(data.get("screen"))
        elif name == "window_seen":
            if not self.show:
                return {"error": "side show is off; set enabled = true under [window]"}
            if not self.show.checked_in(str(data.get("screen", ""))):
                return {"error": f"no window called '{data.get('screen')}' in config.toml",
                        "windows": list(self.screens)}
        elif name == "ghost_tour":
            if not self.show:
                return {"error": "side show is off; set enabled = true under [window]"}
            route = self.show.tour_order()
            ghost = next((c for c in self.show.creatures if c.get("flies")), None)
            if not ghost or len(route) < 2:
                return {"error": "a ghost tour needs the built-in ghost and at least two windows switched on"}
            threading.Thread(target=self._tour, args=(ghost["name"], route), daemon=True).start()
        elif name == "doorbell":
            if not self.home.allowed(data):
                return {"error": "home automation is off, or the key is wrong"}
            if self.state not in ("idle", "cooldown", "starting"):
                self.log("🔔 doorbell (already greeting someone)")
                return {"ok": True, "note": "busy"}
            self.log("🔔 doorbell")
            self.doorbell_pending = True
            self.force_greet = True
        elif name.startswith("align_"):
            return self.align_command(name, data)
        elif name == "window_identify":
            self.hub.publish({"t": "identify"})
        elif name == "summon":
            if not self.show or not self.show.creatures:
                return {"error": "side show is off; set enabled = true under [window]"}
            screen = str(data.get("screen", ""))
            fits = [c for c in self.show.creatures if self.show.place(c, screen) == screen] or self.show.creatures
            c = fits[self.test_index % len(fits)]
            self.test_index += 1
            if not self.show.summon(c["name"], screen):
                return {"error": "no window is switched on (open window.html on a projector)"}
            self.look_gaze = self.screens[self.show.screen]["gaze"]
            self.look_window_until = time.time() + 3
            threading.Thread(target=lambda: (time.sleep(1.2), self.show.speak("Hee hee! Hello there! Happy Halloween!", lambda: False)),
                             daemon=True).start()
        elif name == "window_adjust":
            screen = str(data.get("screen", ""))
            if screen not in self.window_calib:
                return {"error": "unknown window"}
            self.adjust_window(screen, str(data.get("key", "")))
        elif name == "guide":
            self.guide = not self.guide
            self.hub.publish({"t": "guide", "on": self.guide})
        elif name == "volume":
            try:
                Speaker.volume = min(1.5, max(0.0, float(data.get("percent", 100)) / 100))
            except (TypeError, ValueError):
                return {"error": "percent must be a number"}
            self._save()
            self.log(f"volume {round(Speaker.volume * 100)}%")
        elif name == "hearing_test":
            return self.hearing_test()
        elif name == "wake_bar":
            try:
                bar = float(data.get("value", 0))
            except (TypeError, ValueError):
                return {"error": "value must be a number"}
            self.listener.wake_bar = min(2000.0, bar) if bar > 0 else None
            if not bar:
                self.listener.wake_flag = True
            self._save()
            self.log(f"🎤 hearing threshold {round(bar) if bar > 0 else 'automatic'}")
        elif name == "pause":
            self.set_paused(not Speaker.paused)
        elif name == "mute":
            self.mic.user_muted = not self.mic.user_muted
            self.log(f"mic {'muted' if self.mic.user_muted else 'on'}")
        elif name in ("say", "set_mood", "play_song", "songs", "recent", "jump_scare"):
            return self.remote_command(name, data)
        elif name == "ask":
            text = str(data.get("text", ""))[:300].strip()
            if text:
                self.typed.put(text)
        else:
            return {"error": "unknown command"}
        return {"ok": True}

    def remote_command(self, name, data):
        """Puppet controls for the remote control (mcp_server.py). web.py only lets these through from the
        Pi itself or with the PUMPKIN_KEY."""
        if name == "say":
            text = str(data.get("text", ""))[:600].strip()
            if not text:
                return {"error": "nothing to say"}
            mood = data.get("mood")
            if mood and mood not in brain.MOODS:
                return {"error": f"mood must be one of {brain.MOODS}"}

            def speak():
                with self.idle_paused():
                    _speak()

            def _speak():
                before, was_awake = self.state, self.awake
                self.awake = True          # eyes open and lit up, even between visits
                if not was_awake:
                    self.reveal(True)
                if mood:
                    self.mood(mood)
                self.log("🎮 remote: say", "info")
                try:
                    self.say(text)
                finally:
                    if self.visit_session:   # so his next reply knows he said it
                        brain.note_said(self.visit_session, text)
                    if self.state == "speaking":
                        self.set_state(before)
                    if not was_awake and not self.visit_session:
                        self.awake = False
                        self.reveal(False)
            threading.Thread(target=speak, daemon=True, name="remote-say").start()
            return {"ok": True, "in_visit": bool(self.visit_session)}
        if name == "set_mood":
            mood = str(data.get("mood", ""))
            if mood not in brain.MOODS:
                return {"error": f"mood must be one of {brain.MOODS}"}
            self.mood(mood)
            return {"ok": True}
        if name == "songs":
            return {"songs": {sid: {"title": i["title"], "kind": i["kind"]} for sid, i in tunes.catalog().items()}}
        if name == "play_song":
            song = str(data.get("song_id", ""))
            if song not in tunes.catalog():
                return {"error": "unknown song; list them with songs"}
            threading.Thread(target=self.play_song, args=(song,), daemon=True, name="remote-song").start()
            return {"ok": True, "title": tunes.catalog()[song]["title"]}
        if name == "jump_scare":
            if self._scaring:
                return {"error": "a jump scare is already on its way"}
            try:
                delay = max(0.0, min(10.0, float(data.get("delay_s", 0) or 0)))
            except (TypeError, ValueError):
                return {"error": "delay_s must be a number of seconds (0-10)"}
            self._scaring = True
            threading.Thread(target=self.jump_scare, args=(delay, data.get("gotcha", True) is not False),
                             daemon=True, name="jump-scare").start()
            return {"ok": True, "in_s": delay}
        if name == "recent":
            try:
                n = max(1, min(400, int(data.get("lines", 60))))
            except (TypeError, ValueError):
                n = 60
            kinds = data.get("kinds")
            lines = [h for h in self.history if not kinds or h["kind"] in kinds]
            return {"lines": lines[-n:], "state": self.state}
        return {"error": "unknown command"}

    DARK_S, SCARE_S = 0.8, 1.6   # jump scare: lights out, then the monster face this long

    def jump_scare(self, delay=0.0, gotcha=True):
        """Remote control only (never the brain): the face goes dark, then a huge red monster face lunges out with a
        roar and a flash of the porch lights; then he giggles so little ones aren't left scared."""
        time.sleep(delay)
        with self.idle_paused():
            self._jump_scare(gotcha)

    def _jump_scare(self, gotcha):
        try:
            self.log("🎮 remote: jump scare", "info")
            was_awake, self.awake = self.awake, True
            self.reveal(True)
            roar, rate = _roar()
            with Speaker._output_lock:     # wait for anything he's saying, then nothing else talks over the roar
                self.hub.publish({"t": "scare", "dark_ms": int(self.DARK_S * 1000), "ms": int(self.SCARE_S * 1000)})
                # the speaker takes about lead_in + latency to start sounding; land the roar on the face
                time.sleep(max(0.0, self.DARK_S - self.speaker.lead_in - 0.15))
                threading.Thread(target=lambda: (time.sleep(0.15), self.props.arrival()), daemon=True).start()
                self.props.expression("surprised")
                self.speaking = True
                self.speaker.play_clip(roar, rate, on_level=self._set_mouth)
                self.speaking = False
                self.mouth = 0.0
            time.sleep(0.3)
            if gotcha:
                self.mood("silly")
                self.say(random.choice(GOTCHAS))
            if self.visit_session:
                brain.note_said(self.visit_session, "(a jump scare: lights out, then a monster face and a ROAR) "
                                + ("then: gotcha!" if gotcha else ""))
            if not was_awake and not self.visit_session:
                self.awake = False
                self.reveal(False)
        finally:
            self._scaring = False

    def _tour(self, name, route):
        if not self.show.summon(name, route[0]):
            return
        self.look_gaze = self.screens[route[0]]["gaze"]
        self.look_window_until = time.time() + 3
        time.sleep(2.5)
        for nxt in route[1:]:
            if not self.show.fly_to(nxt, on_leave=self._follow):
                return
            time.sleep(2.2)
        self.show.speak("Wheee! Boo!", lambda: False)

    # ---- auto-alignment from a phone photo ----------------------------------------------------
    def _align_target(self, data):
        t = str(data.get("target", ""))
        return t if t == "face" or t in self.window_calib else None

    def _get_warp(self, target):
        return self.face_warp if target == "face" else self.window_calib[target].get("warp")

    def _set_warp(self, target, warp):
        if target == "face":
            self.face_warp = warp
            self.calib = {"x": 0.0, "y": 0.0, "scale": 0.9, "rot": 0.0, "stretch": 1.0}   # the fit replaces manual nudges
            self.hub.publish({"t": "calib", "calib": self.calib})
            self.hub.publish({"t": "face_warp", "warp": warp})
        else:
            w = self.window_calib[target]
            w.update(NO_CALIB)
            if warp:
                w["warp"] = warp
            else:
                w.pop("warp", None)
            self.hub.publish({"t": "window_calib", "screen": target, "calib": w})
        self._save()

    def align_command(self, name, data):
        target = self._align_target(data)
        if not target:
            return {"error": "pick the face or a window to align"}
        if name == "align_start":
            return self.aligner.start(target)
        if name == "align_photo":
            return self.aligner.photo(target, "face" if target == "face" else "window", str(data.get("image", "")))
        if name == "align_corners":
            return self.aligner.corners(target, data.get("corners") or [])
        if name == "align_done":
            return self.aligner.done(target)
        if name == "align_undo":
            self.aligner.done(target)
            self._set_warp(target, None)
            return {"ok": True, "msg": "Auto-alignment removed."}
        return {"error": "unknown command"}

    def adjust_window(self, screen, key):
        w = self.window_calib[screen]
        if w.get("warp"):
            return self._nudge_warp(screen, key)
        moves = {"left": ("x", -0.01), "right": ("x", 0.01), "up": ("y", -0.01), "down": ("y", 0.01),
                 "rotl": ("rot", -0.02), "rotr": ("rot", 0.02)}
        if key in moves:
            k, d = moves[key]
            w[k] = round(w[k] + d, 4)
        elif key == "bigger":
            w["scale"] = round(w["scale"] * 1.05, 4)
        elif key == "smaller":
            w["scale"] = round(w["scale"] / 1.05, 4)
        elif key == "mirror":
            w["mirror"] = not w["mirror"]
        elif key == "reset":
            w.update(x=0.0, y=0.0, scale=1.0, rot=0.0, mirror=False)
        self.hub.publish({"t": "window_calib", "screen": screen, "calib": w})
        self._save()

    def _nudge_warp(self, screen, key):
        """The W buttons move, resize, turn or mirror an auto-aligned window."""
        w = self.window_calib[screen]
        q = [list(p) for p in w["warp"]["quad"]]
        cx, cy = sum(p[0] for p in q) / 4, sum(p[1] for p in q) / 4
        moves = {"left": (-0.01, 0), "right": (0.01, 0), "up": (0, -0.01), "down": (0, 0.01)}
        if key in moves:
            q = [[x + moves[key][0], y + moves[key][1]] for x, y in q]
        elif key in ("bigger", "smaller"):
            f = 1.05 if key == "bigger" else 1 / 1.05
            q = [[cx + (x - cx) * f, cy + (y - cy) * f] for x, y in q]
        elif key in ("rotl", "rotr"):
            a = -0.02 if key == "rotl" else 0.02
            q = [[cx + (x - cx) * math.cos(a) - (y - cy) * math.sin(a),
                  cy + (x - cx) * math.sin(a) + (y - cy) * math.cos(a)] for x, y in q]
        elif key == "mirror":
            q = [q[1], q[0], q[3], q[2]]
        elif key == "reset":
            return self._set_warp(screen, None)
        w["warp"]["quad"] = [[round(x, 5), round(y, 5)] for x, y in q]
        self.hub.publish({"t": "window_calib", "screen": screen, "calib": w})
        self._save()

    def _save(self):
        STATE_FILE.write_text(json.dumps({"gaze_flip": self.gaze_flip, "calib": self.calib, "face_warp": self.face_warp,
                                          "window_calib": self.window_calib,
                                          "volume": Speaker.volume, "wake_bar": self.listener.wake_bar,
                                          "wake_flag": self.listener.wake_flag}))

    @functools.cached_property
    def version(self):
        """The installed code: git commit and when it was made (shown on the control page)."""
        try:
            out = subprocess.run(["git", "log", "-1", "--format=%h %cd", "--date=format-local:%b %d %H:%M"],
                                 cwd=os.path.dirname(os.path.abspath(__file__)), capture_output=True, text=True, timeout=5).stdout.strip()
            return out or "unknown"
        except Exception:
            return "unknown"

    def status(self):
        if not hasattr(self, "listener"):   # the debug panel can ask while the camera and mic are still starting
            return {"state": "starting", "people": 0, "volume": round(Speaker.volume * 100), "version": self.version}
        people, _, _, _ = self.vision.state()
        st = {"state": self.state, "people": len(people), "detect_ms": round(self.vision.detect_ms),
              "fake": self.cfg["claude"]["fake"], "model": self.cfg["claude"]["model"],
              "mic_muted": self.mic.user_muted,
              "paused": Speaker.paused,
              "version": self.version,
              "wake_bar": round(self.listener.wake_bar) if self.listener.wake_bar else 0,
              "wake_auto": round(max(self.listener.noise * 1.8, 50.0)),
              "hearing_test": bool(self.listener.test),
              "volume": round(Speaker.volume * 100),
              "creature": (self.show.on_stage or {}).get("name") if self.show else None,
              "creature_window": self.show.screen if self.show else None,
              "windows": list(self.screens) if self.show else [],
              "windows_on": self.show.online() if self.show else []}
        if self.doa and self.doa.available:
            st["doa_raw"] = self.doa.raw()
            st["doa_bearing"] = self.doa.bearing(max_age=3)
        return st

    def debug_jpeg(self):
        if not hasattr(self, "listener"):
            return None
        return self.vision.debug_jpeg(self.doa.bearing(max_age=1.5) if self.doa and self.doa.available else None)


def main():
    for stream in (sys.stdout, sys.stderr):   # the log has emoji; Windows consoles and log files default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    cfg = config.load()
    Pumpkin(cfg).run()


if __name__ == "__main__":
    main()
