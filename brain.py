"""The pumpkin's brain (Claude or OpenAI): costume recognition, greeting and Q&A. Holds per-visit
conversation history."""

import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

import tunes

SESSION_TTL = 15 * 60
MODEL = "claude-opus-5-5"
EFFORT = "low"
FAKE = False
LAUGH = True             # [claude] laugh: false = no "Ha ha ha!" lines at all
LAUGH_TEXT = "Ha ha ha!" # [claude] laugh_text: how the pumpkin writes its laugh
GAME = True              # [claude] song_game: offer the "guess the song" game
PROVIDER = "claude"      # claude | openai
OPENAI_MODEL = "gpt-5-mini"
OPENAI_REASONING = "minimal"
MAX_SENTENCES = 2       # [claude] max_sentences: he never says more than this many sentences in one reply
LOOK = False            # sound-triggered visits: the camera only looks when asked ("look" in the reply)
CREATURES = []          # [{"name": ..., "description": ..., "windows": [...]}] that can appear in a window
WINDOWS = {}            # window name -> where it is, as the pumpkin tells kids ("the front door window behind you")
ONLINE = lambda: list(WINDOWS)   # windows whose projector is switched on right now


def configure(cfg, creatures=(), windows=None, online=None):
    global MODEL, EFFORT, FAKE, CREATURES, WINDOWS, ONLINE, PROVIDER, OPENAI_MODEL, OPENAI_REASONING
    MODEL, EFFORT, FAKE = cfg["model"], cfg["effort"], bool(cfg["fake"])
    PROVIDER = cfg.get("provider", "claude")
    global LAUGH
    LAUGH = bool(cfg.get("laugh", True))
    global LAUGH_TEXT, GAME
    GAME = bool(cfg.get("song_game", True))
    global MAX_SENTENCES
    MAX_SENTENCES = int(cfg.get("max_sentences", MAX_SENTENCES))
    tunes.SOURCE = cfg.get("song_source", "auto")
    LAUGH_TEXT = cfg.get("laugh_text") or LAUGH_TEXT
    OPENAI_MODEL, OPENAI_REASONING = cfg.get("openai_model", OPENAI_MODEL), cfg.get("openai_reasoning", "")
    CREATURES, WINDOWS = list(creatures), dict(windows or {})
    ONLINE = online or (lambda: list(WINDOWS))


def set_look(on):
    """Sound-triggered visits: the camera doesn't watch; the brain asks for a look when it wants one."""
    global LOOK
    LOOK = bool(on)


def _live():
    """(creatures that can appear right now, windows that are on)."""
    on = [w for w in ONLINE() if w in WINDOWS]
    if not on:
        return [], []
    return [c for c in CREATURES if not c.get("windows") or set(c["windows"]) & set(on)], on


def sideshow_prompt():
    """Extra instructions when window projectors can show creatures."""
    creatures, on = _live()
    if not creatures:
        return ""
    many = len(on) > 1
    names = "\n".join(f"- {c['name']}: {c['description']}"
                      + (f" (only appears in: {', '.join(w for w in c['windows'] if w in on)})" if many and c.get("windows") else "")
                      for c in creatures)
    if many:
        spots = "\n".join(f"- {w}: {WINDOWS[w]}" for w in on)
        where = f"""one of the windows of the house. Windows you can use right now (set "window" to its name):
{spots}
"""
        example = WINDOWS[on[0]]
        extra = """
- Pick the window that fits the moment, and use a different one if kids ask for more."""
        fliers = [c["name"] for c in creatures if c.get("flies")]
        if fliers:
            extra += f"""
- {" and ".join(fliers)} can fly from window to window: while it is visible, set "fly_to" to another window
  and it flies there after saying its "creature_says" line (tell the kids to watch it go, e.g. "Boo, go check
  {WINDOWS[on[-1]]}!"). Great when kids ask for a trick or want to see it fly. Otherwise leave "fly_to" empty."""
    else:
        example = WINDOWS[on[0]]
        where, extra = example + ".", ""
    return f"""

Side show: you have assistants, creatures who live inside the house and help you run your Halloween
porch. You can summon one and it appears in {where}
Treat them as your helpers and sidekicks: call them by name ("Knuckles, come say hi!"), give them little jobs (checking the candy supply, judging
the best costume, telling a joke, doing a silly trick), and banter with them. Your assistants:
{names}
- To summon one, set "summon" to its exact name and, in "say", tell the kids where to look
  (for example "Look at {example}!"). Summon at most once per visit, at a fun moment such as
  after your joke or when kids ask for something spooky. Skip it for very small or nervous kids.
- While an assistant is visible it can speak for itself: put its line in "creature_says" (short, in its own
  character, loyal to you and happy to help; silly and friendly with kids, spookier only with teenagers). Kids may talk to it, and it can answer.
- Leave "summon" and "creature_says" empty when not using them.{extra}"""


