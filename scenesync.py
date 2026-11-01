"""SceneSync (github.com/rkedward/scene-sync): the window projectors around the house. During a show the pumpkin
blacks out one SceneSync group so its clips don't distract, can play a clip in one of its windows, and hands
every projector back to its own schedule when the group leaves.

Uses the SceneSync dashboard API with the dashboard password. If SceneSync can't be reached, the show goes on
and the windows simply keep playing.
"""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

import config

STATE = config.ROOT / "scenesync_state.json"   # overrides this pumpkin issued, so a restart can undo them
REFRESH_S = 30


class SceneSync:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.url = str(cfg["url"]).rstrip("/")
        password = str(cfg.get("password") or os.environ.get("SCENESYNC_PASSWORD", ""))
        self._auth = "Basic " + base64.b64encode(f"pumpkin:{password}".encode()).decode()
        self.devices, self.media, self.group_id = {}, {}, None   # name -> device dict, file name -> media dict
        self.dark = False            # the group is blacked out for a show
        self._ours = set(self._load_state())
        self._lock = threading.Lock()
        self._timers = {}            # device id -> Timer that blacks the window out again after its clip
        self.refresh()
        threading.Thread(target=self._refresh_loop, daemon=True, name="scenesync").start()
        self._undo_leftovers()

    # ---- API -----------------------------------------------------------------------------------
    def _call(self, method, path, body=None, timeout=4):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": self._auth, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        return json.loads(raw) if raw else None

    def refresh(self):
        """Projectors, clips and the show group, by name."""
        try:
            devices = self._call("GET", "/api/v1/admin/devices") or []
            media = self._call("GET", "/api/v1/admin/media") or []
            groups = self._call("GET", "/api/v1/admin/groups") or []
        except urllib.error.HTTPError as exc:
            self.log(f"SceneSync: {exc.code} {'(check the dashboard password)' if exc.code == 401 else exc.reason}")
            return False
        except Exception as exc:
            self.log(f"SceneSync unreachable at {self.url} ({type(exc).__name__}: {exc})")
            return False
        first = not self.devices
        self.devices = {d["name"]: d for d in devices}
        self.media = {m["filename"]: m for m in media}
        want = str(self.cfg["group"]).strip().lower()
        self.group_id = next((g["id"] for g in groups if str(g.get("name", "")).strip().lower() == want), None)
        if first:
            names = ", ".join(self.devices) or "none"
            self.log(f"SceneSync: projectors {names}; show group "
                     + (f"'{self.cfg['group']}' ({len(self.group_devices())})" if self.group_id
                        else f"'{self.cfg['group']}' not found, so no window goes dark"))
            for name, clip in (self.cfg.get("clips") or {}).items():
                if clip.get("media") not in self.media:
                    self.log(f"SceneSync: clip '{name}' needs {clip.get('media')!r} uploaded in the dashboard")
        return True

    def _refresh_loop(self):
        while True:
            time.sleep(REFRESH_S)
            self.refresh()

    def group_devices(self):
        return [d["id"] for d in self.devices.values() if self.group_id and d.get("group_id") == self.group_id]

    def online(self, name):
        d = self.devices.get(name)
        return bool(d and d.get("online", True))

    def _command(self, ids, action, note):
        """action None hands the projectors back to their schedules."""
        if not ids:
            return False
        body = {"device_ids": ids, "action": action, "hold": action is not None}
        try:
            out = self._call("POST", "/api/v1/admin/command", body) or {}
        except Exception as exc:
            self.log(f"SceneSync: {note} failed ({type(exc).__name__}: {exc})")
            return False
        ov = out.get("override") or {}
        with self._lock:
            if action is None:
                self._ours -= set(ids)
            else:
                self._ours |= set(ids)
            self._save_state()
        self.log(f"🪟 SceneSync: {note}" + (f" ({ov.get('override_id')})" if ov else ""))
        return True

    # ---- shows ---------------------------------------------------------------------------------
    def blackout(self):
        """The show starts: the group's projectors go black and stay black until release()."""
        self.dark = self._command(self.group_devices(), {"type": "stop"}, "windows dark for the show")
        return self.dark

    def release(self):
        """The group left: every projector the pumpkin touched goes back to its own schedule."""
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        ids = sorted(set(self.group_devices()) | self._ours)
        self.dark = False
        if ids:
            self._command(ids, None, "windows back to their schedules")

    def play(self, window, media_file):
        """Play one clip in one window. Returns its length in seconds, or None. It loops while held, and is
        stopped just before it would repeat, so the schedule never shows through in between."""
        d, m = self.devices.get(window), self.media.get(media_file)
        if not d or not m:
            self.log(f"SceneSync: can't play {media_file!r} in {window!r} "
                     f"({'no such projector' if not d else 'clip not uploaded'})")
            return None
        if not self._command([d["id"]], {"type": "play", "media_id": m["id"], "loop": True},
                             f"{media_file} in {window}"):
            return None
        length = (m.get("duration_ms") or 10000) / 1000
        old = self._timers.pop(d["id"], None)
        if old:
            old.cancel()
        t = threading.Timer(max(0.5, length - 0.3), self.stop_clip, args=(window,))
        t.daemon = True
        self._timers[d["id"]] = t
        t.start()
        return length

    def stop_clip(self, window):
        """The clip is over: dark again during a show, otherwise back to the schedule."""
        d = self.devices.get(window)
        if not d:
            return
        t = self._timers.pop(d["id"], None)
        if t:
            t.cancel()
        if self.dark:
            self._command([d["id"]], {"type": "stop"}, f"{window} dark again")
        else:
            self._command([d["id"]], None, f"{window} back to its schedule")

    # ---- crash safety --------------------------------------------------------------------------
    # SceneSync keeps a held command until it's cleared, so a pumpkin that crashed mid-show would leave the
    # windows dark all night. The ids of the projectors it holds are saved; at start-up it lets go of them.
    def _load_state(self):
        try:
            return json.loads(STATE.read_text()).get("held", [])
        except Exception:
            return []

    def _save_state(self):
        try:
            STATE.write_text(json.dumps({"held": sorted(self._ours)}))
        except OSError:
            pass

    def _undo_leftovers(self):
        if self._ours:
            self.log("SceneSync: a show didn't finish last time; handing its windows back")
            self._command(sorted(self._ours), None, "windows back to their schedules")


