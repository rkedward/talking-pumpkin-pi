# Talking Pumpkin

Projects an animated jack-o'-lantern face onto a pumpkin. A camera spots trick-or-treaters, the eyes
follow them and turn toward whoever is talking, Claude greets them by their costume with a joke (and
laughs at it), and the pumpkin answers questions out loud with a neural voice and lip sync. Built for a
Raspberry Pi 5; a [Windows laptop version](#windows-laptop-version) runs the same pumpkin. Parts list: [PARTS.md](PARTS.md). Design notes: [PLAN.md](PLAN.md).

**Start with the core pumpkin and get it working on its own.** Everything else is an add-on: each one is
off until you turn it on in `config.toml`, works without the others, and can be added in any order.

| | What it adds | Extra hardware |
|---|---|---|
| **1. Core pumpkin** | Face on the pumpkin, camera, conversation, eyes that follow kids | Pi 5, camera, mic array, speaker, one projector |
| Add-on: [Window side show](#add-on-window-side-show) | Jack's assistants appear in your windows; start with one window, add a second later | 1 or 2 window projectors |
| Add-on: [Doorbell and Home Assistant](#add-on-doorbell-and-home-assistant) | The doorbell wakes the pumpkin, a light comes on | Home Assistant (your Pi 4), a doorbell and light it supports |
| Add-on: [Fog and lights](#add-on-fog-and-lights) | A puff of fog and flashing lights when kids arrive | Relay, fog machine |
| Add-on: [Moving eyebrows](#add-on-moving-eyebrows) | Servo eyebrows that react as he talks | Servo HAT, 2 servos |

## 1. Core pumpkin

### 1a. Set up the Pi

1. Flash **Raspberry Pi OS (64-bit, with desktop)** with Raspberry Pi Imager. In its settings, set a
   username, Wi-Fi and enable SSH.
2. Connect the camera (Pi 5 camera cable), the reSpeaker mic array, USB speaker, and the face projector on
   the micro-HDMI port next to the USB-C power. Plug the mic array in by the USB-C port on the round
   mic board, not the one on the small XIAO module.
3. Copy this `pumpkin` folder to the Pi (e.g. `scp -r pumpkin <user>@<pi>.local:~`), then on the Pi:
   ```
   cd ~/pumpkin
   bash install_pi.sh
   sudo reboot
   ```
   The installer asks for your Claude API key (leave it blank to try fake mode first). It installs
   everything, downloads the voice and speech models, and sets the pumpkin to start at boot with the
   face full screen.
4. After the reboot the face appears on the projector. Walk in front of the camera.

**reSpeaker XVF3800 with XIAO ESP32S3**: this version ships with firmware for the XIAO, so the Pi can't
hear it until you load its USB firmware once. After `install_pi.sh`: unplug the mic array, hold its
**Mute** button while plugging it back in (red LED blinks), then run `bash flash_xvf3800.sh`, and
unplug/replug it. `arecord -l` should then list the XVF3800. (The plain XVF3800 and the older
ReSpeaker v2.0 work without this step.)

Logs: `journalctl --user -u pumpkin -f` · Restart: `systemctl --user restart pumpkin`
To use OpenAI (ChatGPT's maker) instead of Claude, put `OPENAI_API_KEY=...` in `.env` and leave
`ANTHROPIC_API_KEY` out: the pumpkin then uses OpenAI for the conversation (`[claude] openai_model`,
default `gpt-5-mini`). Add `engine = "openai"` under `[tts]` for OpenAI's voice too.
Add the API key later by editing `~/pumpkin/.env` (`ANTHROPIC_API_KEY=...`, and delete the
`PUMPKIN_FAKE=1` line), then restart.

### 1b. Line it up on the porch

Use your phone as a remote: in `config.toml` set `host = "0.0.0.0"` under `[web]`, restart, then open
`http://<pi-name>.local:8000/?debug` on your phone (same Wi-Fi). You get the camera view with boxes
(orange = who the pumpkin is looking at, green line = where a voice is coming from), the log, and buttons.

- **Fit the face automatically**: pick "Align: face" and tap **Show pattern**. The projector shows a white
  frame with four black-and-white squares. Stand where the kids will stand, tap **Take photo** and
  photograph the pumpkin with the whole pattern in view. The Pi finds the squares and the pumpkin's outline,
  then fits the face to it, fixing position, size and keystone skew. The message says how far off it was;
  take another photo to check, up to three tries (usually two). If it picked the wrong outline, drag the
  orange corners on your photo onto the pumpkin's edges and tap **Use my corners**. Tap **Done** to save
  (the projector flashes an orange outline of the fit), or **Undo** to remove it.
- **Fine-tune by hand**: tap Guide, then the arrow / bigger / smaller / rotate buttons. It's saved on the
  Pi. (With a keyboard on the Pi: `C`, arrows, `+`/`-`, `[`/`]`, `{`/`}`.)
- **Eyes look the wrong way**: tap Flip eyes (or press `F`).
- **Ignore the sidewalk**: if people far away trigger it, raise `min_height` in `[detector]`; if kids at
  the door don't, lower it.
- **Point the mic array**: stand straight in front of the pumpkin and talk; the status line shows
  "voice N°". Put that N in `offset_deg` under `[doa]` and restart. Then walk sideways while talking:
  the green line in the camera view should follow you; if it goes the opposite way, set `flip = true`.
- **Test without talking**: type a question in the box. "Greet now" forces a greeting, "End visit" stops one.

### 1c. Jack's personality

Jack is jolly: silly, warm, and he laughs at his own jokes ("Ho ho ho!"). His eyes squint into happy
crescents and his face bounces while he laughs. With little kids or mixed groups he stays fun, never
scary. If the camera shows a group that is clearly all teenagers, he and his assistants get spookier and
campier, but never gory or mean. When Claude can't tell, it treats them as little kids. The prompt lives in
`brain.py` (`PERSONA`) if you want to change his style.

### 1d. Rehearse

Try it with family before Halloween night. With no API key (fake mode) it uses canned lines, which is handy
for checking the camera, eyes, voice and mic; add the key for real costume jokes. Once the core pumpkin
works on its own, add whichever add-ons you want below.

### 1e. Let kids interrupt him (barge-in)

Like ChatGPT's voice mode, the pumpkin can keep listening while he talks and stop the moment someone talks
over him; he then hears out what they said and answers that. Shouting the answer during the song game
stops the song too.

The trick is not hearing himself. The XVF3800 mic array cancels the echo of whatever it plays *itself*, so
the speaker has to be plugged into the mic array rather than into the Pi:

1. Plug a powered speaker into the mic array's 3.5 mm jack (or a small passive 4-8 ohm speaker into its JST
   speaker connector). The USB-only speaker (item 9) can't be used for this.
2. In `config.toml` set `output_device = "XVF3800"` under `[tts]`. If it is quiet, run `alsamixer`, pick the
   XVF3800, raise PCM to 100% and `sudo alsactl store`.
3. Restart. The log says `barge-in on ... (mic array cancels his voice)`.

The echo canceller needs a few seconds of his voice to settle after starting up. `[mic] barge_in = true`
tries barge-in with a separate speaker as well, but then kids have to be clearly louder than his voice;
`false` turns it off. If he stops for noises that aren't speech, raise `barge_ratio` (1.5) or `barge_min_s`
(0.25); if kids have to shout, lower them.

### 1f. How a visit starts and ends

By default he listens instead of watching (`[visit] trigger = "sound"`):

- **A visit starts** when someone says "hello", "trick or treat", "knock" or "doorbell", knocks twice, rings the
  doorbell (see the add-on) or you press Greet; other talking nearby doesn't wake him (`[visit] wake_words`, or
  `wake_on = "any"` to wake for anyone talking). Tune knocks with `knock_ratio` (higher = less sensitive).
- **Not hearing you?** In the control page (`?debug`) press **🎤 Hearing test** and say "trick or treat" 2 or 3
  times from where kids will stand, with the song playing. It measures your voice against the music and room noise
  and suggests a **Threshold** (set it with the slider; lower = easier to wake, all the way left = automatic). The log shows what it
  measured.
- **A visit ends** right away when a visitor says "bye", "goodbye" or "adios" (`[visit] bye_words`).
- **The camera only looks when he wants to see**: one picture when they arrive, for costumes. If nobody is in view
  he asks them to step in front of him and takes another look. It never tracks people, so the Pi stays nearly idle.
- **His eyes** follow whoever is talking, from the mic array's direction.
- **A visit ends** after `quiet_end_s` (12 s) of nobody talking (he asks "are you still there?" first), when they
  say bye, or at `max_turns` / `max_visit_s`.

`trigger = "camera"` brings back the old way: person detection starts the visit, the eyes follow people, and the
visit ends when they walk away.

## Add-ons

### Add-on: Window side show

Jack's assistants appear in a window and talk back in their own voices: Boo the giggly ghost, the
Peepers (glowing eyes that talk all at once), and Knuckles the skeleton butler, plus any clips of your
own. Jack tells the kids where to look and glances there himself. Start with one window; a second one
lets Boo fly between them.

**First window**

1. **Get the assistants' voices**: turn the side show on in `config.toml` (step 3), then rerun
   `bash install_pi.sh` once to download their voices.
2. **Set up the projector.** A smart projector on Wi-Fi like the Magcubic HY300 Pro+ needs no cable:
   - Give the Pi a fixed address: in your router's settings, reserve an IP address for it (often called
     "DHCP reservation"). The projector opens that address, so it must not change.
   - Connect the projector to your Wi-Fi and install a kiosk browser.
     [Fully Kiosk Browser](https://www.fully-kiosk.com/) works well: download its APK onto a USB stick
     from a computer, plug the stick into the projector, and open the APK from the projector's file
     manager (allow installing from unknown sources when asked).
   - In Fully Kiosk, set the **Start URL** to `http://<pi address>:8000/window.html?w=door` (the
     pumpkin's log prints the exact address). Turn on **Autoplay videos**, **Keep screen on** and **Start
     on boot**.
   - In the projector's settings, turn off the screensaver or sleep timer, and choose rear projection if it
     has it (otherwise use "W mirror" below).

   A projector without Wi-Fi can plug into the Pi's second micro-HDMI port instead (the one farther from
   the power jack); add `hdmi = true` to its window below.
3. **Turn it on** in `config.toml`, describing where the window is from where the kids stand. `gaze` is
   where Jack's eyes glance (x: -1 left to 1 right, y: -1 up to 1 down, as the kids see it):
   ```
   [window]
   enabled = true

   [window.screens.door]
   where = "the front door window behind you"
   gaze = [-1.0, -0.3]
   ```
   Restart (`systemctl --user restart pumpkin`). The window page shows `window "door" connected`, then
   goes black until an assistant appears.
4. **Line it up** from your phone's debug page. Easiest: let the projector shine a little past the glass on
   every side, pick "Align: door window", tap **Show pattern**, and take a photo from outside where the kids
   will stand. The Pi finds the lit glass, fits the picture inside it, and corrects keystone skew and
   rear-projection mirroring by itself; a second photo confirms it. Drag the corners if the glass edges
   were missed, then tap **Done**. "Summon test" shows an assistant to check, and the W ← → ↑ ↓ / bigger /
   smaller / mirror buttons fine-tune it by hand.

**Second window (optional).** Add another table with its own name, set up its projector the same way with
`?w=<that name>` in the address, and allow two summons per visit:
```
[window]
enabled = true
max_summons = 2

[window.screens.garage]
where = "the garage window on your left"
gaze = [-1.0, 0.0]
```
Pick the window in the debug page's drop-down to align it; **Identify** flashes each window's name on its
own glass. With two or more windows, Boo can fly from one to the other: he shoots out the side of one
window with a whoosh and swoops into the next, while Jack's eyes follow him. Claude sends him flying when
kids want to see a trick, and **Ghost tour** on the debug page tests it. The direction comes from each
window's `gaze`, so set those roughly right. `fly_gap_s` (default 0.8) is how long he's out of sight
between windows.

**Your own clips (optional).** Copy creature videos (black background, MP4 or WebM, 720p) into
`static/creatures/`. Give them names, personalities and voices in `static/creatures/creatures.toml`; add
`windows = ["door"]` to keep a clip in one window, like a skeleton knocking on the door. A clip's own
sound plays from that window's projector.

The pumpkin summons at most `max_summons` times per visit, usually after its joke or when kids ask for a
trick, and only into windows whose projector is on, so switching one off for the night is fine. While an
assistant is showing, kids can talk to it and it answers.

### Add-on: Doorbell and Home Assistant

When the real doorbell rings, the pumpkin wakes up and greets the kids. With the side show on, Knuckles
the butler answers in the door window first ("Ding dong! Visitors, sir! Allow me to get the lights.") and
Home Assistant turns on the porch light. It works with Home Assistant, or anything else that sends and
receives webhooks (Node-RED, Hubitat, Homey). The pumpkin also reports `visit_start`, `visit_end` and
`summon`, so other lights or music can follow along.

**Pumpkin side**, in `config.toml` (use your Home Assistant's address):
```
[face]
hide_when_idle = true          # optional: the pumpkin stays dark until someone arrives

[home]
enabled = true
key = "pick-a-secret"          # the doorbell request must send this

[home.webhooks]
doorbell = "http://homeassistant.local:8123/api/webhook/pumpkin_doorbell"
```
Test without a doorbell:
`curl -X POST -H 'Content-Type: application/json' -d '{"key":"pick-a-secret"}' http://localhost:8000/api/doorbell`

**Home Assistant side** (on its own device, like a Pi 4 running Home Assistant OS), in
`configuration.yaml` or the same automations built in the UI. Replace the pumpkin's address and the
doorbell and light entities with yours:
```yaml
rest_command:
  pumpkin_doorbell:
    url: "http://192.168.1.50:8000/api/doorbell"    # the pumpkin Pi's fixed address
    method: post
    content_type: "application/json"
    payload: '{"key": "pick-a-secret"}'

automation:
  - alias: "Doorbell wakes the pumpkin"
    triggers:
      - trigger: state
        entity_id: event.front_door_ding            # your doorbell's ring/press entity
    actions:
      - action: rest_command.pumpkin_doorbell
  - alias: "Pumpkin butler turns on the porch light"
    triggers:
      - trigger: webhook
        webhook_id: pumpkin_doorbell
        allowed_methods: [POST]
        local_only: true
    actions:
      - action: light.turn_on
        target:
          entity_id: light.porch
```

### Add-on: AI assistant remote control (MCP)

Let an AI assistant drive the pumpkin: "make the pumpkin say 'Nice Spider-Man costume!' with his tongue out",
"play the song game", "end the visit", "what did the kids ask him tonight?", "is he OK?". `mcp_server.py` is
an [MCP](https://modelcontextprotocol.io) server with tools to say something (with a face), set his face,
greet, end a visit, pass him a question, play a song, change the volume, mute the mic, check his status, read
the conversation and log, see the camera, run the echo test, send the ghost on a tour, and set off a jump
scare (lights out, then a red monster face lunges out with a roar; he giggles "Gotcha!" after). Only you
can trigger the scare, optionally a few seconds later (`delay_s`); he never does it on his own. He still chats with
visitors on his own; this is for stepping in. Whatever he says mid-visit, his next reply knows about.

On the Pi, with the pumpkin running, add it to Claude Code:

```bash
cd ~/pumpkin && .venv/bin/pip install "mcp>=2.3"
claude mcp add pumpkin -- ~/pumpkin/.venv/bin/python ~/pumpkin/mcp_server.py
```

From Claude Desktop on a laptop, start it over SSH (in its MCP settings):
`"pumpkin": {"command": "ssh", "args": ["you@pumpkin.local", "~/pumpkin/.venv/bin/python", "~/pumpkin/mcp_server.py"]}`

It talks over stdio and opens nothing new to the network. The puppet commands on the web API (`/api/say`,
`/api/set_mood`, `/api/play_song`, `/api/recent`) only answer the Pi itself, or a request carrying the
`X-Pumpkin-Key` header that matches `PUMPKIN_KEY` in `.env`. Don't expose the web port to the internet.

### Add-on: Singing pumpkins while he waits (idle video)

Show a video, such as AtmosFX's singing pumpkins, while nobody is visiting. When a visit starts it fades into the
live face, and it comes back after the goodbye. Copy the video to the Pi (e.g. `static/atmos/`) and set:

```toml
[idle]
video = "static/atmos/your-video.mp4"
# sound = true                 # soundtrack through his speaker; the mic array cancels it so he still hears "trick or treat"
# volume = 0.8
```

The soundtrack is decoded once with ffmpeg. The picture plays muted in the face page, lined up with the sound.

### Add-on: Fog and lights

A puff of fog and a flash of lights when kids arrive. Use a plug-in power relay for lights (no mains
wiring) and set its GPIO pin; for fog, wire a small relay across the fog machine's remote button only if
a meter shows the remote is low voltage. Otherwise run the fog machine on its own timer.
```
[props]
lights_pin = 27
fog_pin = 17
```

### Add-on: Moving eyebrows

Two servos with black craft-foam brows above the projected eyes, on a PCA9685 servo HAT with its own 5 V
supply. They raise, frown and wiggle with what Jack says.
```
[props]
eyebrows = "pca9685"
brow_left = 0
brow_right = 1
```

## Settings

Edit `config.toml` (created from `config.example.toml`; every option and its default is in `config.py`).
The most useful ones:

| Setting | Default | What it does |
|---|---|---|
| `[claude] model` | `claude-opus-5-5` | `claude-haiku-5-5` replies faster. |
| `[camera] hfov_deg` | 66 | Camera's horizontal field of view (Camera Module 3 Wide: 102). Used to match voices to people. |
| `[detector] min_height` | 0.25 | Smallest person (fraction of frame height) that counts as a visitor. |
| `[visit] max_turns`, `max_visit_s` | 3, 45 | Wrap up so the line keeps moving. |
| `[visit] trigger` | `sound` | `sound`: words, a knock, the doorbell or Greet start a visit; `camera`: person detection does. |
| `[stt] model` | `base.en` | `tiny.en` is faster, `small.en` more accurate (slower on the Pi). |
| `[stt] engine` | `whisper` | `openai` sends speech to OpenAI (needs `OPENAI_API_KEY`): about 0.5 s faster and much better with kids' voices, at well under a cent a visit. Whisper on the Pi takes over if the internet drops. Visitors' voices leave the porch, so a small sign is a nice touch. |
| `[tts] engine` | `piper` | `openai` or `elevenlabs` for natural cloud voices, `espeak` for a robotic fallback. |
| `[tts] piper_voice` | `en_US-ryan-medium` | Any voice from the Piper voice list; rerun `install_pi.sh` to download it. Try `en_GB-alan-medium` or `en_US-joe-medium`. |
| `[tts] pitch_semitones` | -2 | Deeper (negative) or squeakier (positive) voice. |
| `[props] eyebrows` | `none` | `pca9685` for the servo HAT/bonnet, `gpio` for servos straight on GPIO pins. |
| `[props] fog_pin`, `lights_pin` | 0 (off) | GPIO numbers of the fog and light relays, e.g. 17 and 27. |

OpenAI: set `engine = "openai"` (and optionally `openai_voice`, e.g. `cedar`, `marin`, `ash`, `onyx`), and add
`OPENAI_API_KEY=...` to `.env`. The key comes from platform.openai.com (pay as you go, about a cent or two per
minute of speech); a ChatGPT subscription doesn't include it. `openai_instructions` sets the speaking style.

ElevenLabs: set `engine = "elevenlabs"` and `elevenlabs_voice_id`, and add `ELEVENLABS_API_KEY=...` to `.env`.

## Windows laptop version

The same pumpkin, with the same add-ons, can run on a Windows 10 or 11 laptop instead of the Pi. Only
two add-ons need the Pi's GPIO pins and don't work on a laptop: the moving eyebrows and the fog and light
relays. For lights and fog, use smart plugs through the Home Assistant add-on instead.

**Hardware**: a 64-bit laptop with 8 GB of RAM, a USB webcam set right by the pumpkin (the Pi camera
module only fits a Pi), the mic array or another USB mic, a USB speaker, and the projector on the
laptop's HDMI port. A built-in webcam and mic work for trying it out on a desk, but the eyes need the
camera next to the pumpkin, and a built-in mic hears poorly outdoors.

1. **Copy** the `pumpkin` folder onto the laptop (unzip it somewhere like `Documents\pumpkin`).
2. **Double-click `windows\install.bat`.** It installs Python 3.12 if needed, the Python packages and the
   models (about 1 GB), asks for your Claude API key (leave it blank to try practice mode first), and puts
   **Talking Pumpkin** and **Stop Pumpkin** shortcuts on the desktop. It can also start the pumpkin
   automatically when you log in. Run it again any time; it skips what's already done.
3. **Plug in** the projector and set Windows to *Extend* the display (Windows key + P), then the camera,
   mic and speaker.
4. **Double-click Talking Pumpkin.** The face opens full screen on the projector and the debug panel on the
   laptop's own screen, so you can line up the face and watch the log right there. `logs\pumpkin.log` has
   the full log.

**Laptop settings** to check once:
- Settings > System > Power: never sleep when plugged in. If you close the lid, set closing the lid to "Do
  nothing".
- Settings > Privacy & security > Camera and Microphone: allow desktop apps.
- Settings > System > Sound: pick the mic array and USB speaker, or name them in `config.toml` (`[mic]
  device`, `[tts] output_device`).
- If it opens the built-in webcam instead of the USB one, set `usb_index = 1` under `[camera]`. Set
  `hfov_deg` to your webcam's field of view (most are 70 to 78 degrees).
- For the phone remote, Wi-Fi window projectors or Home Assistant, allow Python through the Windows
  firewall when asked, and give the laptop a fixed address in your router.

**reSpeaker XVF3800 (XIAO version) on Windows**: its USB firmware still has to be loaded once. The
easiest way is on the Pi (or any Mac or Linux computer) with `flash_xvf3800.sh`. After that it's a normal
USB microphone in Windows. Turning the eyes toward voices also needs its control interface switched to
the WinUSB driver with [Zadig](https://zadig.akeo.ie/); that's optional and a bit fiddly, and without it
the eyes simply follow the camera.

**Mac or Linux laptop** (for bench testing):
```
python3 -m pip install -r requirements.txt numpy opencv-python-headless
python3 windows/fetch_models.py
PUMPKIN_FAKE=1 python3 main.py          # or put ANTHROPIC_API_KEY=... in .env and drop PUMPKIN_FAKE
```
Open http://localhost:8000 (add `?debug` for the panel). On Linux install `libportaudio2` first.

## Files

| File | What it does |
|---|---|
| `main.py` | The brain: visit state machine, where to look, ties everything together. |
| `vision.py` | Pi camera / webcam capture, person detection and tracking, snapshots. |
| `audio.py` | Mic, voice activity detection, Whisper speech recognition, Piper/ElevenLabs/espeak voice, lip-sync levels. |
| `doa.py` | Voice direction from the reSpeaker XVF3800 or ReSpeaker v2.0 mic array. |
| `props.py` | Eyebrow servos, fog and light relays. |
| `sideshow.py`, `static/window.*` | Second-projector side show: creature catalog, creature voice, the window page. |
| `brain.py` | Claude prompts: costume greeting and Q&A. |
| `home.py` | Home automation: doorbell in, webhooks out. |
| `web.py` | Serves the face page, live state, camera view and remote-control commands. |
| `mcp_server.py` | Remote control for AI assistants (MCP): say, faces, songs, greet, status, conversation, camera. |
| `align.py`, `static/warp.js` | Auto-alignment from a phone photo: pattern, marker detection, keystone fit. |
| `static/` | The projected face (`face.js`) and debug panel (`app.js`). |
| `install_pi.sh`, `kiosk.sh` | Pi installer and full-screen browser launcher. |
| `windows\` | Windows laptop installer (`install.bat`), launcher (`start.bat`), stopper (`stop.bat`) and model download. |
| `flash_xvf3800.sh` | One-time USB firmware load for the XVF3800 mic array (XIAO version). |
| `models/` | Person detector (bundled); voice and Whisper models are downloaded by the installer. |