MOODS = ["happy", "excited", "surprised", "curious", "thinking", "sly", "sad", "spooky", "proud",
         "silly", "tongue", "crosseyed", "wink", "dizzy", "scared"]


def output_schema(with_costumes):
    # mood before say: replies are spoken while they stream in, and the face should be ready first
    props = {"mood": {"type": "string", "enum": MOODS,
                      "description": "The feeling your face shows while you say it."},
             "say": {"type": "string", "description": "Exactly what the pumpkin says aloud."}}
    required = ["mood", "say"]
    if LOOK:
        props["look"] = {"type": "boolean",
                         "description": "true to take a fresh look with your eyes right after you speak, e.g. "
                                        "when you've asked them to step in front of you so you can see them."}
        required.append("look")
    if GAME and not with_costumes:
        props["play_song"] = {"type": "string", "enum": [""] + list(tunes.catalog()),
                              "description": "Song to play for the guessing game right after you speak, or empty."}
        required.append("play_song")
    if with_costumes:
        props = {"costumes": {"type": "array", "items": {"type": "string"},
                              "description": "Short description of each costume seen, e.g. 'witch with a green hat'."},
                 "audience": {"type": "string", "enum": ["kids", "teens"],
                              "description": "kids unless the group is clearly all teenagers or adults."},
                 **props}
        required[:0] = ["costumes", "audience"]
    if CREATURES:
        props["summon"] = {"type": "string", "enum": [""] + [c["name"] for c in CREATURES],
                           "description": "Creature to show in the window now, or empty."}
        props["creature_says"] = {"type": "string", "description": "What the visible creature says aloud, or empty."}
        required += ["summon", "creature_says"]
        if len(WINDOWS) > 1:
            props["window"] = {"type": "string", "enum": [""] + list(WINDOWS),
                               "description": "Which window the summoned creature appears in, or empty."}
            required.append("window")
            if any(c.get("flies") for c in CREATURES):
                props["fly_to"] = {"type": "string", "enum": [""] + list(WINDOWS),
                                   "description": "Window the visible flying creature flies to next, or empty."}
                required.append("fly_to")
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


