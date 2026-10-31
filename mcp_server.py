"""Remote control for the pumpkin as an MCP server, so an AI assistant (Claude Desktop, Claude Code, ...) can
puppet him, run the show and check on him. His own conversations with kids don't go through here; they stay
in main.py's fast loop. This just talks to the pumpkin's local web API.

Run it on the Pi (it reaches the pumpkin on 127.0.0.1). From a laptop, Claude Desktop can start it over SSH:

    "pumpkin": {"command": "ssh", "args": ["you@pumpkin.local", "~/pumpkin/.venv/bin/python",
                                           "~/pumpkin/mcp_server.py"]}

or on the Pi itself:  claude mcp add pumpkin -- ~/pumpkin/.venv/bin/python ~/pumpkin/mcp_server.py
Set PUMPKIN_URL (and PUMPKIN_KEY from .env) to run it on another machine instead.
"""

import json
import os
import urllib.error
import urllib.request

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import config

_cfg = config.load()
BASE = os.environ.get("PUMPKIN_URL", f"http://127.0.0.1:{_cfg['web']['port']}").rstrip("/")
KEY = os.environ.get("PUMPKIN_KEY", "")
MOODS = ["happy", "excited", "surprised", "curious", "thinking", "sly", "sad", "spooky", "proud",
         "silly", "tongue", "crosseyed", "wink", "dizzy", "scared"]

server = MCPServer(
    "pumpkin",
    instructions=(
        "Controls a talking jack-o'-lantern on a porch: a face projected onto a pumpkin, a camera, a mic and a "
        "speaker. He chats with trick-or-treaters on his own; use these tools to step in (make him say something, "
        "pull a face, play a song), run the show (greet, end a visit, volume) or check on him (status, what "
        "kids said, the camera). Whatever he says is heard out loud by whoever is on the porch, often young "
        "children, so keep it friendly and spooky-fun."),
)


def _call(path, body=None, raw=False):
    headers = {"Content-Type": "application/json"}
    if KEY:
        headers["X-Pumpkin-Key"] = KEY
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
    except urllib.error.HTTPError as exc:
        raise ToolError(f"{exc.code}: {exc.read().decode(errors='replace')[:200]}") from exc
    except OSError as exc:
        raise ToolError(f"the pumpkin isn't answering at {BASE} ({exc}). Is it running?") from exc
    if raw:
        return data
    reply = json.loads(data or b"{}")
    if isinstance(reply, dict) and reply.get("error"):
        raise ToolError(reply["error"])
    return reply


@server.tool()
def status() -> dict:
    """What he's doing right now (idle, listening, speaking...), how many people the camera sees, volume,
    whether the mic is muted, and which window creature is showing."""
    return _call("/api/status")


@server.tool()
def say(text: str, mood: str = "") -> dict:
    """Make the pumpkin say something out loud, right now, in his voice (his mouth moves with it).
    Optional mood sets his face first: happy, excited, surprised, curious, thinking, sly, sad, spooky, proud,
    silly, tongue, crosseyed, wink, dizzy, scared. If a visit is going on, his next reply knows he said it."""
    return _call("/api/say", {"text": text, "mood": mood or None})


@server.tool()
def set_mood(mood: str) -> dict:
    """Change his face: happy, excited, surprised, curious, thinking, sly, sad, spooky, proud, or a silly one:
    silly (cross-eyed with a goofy crooked grin), tongue (sticks his tongue out), crosseyed, wink, dizzy (spinning
    eyes), scared."""
    return _call("/api/set_mood", {"mood": mood})


@server.tool()
def list_songs() -> dict:
    """The songs he can play for the guess-the-song game, by id."""
    return _call("/api/songs", {})


@server.tool()
def play_song(song_id: str) -> dict:
    """Play a song clip (an id from list_songs). Kids can shout the answer over it."""
    return _call("/api/play_song", {"song_id": song_id})


@server.tool()
def greet() -> dict:
    """Start a visit now, as if someone walked up: he looks, comments on their costumes and starts chatting."""
    return _call("/api/greet", {})


@server.tool()
def end_visit() -> dict:
    """Wrap up the current visit."""
    return _call("/api/end", {})


@server.tool()
def ask(text: str) -> dict:
    """Type something as if a visitor said it out loud; during a visit he answers it in his own words."""
    return _call("/api/ask", {"text": text})


@server.tool()
def set_volume(percent: int) -> dict:
    """Speaker volume, 0-150 (100 is normal)."""
    return _call("/api/volume", {"percent": percent})


@server.tool()
def pause() -> dict:
    """Pause everything (his voice mid-word, the songs, the idle video, listening, visit timers), or resume
    from the same spot if he's already paused."""
    _call("/api/pause", {})
    return {"paused": _call("/api/status").get("paused")}


@server.tool()
def toggle_mic() -> dict:
    """Mute his hearing, or unmute it if it's muted."""
    _call("/api/mute", {})
    return {"mic_muted": _call("/api/status").get("mic_muted")}


@server.tool()
def recent_conversation(lines: int = 40) -> dict:
    """What visitors said ("heard") and what he said back ("said"), newest last, with times."""
    return _call("/api/recent", {"lines": lines, "kinds": ["heard", "said"]})


@server.tool()
def recent_log(lines: int = 80) -> dict:
    """His full recent log: visits, timings (lines with ⏱), errors, settings at startup."""
    return _call("/api/recent", {"lines": lines})


@server.tool()
def look() -> Image:
    """What his camera sees right now, with the people he's detected boxed."""
    return Image(data=_call("/frame.jpg", raw=True), format="jpeg")


@server.tool()
def ghost_tour() -> dict:
    """Send the ghost flying from window to window (needs the window projectors on)."""
    return _call("/api/ghost_tour", {})


@server.tool()
def jump_scare(delay_s: float = 0, gotcha: bool = True) -> dict:
    """BOO! His face goes dark for a moment, then a huge glowing red monster face lunges out with a loud roar
    (and a flash of the porch lights and fog, if wired up). Then he giggles "Gotcha!" so little kids aren't
    left scared (gotcha=False skips that). delay_s (0-10) arms it to go off a few seconds later, e.g. once a
    kid leans in. Best for older kids and grown-ups; check with look first if you're not sure who's there."""
    return _call("/api/jump_scare", {"delay_s": delay_s, "gotcha": gotcha})


@server.tool()
def echo_test() -> dict:
    """Check the speaker and the mic array's echo cancellation (about 25 s; plays a test sentence out loud).
    Read the result with recent_log afterwards: the lines starting with 🔊."""
    return _call("/api/echo_test", {})


if __name__ == "__main__":
    server.run()
