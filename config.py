"""Settings: built-in defaults, overridden by config.toml next to this file."""

import copy
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DEFAULTS = {
    "claude": {
        "model": "claude-opus-5-5",   # claude-haiku-5-5 answers faster
        "effort": "low",
        "fake": False,                # canned replies, no API calls (PUMPKIN_FAKE=1 also works)
        "laugh": True,                # false = the pumpkin never says "Ha ha ha!"
        "laugh_text": "Ha ha ha!",
        "song_game": True,            # offer the "guess the song" game
        "song_source": "auto",        # auto: real song clips (run fetch_songs.py once) plus music box | real | musicbox    # how it laughs, e.g. "Ho ho ho!"
        "provider": "auto",           # claude | openai | auto (Claude if ANTHROPIC_API_KEY is set, else OpenAI)
        "openai_model": "gpt-5-mini", # used when the provider is openai
        "openai_reasoning": "minimal",  # quick answers; "" to leave it to the model
    },
    "camera": {
        "source": "auto",             # auto | picamera | usb | file:/path/to/video.mp4
        "usb_index": 0,
        "width": 1280,
        "height": 720,
        "hfov_deg": 66,               # Camera Module 3: 66, Camera Module 3 Wide: 102, typical webcam: ~70
        "flip_horizontal": False,
        "flip_vertical": False,
    },
    "detector": {
        "model": "models/efficientdet_lite0.tflite",
        "min_score": 0.45,
        "min_height": 0.25,           # ignore people smaller than this fraction of the frame (far away)
        "threads": 4,
        "fps": 12,
        "visit_fps": 4,               # during a conversation: fewer detections, more CPU for speech (eyes still follow)
        "nice": 10,                   # lower CPU priority for detection than for speech (0 = same, 19 = lowest)
    },
    "visit": {
        "arrive_s": 0.9,              # person must be in view this long before the greeting
        "gone_s": 3.5,                # nobody seen this long = visit over
        "max_turns": 3,
        "max_visit_s": 45,
        "game_extra_turns": 2,        # a visit that plays the song game gets this many more turns ...
        "game_extra_s": 30,           # ... and this much more time
        "listen_s": 8,                # how long to wait for each question
        "regreet_s": 12,
        # What starts a visit: "sound" = someone says hello / trick or treat, knocks, rings the doorbell (or the
        # remote's Greet); the camera only takes a picture for costumes and never tracks. "camera" = the old way:
        # person detection starts the visit, the eyes follow people, and the visit ends when they walk away.
        "trigger": "sound",
        "wake_words": ["trick or treat", "hello", "knock", "doorbell", "door bell", "ding dong"],
        "bye_words": ["goodbye", "good bye", "bye", "adios", "adiós"],   # a visitor saying one of these ends the visit
        "wake_on": "words",           # any = anyone talking near him (the mic array's voice light + real words);
                                      # words = only the wake_words above
        "wake_stt": "cloud",          # cloud = the accurate speech-to-text (what's said near him before a visit goes
                                      # to OpenAI too); local = whisper on the Pi (private, mishears more)
        "use_array_vad": True,        # speech only counts while the mic array's own voice light is on
        "knock": True,                # a knock on the door starts a visit too
        "knock_ratio": 8.0,           # how far above the room's noise a knock must jump (higher = less sensitive)
        "knocks": 2,                  # knocks within 1.5 s that count ("knock knock")
        "cooldown_s": 0,              # sound mode: pause after a visit before listening for the next one
        "quiet_end_s": 12,            # sound mode: this long with nobody talking = "are you still there?", then bye
    },
    "mic": {
        "device": "ReSpeaker",        # part of the input device name; falls back to the default mic
        "channel": "auto",            # auto: 1 (speech-recognition mix) on the XVF3800, else 0; or a number
        "rate": 16000,
        "start_ratio": 3.0,           # speech = this many times louder than the background noise
        "min_rms": 250,
        "silence_end_s": 0.6,         # pause that ends what they say: lower answers sooner, too low cuts them off
        "max_utterance_s": 8,
        # Barge-in: keep listening while the pumpkin talks and stop when someone talks over him. "auto" turns
        # it on when he plays through the mic array (its 3.5 mm jack or speaker connector), whose echo
        # cancellation keeps him from hearing himself. true also tries it with a separate speaker (kids then
        # have to talk louder than his echo); false = never.
        "barge_in": "auto",
        "barge_in_finish": "sentence",   # talked over: finish the sentence he's on ("sentence") or stop at once ("now")
        "barge_min_s": 0.25,          # this much speech within barge_window_s counts as talking over him
        "barge_window_s": 0.4,
        "barge_ratio": 1.5,           # stricter than normal listening (start_ratio, min_rms) by this factor
        "barge_echo_margin": 2.5,     # and this many times louder than the predicted echo of his voice
        "barge_grace_s": 0.5,         # ignore the first moment of each reply
        "barge_vad": "auto",          # also require the mic array's own speech detector (auto: with echo cancellation)
    },
    "stt": {
        "engine": "whisper",          # whisper (on the Pi) | openai (cloud, whisper as backup) | none (type in the debug panel)
        "model": "base.en",           # whisper: tiny.en is faster, small.en more accurate
        "compute_type": "int8",
        "threads": 4,
        "openai_model": "gpt-4o-transcribe",        # or gpt-4o-mini-transcribe (cheaper; on the Pi both took ~0.7-1 s)
        "openai_timeout_s": 4.0,      # give up on the cloud and use whisper after this long
        "pause_camera": True,         # stop person detection while it transcribes (the camera competes for the CPU)
    },
    "tts": {
        "engine": "piper",            # piper | openai | elevenlabs | espeak | none
        "output_device": "",          # part of the output device name; empty = default. "XVF3800" plays through
                                      # the mic array's jack, which lets barge-in cancel the echo
        "sentence_pause": 0.35,       # seconds of silence after each sentence
        "question_pause": 1.0,        # seconds of silence after a question
        "mouth_lead": 0.1,            # move the mouth this much early: the face page and projector lag a little
        "stream_first": True,         # cloud voices: start playing the first sentence while it is still arriving
        "mouth_delay": 0.0,           # seconds to hold the mouth back if it moves before the sound (the echo test measures it)
        "output_latency": 0.25,       # speaker buffer, seconds: raise (0.4) if speech is choppy, lower for tighter lip sync
        "piper_voice": "models/voices/en_US-ryan-medium.onnx",
        "piper_length_scale": 1.15,   # >1 is slower
        "pitch_semitones": -2.0,      # deepen the voice a little (piper/espeak); 0 = off
        "openai_voice": "cedar",      # cedar, marin, ash, ballad, coral, echo, fable, onyx, sage, verse ...
        "openai_model": "gpt-4o-mini-tts",
        "openai_instructions": "You are Jack, a jolly, theatrical talking jack-o'-lantern on a porch on Halloween "
                               "night. Sound delighted and full of energy, like a big, bubbly, larger-than-life "
                               "uncle who can't stop smiling. Speak at a brisk, lively pace with a bright, "
                               "bouncy tone. Words like 'Ha ha ha' are laughter: never read them out as "
                               "words, actually laugh out loud, a big, breathy, rolling belly laugh that "
                               "bubbles up naturally from the joke before it.",
        "laugh_dir": "sounds/laughs", # .wav laugh recordings: played instead of a spoken "Ha ha ha!" when present
        "openai_speed": 1.1,          # 1.0 = normal; up to about 1.25 before it sounds rushed
        "elevenlabs_voice_id": "",
        "elevenlabs_model": "eleven_flash_v2_5",
        "espeak_voice": "en-us+m3",
    },
    "doa": {
        "enabled": True,              # ReSpeaker USB Mic Array v2.0 direction of arrival
        "offset_deg": 0,              # ReSpeaker angle that points straight out from the pumpkin
        "flip": False,
        "match_deg": 25,              # how close a voice direction must be to a detected person
    },
    "idle": {
        # A video shown while he waits for visitors (e.g. AtmosFX singing pumpkins); the live face takes over
        # when a visit starts and the video comes back after it. Path relative to the pumpkin folder, or a list
        # of paths to play in turn.
        "video": "",
        "sound": True,                # play its soundtrack through his speaker (needs ffmpeg; with the mic
                                      # array's echo cancellation he still hears "trick or treat" over it)
        "volume": 0.8,                # soundtrack loudness relative to his voice
        "fade_s": 1.0,                # crossfade between the video and the face
    },
    "face": {
        "gaze_flip": False,
        "hide_when_idle": False,      # true: the pumpkin is dark until someone arrives (or the doorbell rings)
        "screen": 0,                  # Windows laptop: display number for the face (1, 2, ...); 0 = the projector, i.e. the first non-main display
    },
    "props": {
        "eyebrows": "none",           # none | pca9685 | gpio
        "brow_left": 0,               # PCA9685 channel, or GPIO pin for the gpio backend
        "brow_right": 1,
        "brow_neutral_deg": 90,
        "brow_raise_deg": 30,
        "brow_invert_right": True,    # servos mounted mirror-image
        "fog_pin": 0,                 # GPIO for a relay across the fog machine's remote button; 0 = off
        "fog_seconds": 2.0,
        "fog_cooldown_s": 180,
        "lights_pin": 0,              # GPIO for a relay/MOSFET driving extra lights; 0 = off
        "active_high": True,
    },
    "window": {
        "enabled": False,             # side show: creatures appear in windows on other projectors
        "where": "the front door window behind you",   # how the pumpkin tells kids where to look
        # Several windows: one table per projector, e.g. [window.screens.garage] where = "..." gaze = [...]
        # hdmi = true for the one plugged into the Pi's 2nd HDMI port; the rest open window.html?w=<name> over Wi-Fi.
        # Empty = a single window called "door" on the Pi's 2nd HDMI port, using where/gaze above.
        "screens": {},
        "builtins": True,             # include the built-in animated ghost, shadow eyes, skeleton hand
        "stay_s": 20,                 # how long a built-in creature stays (videos play to the end)
        "max_summons": 1,             # per visit
        "gaze": [-1.0, -0.3],         # where the pumpkin's eyes glance when it summons (x, y in -1..1)
        "creature_voice": "",         # Piper voice for creatures without their own; empty = same voice as the pumpkin
        "creature_pitch": -7.0,       # semitones; deeper and ghoulish
        "creature_length_scale": 1.3, # slower, spookier
        "screen_x": 0,                # x position of the 2nd display; 0 = detect automatically
        "fly_gap_s": 0.8,             # a ghost flying between windows is out of sight this long
    },
    "home": {
        "enabled": False,             # home automation (Home Assistant, Node-RED, ...): doorbell in, webhooks out
        "key": "",                    # shared secret the doorbell request must send as {"key": "..."}
        "butler": "Knuckles",         # assistant who answers the doorbell (in the "door" window if there is one)
        "butler_window": "door",
        "reveal_delay_s": 1.5,        # after the butler, the pumpkin lights up
        # Webhook URL per event, e.g. Home Assistant: "http://homeassistant.local:8123/api/webhook/pumpkin_doorbell"
        # events: doorbell (the butler is turning on the lights), visit_start, visit_end, summon
        "webhooks": {},
    },
    "web": {
        "host": "127.0.0.1",          # 0.0.0.0 lets a phone on your Wi-Fi open the debug panel / remote
                                      # (switched on automatically for Wi-Fi window projectors and home automation)
        "port": 8000,
        "debug_screen": "auto",       # camera debug view on a small DSI screen: "auto", an output like "DSI-1", or "off"
    },
}


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_env_file():
    """KEY=value lines from .env (API keys) become environment variables, unless already set."""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#") and value:
                os.environ.setdefault(key.strip(), value.strip())