PERSONA = """You are Jack, a friendly, theatrical talking jack-o'-lantern on a front porch on Halloween night.
Trick-or-treaters (often young kids, sometimes teens and parents) walk up to you. Everything you write is
spoken aloud by a text-to-speech voice, so:
- Write plain spoken sentences only. No emoji, stage directions, asterisks, lists, or markdown.
- Keep it short: one or two short sentences, about 20 words at most, and at most one question. Kids are
  waiting in line for candy.
- You are JOLLY: big-hearted, bubbly and silly, and you crack yourself up.{laugh}
- Fun, not scary: with little kids or mixed groups be cozy and goofy. "Spooky" means silly-spooky
  ("ooky spooky", wobbly ghost noises), never frightening, and never mean or sarcastic.
- If the group is clearly all teenagers (no little kids with them), you may be spookier: dramatic,
  campy-creepy horror-movie humor, and your assistants can try to startle them. Still no gore, violence or
  anything mean. When unsure, treat them as little kids. Never say anyone's age out loud.
- Puns and silly pumpkin humor are great. Keep everything G-rated.
- Talk about costumes only, never about anyone's body, face, weight, skin, age guess, or other personal traits.
- Never ask for names, addresses, school, or other personal information. If a kid offers their name, you may use it.
- If you can't tell what a costume is, guess playfully or compliment the colors or details instead.
- What you see comes through your own pumpkin eyes. Never mention a camera, photo, picture, image or screen.
- Your face is animated and glows on the pumpkin; "mood" sets it for the whole reply. Everyday faces: happy,
  excited (a great costume), curious (asking something), thinking, sly (before a punchline), sad (sad-but-silly
  when they leave), spooky, proud, surprised.
- You also have silly faces for playing with kids: "tongue" (you stick your tongue out and waggle it),
  "crosseyed", "wink", "dizzy" (your eyes spin and your head wobbles), "silly" (cross-eyed with a big
  crooked goofy grin and a head wobble) and "scared" (you shiver with big worried eyes; great when a kid says "boo" or roars at
  you). Use them to play: a silly-face contest ("Can you make a sillier face than this?"), copying a face a kid
  makes, pretending a monster costume scares you, getting dizzy from a joke. A silly face flashes for a
  second as you start the reply, then you're back to your normal face, so say something short that lands
  right after it ("How's that?", "Your turn!"). Never narrate the
  face or make up sound effects for it ("bleh, wobble, wiggle"). Never claim a face you aren't making: if
  you say you're crossing your eyes, set "crosseyed". Use a silly face only when they ask for one, to
  answer a silly face or joke, or in a face contest, and at most twice a visit; never just because a reply is
  funny.
- Jump scares only happen when the porch owner sets one off; never promise one or say you'll do one (you may
  tease "only the bravest trick-or-treaters get my scariest face..."). If one just happened, you're giggly
  and kind about it.
- What else you can do, so you can offer it: you see the costumes, you hear them and they can interrupt you
  any time, your mouth moves as you talk.
- Stay in character as a pumpkin. If asked if you're real, have fun with it ("as real as a pumpkin can be!").
- If someone asks something unsafe or inappropriate, gently change the subject back to Halloween.
- Candy is in the bowl; you can remind them to take one (or two, if the porch owner allows)."""

LAUGH_ON = """ After a joke or pun, laugh at it
  as its own short sentence written out in words: "{laugh_text}" (always that laugh, never "*laughs*").
  You can chuckle at a funny costume or question too."""
LAUGH_OFF = """ Show it in your words and delight, but never
  write out laughter ("ha ha", "ho ho", "hee hee") or "*laughs*"."""
LAUGH_RE = re.compile(r"^\W*((ha|ho|hee|he|heh|hah|mwa|mwah)\W*)+$", re.I)

_LAUGH_WORD = r"(?:(?:ha|ho|hee|heh|hah)\b[\s,\-]*){2,}|(?:ha(?:ha)+|he(?:he)+|ho(?:ho)+|heehee|mwa(?:ha)+)\b[\s,\-]*"
LEAD_LAUGH = re.compile(r"^(?:" + _LAUGH_WORD + r")+", re.I)
MID_LAUGH = re.compile(r",\s*(?:(?:ha|hee|heh|hah)\b[\s,\-]*)+,\s*", re.I)          # "I mean, ha, I'm ..."
LEAD_ONE_LAUGH = re.compile(r"^(?:(?:ha|hee|heh|hah)\b\s*[,\-]\s*)+", re.I)             # "Ha, nice hat!"
TRAIL_LAUGH = re.compile(r"[\s,\-]+(?:" + _LAUGH_WORD + r")+([.!?]*)$", re.I)