class SceneSyncShow:
    """Window clips through SceneSync for the brain, with the same calls main.py uses for the old window page
    side show (sideshow.SideShow). Clips carry their own sound and can't talk back."""

    fly_gap = 0.0

    def __init__(self, sync, cfg, log):
        self.sync, self.log = sync, log
        self.windows = {}
        for name, w in (cfg.get("windows") or {}).items():
            w = w if isinstance(w, dict) else {}
            self.windows[str(name)] = {"where": str(w.get("where") or f"the {name} window"),
                                       "gaze": [float(v) for v in w.get("gaze", [-1.0, -0.3])], "hdmi": False}
        self.creatures = []
        for name, c in (cfg.get("clips") or {}).items():
            if not isinstance(c, dict) or not c.get("media"):
                continue
            self.creatures.append({"name": str(name), "media": str(c["media"]), "silent": True,
                                   "description": str(c.get("description") or name),
                                   "windows": [str(w) for w in c.get("windows", []) if str(w) in self.windows]})
        self.on_stage, self.screen, self.until = None, None, 0.0
        if self.creatures:
            log(f"SceneSync clips: {', '.join(c['name'] for c in self.creatures)}; windows: {', '.join(self.windows)}")

    def online(self):
        return [w for w in self.windows if self.sync.online(w)]

    def find(self, name):
        return next((c for c in self.creatures if c["name"].lower() == str(name).lower()), None)

    def place(self, creature, window=""):
        on = self.online()
        fits = [w for w in on if not creature.get("windows") or w in creature["windows"]]
        return window if window in fits else (fits[0] if fits else None)

    def summon(self, name, window=""):
        c = self.find(name)
        where = self.place(c, window) if c else None
        if not where:
            return False
        length = self.sync.play(where, c["media"])
        if length is None:
            return False
        self.on_stage, self.screen, self.until = c, where, time.time() + length
        return True

    def active(self):
        if self.on_stage and time.time() >= self.until:
            self.on_stage = None
        return self.on_stage

    def speak(self, text, should_stop=None):
        """A clip can't talk back: its own soundtrack is all it says."""

    def fly_to(self, window, on_leave=None):
        return False

    def dismiss(self):
        if self.on_stage and self.screen:
            self.sync.stop_clip(self.screen)
        self.on_stage = None

    def tour_order(self):
        return []

    def checked_in(self, screen):
        return screen in self.windows

    def clip_finished(self, screen):
        pass