def load(path=None):
    _load_env_file()
    cfg = copy.deepcopy(DEFAULTS)
    path = Path(path or os.environ.get("PUMPKIN_CONFIG") or ROOT / "config.toml")
    if path.exists():
        with open(path, "rb") as f:
            _merge(cfg, tomllib.load(f))
    if os.environ.get("PUMPKIN_FAKE") == "1":
        cfg["claude"]["fake"] = True
    brain = cfg["claude"]
    if brain["provider"] == "auto":
        has_openai = os.environ.get("OPENAI_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY")
        brain["provider"] = "openai" if has_openai else "claude"
    if brain["provider"] == "openai":
        brain["model"] = brain["openai_model"]
    if os.environ.get("PUMPKIN_MODEL"):
        brain["model"] = brain["openai_model"] = os.environ["PUMPKIN_MODEL"]
    if os.environ.get("PUMPKIN_PORT"):
        cfg["web"]["port"] = int(os.environ["PUMPKIN_PORT"])
    return cfg


def screens(window_cfg):
    """The side-show windows: {name: {"where", "gaze", "hdmi"}}."""
    raw = window_cfg.get("screens") or {"door": {"hdmi": True}}
    out = {}
    for name, sc in raw.items():
        sc = sc if isinstance(sc, dict) else {}
        out[str(name)] = {"where": str(sc.get("where") or window_cfg["where"]),
                          "gaze": [float(v) for v in sc.get("gaze", window_cfg["gaze"])],
                          "hdmi": bool(sc.get("hdmi", False))}
    return out


def resolve(p):
    """Paths in the config are relative to the pumpkin folder."""
    p = Path(p)
    return p if p.is_absolute() else ROOT / p