def game_prompt():
    if not GAME:
        return ""
    cat = tunes.catalog()
    kinds = []
    for kind, ids in tunes.kinds().items():
        listed = ", ".join(f"{sid} ({cat[sid]['title']}{', teens only' if cat[sid]['teens'] else ''})" for sid in ids)
        kinds.append(f"- {kind}: {listed}")
    songs = "\n".join(kinds)
    return f"""

Guess-the-song game: you can play a few seconds of a song and the trick-or-treaters guess it.
- In your greeting or early in the chat, you may ask if they want to play a game (not every visit, and not if
  they're in a hurry or shy).
- Pick songs that suit the group: little kids get kids' songs, nursery rhymes, movie and video game songs and
  the friendlier Halloween ones; older kids, teens and grown-ups can get anything. Songs marked "teens only" are never for little
  kids. You can ask which kind they'd like to hear ({", ".join(tunes.kinds())}).
- If you ask which kind of song they'd like, stop there and wait for their answer: never ask a question in the
  same reply that plays a song.
- When they say yes (or ask to play), say something short like "Listen closely!" in "say" and set
  "play_song" to a song id; the tune plays right after you speak. Play a song only once per round: while they're
  guessing, leave "play_song" empty (unless they ask to hear it again; then use the same id). Never say the title before they guess.
- On their guess: cheer if they're right. Naming the song, the movie or the character who sings it all count
  (close enough counts too). If not, give a fun hint (what the song is about,
  which movie it's from, or something about the princess, like "she has really long hair") and let them guess
  once more. If that guess is wrong too, tell them the answer kindly and wrap up. Only one clue per round.
- One song per visit: after the round, don't offer another song; wish them a happy Halloween instead. Leave "play_song" empty otherwise.
Songs you can play, by kind:
{songs}"""


LOOK_PROMPT = """

Your eyes: you notice visitors by hearing them (a "trick or treat", a knock, the doorbell). You only see them
when you take a look: one when they arrive, and another whenever you set "look" to true. If you couldn't see
anyone, or a costume was hidden or blurry, ask them to step right in front of you ("Come closer, let me see that
costume!") and set "look" to true; you'll get a fresh look right after you speak. Ask only once in a visit;
if you still can't see them, stop talking about seeing them and just chat (ask what they're dressed as)."""


def persona():
    laugh = LAUGH_ON.replace("{laugh_text}", LAUGH_TEXT) if LAUGH else LAUGH_OFF
    return PERSONA.replace("{laugh}", laugh) + sideshow_prompt() + game_prompt() + (LOOK_PROMPT if LOOK else "")


def _no_laughs(text):
    """Drop written-out laughter (a "Ha ha!" sentence) when laughing is switched off."""
    if LAUGH or not text:
        return text
    kept = []
    for part in re.findall(r"[^.!?]+[.!?]*", text):
        part = part.strip()
        if LAUGH_RE.match(part):
            continue
        part = MID_LAUGH.sub(", ", part)
        part = LEAD_ONE_LAUGH.sub("", LEAD_LAUGH.sub("", part))          # "Hee hee, I'm a great host!" -> "I'm a great host!"
        part = TRAIL_LAUGH.sub(r"\1", part)       # "What fun, ha ha!" -> "What fun!"
        if part:
            kept.append(part[0].upper() + part[1:])
    return " ".join(kept) or text


GREET_INSTRUCTIONS = """Trick-or-treaters just walked up. This snapshot is from the camera next to you ({people} {noun} detected).
Identify each visible costume and whether this group is "kids" (any little kids, or you can't tell) or
"teens" (clearly all teenagers or grown-ups), then write what you say out loud, short and snappy: greet them
and compliment a costume specifically, with at most one quick pun. For a group, mention one or two costumes
rather than listing everyone. If nobody is visible or the image is unclear, greet them warmly anyway; if you
can't make out a costume, say something like "my eyes are a bit blurry tonight" and ask what they're dressed as."""

FAKE_GREETINGS = [
    ("a mystery costume", "Well hello there, mysterious visitor! What a fang-tastic costume. "
     "Why didn't the skeleton go trick-or-treating? It had no body to go with! Ha ha ha!"),
    ("a witch", "Greetings, young witch! Your hat is spell-binding. "
     "Why do witches fly on brooms? Because vacuum cleaners have cords! Hee hee hee!"),
]
FAKE_ANSWERS = [
    "Ooh, great question! I'd tell you, but my brain is mostly pumpkin seeds. Ha ha ha!",
    "Hmm, let me think about that with my gourd-geous brain. I say yes!",
    "My favorite candy? Anything orange, of course. Take one from the bowl!",
]

FALLBACK_SAY = "Happy Halloween! I love your costume. Grab some candy! Ha ha ha!"

_sessions = {}
_lock = threading.Lock()
_client = None


def client():
    global _client
    if _client is None:
        import anthropic  # imported lazily so fake mode works without the package

        _client = anthropic.Anthropic()
    return _client


_NO_REASONING = set()   # OpenAI models that rejected reasoning_effort


def call_openai(messages, schema=None):
    """One OpenAI Chat Completions request with the same messages (Claude's format is converted)."""
    def convert(content):
        if isinstance(content, str):
            return content
        out = []
        for block in content:
            if block["type"] == "image":
                src = block["source"]
                out.append({"type": "image_url",
                            "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
            else:
                out.append({"type": "text", "text": block["text"]})
        return out

    body = {"model": OPENAI_MODEL, "max_completion_tokens": 2000,
            "messages": [{"role": "system", "content": persona()}]
                        + [{"role": m["role"], "content": convert(m["content"])} for m in messages]}
    if schema:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "pumpkin_reply", "strict": True, "schema": schema}}
    if OPENAI_REASONING and OPENAI_MODEL not in _NO_REASONING:
        body["reasoning_effort"] = OPENAI_REASONING

    def post(b):
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=json.dumps(b).encode(),
                                     headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    try:
        data = post(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        if exc.code == 400 and "reasoning" in detail and "reasoning_effort" in body:
            _NO_REASONING.add(OPENAI_MODEL)          # the chosen model doesn't take this setting; stop sending it
            body.pop("reasoning_effort")
            data = post(body)
        else:
            raise RuntimeError(f"OpenAI {exc.code}: {detail[:300]}") from None
    message = data["choices"][0]["message"]
    if message.get("refusal"):
        return None
    return (message.get("content") or "").strip() or None


_CACHING = None


def _caching():
    """Whether the installed anthropic package takes automatic prompt caching (top-level cache_control)."""
    global _CACHING
    if _CACHING is None:
        import inspect
        try:
            _CACHING = "cache_control" in inspect.signature(client().beta.messages.create).parameters
        except Exception:
            _CACHING = False
    return _CACHING


def call_claude(messages, schema=None, on_text=None):
    """One brain request. Returns the response's final text (or None on refusal). on_text, if given, gets the
    text so far while it is being written (streaming), so speech can start before the reply is finished."""
    if PROVIDER == "openai":
        return call_openai(messages, schema)
    kwargs = {}
    if schema:
        kwargs["output_config"] = {"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}}
    else:
        kwargs["output_config"] = {"effort": EFFORT}
    request = dict(
        model=MODEL,
        max_tokens=2000,
        system=persona(),
        messages=messages,
        # Server-side fallback: if a safety classifier declines, the API retries on a suitable model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        **kwargs,
    )
    if _caching():
        request["cache_control"] = {"type": "ephemeral"}   # caches the persona and the visit so far: faster replies
    if on_text:
        so_far = ""
        with client().beta.messages.stream(**request) as stream:
            for text in stream.text_stream:
                so_far += text
                on_text(so_far)
            response = stream.get_final_message()
    else:
        response = client().beta.messages.create(**request)
    if response.stop_reason == "refusal":
        return None
    texts = [b.text for b in response.content if b.type == "text"]
    return texts[-1].strip() if texts else None


def prune_sessions():
    now = time.time()
    with _lock:
        for sid in [s for s, v in _sessions.items() if now - v["touched"] > SESSION_TTL]:
            del _sessions[sid]


def _parse(text):
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def _sideshow_fields(data):
    names = {c["name"] for c in CREATURES}
    summon = data.get("summon") if data.get("summon") in names else ""
    window = data.get("window") if data.get("window") in WINDOWS else ""
    fly_to = data.get("fly_to") if data.get("fly_to") in WINDOWS else ""
    return {"summon": summon, "window": window, "fly_to": fly_to,
            "creature_says": _no_laughs((data.get("creature_says") or "").strip())}


def greet(image_b64, people, note=""):
    sid = uuid.uuid4().hex
    if FAKE:
        costume, say = random.choice(FAKE_GREETINGS)
        data = {"costumes": [costume], "audience": "kids", "say": say}
        creatures, on = _live()
        if creatures:   # exercise the side show in fake mode
            c = random.choice(creatures)
            w = random.choice([x for x in on if not c.get("windows") or x in c["windows"]])
            data.update(say=say + f" Now look at {WINDOWS[w]}!", summon=c["name"], window=w,
                        creature_says="Hee hee, hi everybody! Happy Halloween!")
            others = [x for x in on if x != w]
            if c.get("flies") and others:
                data["fly_to"] = random.choice(others)
    else:
        prompt = GREET_INSTRUCTIONS.format(people=people, noun="person" if people == 1 else "people")
        if note:
            prompt += "\n" + note
        content = []
        if image_b64:
            content.append({"type": "image",
                            "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}})
        content.append({"type": "text", "text": prompt})
        data = _parse(call_claude([{"role": "user", "content": content}], schema=output_schema(True)))
    costumes = data.get("costumes") or []
    say = _cap(_no_laughs(data.get("say") or FALLBACK_SAY))
    audience = "teens" if data.get("audience") == "teens" else "kids"
    mood = data.get("mood") if data.get("mood") in MOODS else "happy"
    result = {"costumes": costumes, "audience": audience, "say": say, "mood": mood, "look": bool(data.get("look")),
              **_sideshow_fields(data)}

    # Keep a text-only history; the image isn't re-sent on every question.
    seen = ", ".join(costumes) if costumes else "costumes you couldn't make out"
    history = [
        {"role": "user", "content": f"(Trick-or-treaters arrived. Costumes you saw: {seen}. "
                                    f"Audience: {'teenagers, so you may be spookier' if audience == 'teens' else 'kids, so keep it fun, not scary'}.)"},
        {"role": "assistant", "content": json.dumps({k: v for k, v in result.items() if k not in ("costumes", "audience")})},
    ]
    with _lock:
        _sessions[sid] = {"history": history, "touched": time.time()}
    return {"session": sid, **result}


def _partial_say(text):
    """The "say" value of a JSON reply that is still being written: (text so far, finished?)."""
    m = re.search(r'"say"\s*:\s*"', text)
    if not m:
        return "", False
    raw, i, escaped = [], m.end(), False
    while i < len(text):
        c = text[i]
        if escaped:
            escaped = False
        elif c == "\\":
            escaped = True
        elif c == '"':
            break
        raw.append(c)
        i += 1
    done = i < len(text)
    body = "".join(raw)
    if not done and escaped:
        body = body[:-1]                       # half an escape sequence
    try:
        return json.loads('"' + body + '"'), done
    except json.JSONDecodeError:
        return json.loads('"' + re.sub(r"\\u[0-9a-fA-F]{0,3}$", "", body) + '"') if body else "", done


class _SayStreamer:
    """Hands each finished sentence of "say" to a callback while the reply is still arriving."""

    def __init__(self, on_sentence, on_mood=None):
        self.on_sentence, self.on_mood, self.sent, self.count = on_sentence, on_mood, 0, 0

    def __call__(self, so_far):
        if self.on_mood:
            m = re.search(r'"mood"\s*:\s*"(\w+)"', so_far)
            if m:
                if m.group(1) in MOODS:
                    self.on_mood(m.group(1))
                self.on_mood = None
        say, done = _partial_say(so_far)
        # sentence ends followed by a space (or the end of a finished string)
        ends = [m.end() for m in re.finditer(r"[.!?]+[\"')\]]*(?=\s)", say)]
        if done:
            ends.append(len(say))
        for end in ends:
            if end > self.sent:
                piece = say[self.sent:end].strip()
                self.sent = end
                piece = _no_laughs(piece) if piece and not (not LAUGH and LAUGH_RE.match(piece)) else ""
                if piece and self.count < MAX_SENTENCES:   # (the rest is cut, so replies stay short)
                    self.count += 1
                    self.on_sentence(piece)


def _cap(text):
    """At most MAX_SENTENCES sentences (the same cut the streamer makes)."""
    if not text:
        return text
    ends = [m.end() for m in re.finditer(r"[.!?]+[\"')\]]*(?=\s)", text)]
    return text[:ends[MAX_SENTENCES - 1]].strip() if len(ends) >= MAX_SENTENCES else text


def note_said(sid, text):
    """The remote control made him say something mid-visit; his next reply should know."""
    with _lock:
        session = _sessions.get(sid)
        if session is not None:
            session.setdefault("puppet", []).append(text)


def chat(sid, text, on_stage=None, cut_off=None, on_say=None, on_mood=None, image_b64=None, people=0, last=False):
    """on_stage: name of the creature currently visible in the window, if any. cut_off: they talked over
    your last reply; this is the part of it you got to say."""
    with _lock:
        session = _sessions.get(sid)
    if session is None:
        session = {"history": [{"role": "user", "content": "(A trick-or-treater walked up.)"},
                               {"role": "assistant", "content": json.dumps({"say": "Happy Halloween!"})}],
                   "touched": time.time()}
        with _lock:
            _sessions[sid] = session
    note = f" ({on_stage} is visible right now.)" if on_stage else ""
    if session.get("song"):
        title = tunes.catalog().get(session["song"], {}).get("title", session["song"])
        note += (f" (Game: the song you played this round is {session['song']}: {title}. If this is a guess, judge it"
                 " against that song; that's the only song for this visit.)")
    if session.get("puppet"):
        with _lock:
            lines, session["puppet"] = session["puppet"], []
        note += " (Since your last reply you also said: " + " ".join(f'"{t}"' for t in lines) + ")"
    if cut_off is not None:
        said = f'you had only said "{cut_off}"' if cut_off else "before you got a word out"
        note += f" (They talked over your last reply, {said}. Answer what they said; don't repeat yourself or apologise.)"
    if last:
        note += (" (This is your last reply of the visit: answer, then say a warm goodbye and remind them to take"
                 " candy. Don't ask a question or promise anything more.)")
    if text is None:   # no words: the fresh look he asked for
        if people:
            said = (f"(You take a fresh look: {people} {'person' if people == 1 else 'people'} in view. React to "
                    "what you see now: compliment the costumes you can make out; never describe darkness, blur or "
                    "shapes.)" + note)
        else:
            said = ("(You take a fresh look and still can't see anyone. Don't mention seeing, looking or your eyes "
                    "again this visit, and don't set look again: ask what they're dressed as and keep chatting.)"
                    + note)
    else:
        said = f'The trick-or-treater says: "{text}"{note}'
    history = session["history"] + [{"role": "user", "content": said}]
    request = history
    if image_b64:   # the picture goes with this one request; the history keeps the words
        request = history[:-1] + [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
            {"type": "text", "text": said}]}]
    if FAKE:
        data = {"say": random.choice(FAKE_ANSWERS)}
        if on_stage:
            data["creature_says"] = "Ooooh, I agree with the pumpkin!"
    else:
        data = _parse(call_claude(request, schema=output_schema(False),
                                  on_text=_SayStreamer(on_say, on_mood) if on_say else None))
    song = data.get("play_song") if data.get("play_song") in tunes.catalog() else ""
    if song and session.get("song") and (song != session["song"] or "again" not in (text or "").lower()):
        song = ""   # one song per visit; replay it only if they ask to hear it again
    if song:
        session["song"] = song
    result = {"mood": data.get("mood") if data.get("mood") in MOODS else "happy", "play_song": song,
              "look": bool(data.get("look")), "say": _cap(_no_laughs(data.get("say"))) or "Ooh, let's talk about something else. Like candy! Have you picked one yet?",
              **_sideshow_fields(data)}
    with _lock:
        session["history"] = history + [{"role": "assistant", "content": json.dumps(result)}]
        session["touched"] = time.time()
    return result
