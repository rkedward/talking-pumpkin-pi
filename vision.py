"""Camera capture, person detection and tracking.

Works with a Raspberry Pi camera (Picamera2), a USB webcam (OpenCV) or a looping video file (for testing).
Detection uses EfficientDet-Lite0 (TFLite, with built-in box decoding) and keeps only "person" boxes, so
masked and fully costumed kids are found even when their faces are hidden.
"""

import base64
import sys
import os
import threading
import time

import cv2
import numpy as np

from config import resolve


def _interpreter_class():
    try:
        from ai_edge_litert.interpreter import Interpreter
        return Interpreter
    except ImportError:
        pass
    try:
        from tflite_runtime.interpreter import Interpreter
        return Interpreter
    except ImportError:
        pass
    from tensorflow.lite import Interpreter  # type: ignore
    return Interpreter


class Camera:
    """Grabs frames on a background thread; read() returns the latest BGR frame (or None)."""

    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.frame = None
        self.frame_id = 0
        self.frame_time = 0.0
        self._lock = threading.Lock()
        self._open()
        threading.Thread(target=self._loop, daemon=True, name="camera").start()
        threading.Thread(target=self._watchdog, daemon=True, name="camera-watchdog").start()

    def _open(self):
        src = str(self.cfg["source"])
        self.picam = self.cap = None
        self.file_fps = None
        if src in ("auto", "picamera"):
            try:
                from picamera2 import Picamera2
                self.picam = Picamera2()
                size = (int(self.cfg["width"]), int(self.cfg["height"]))
                # "RGB888" in libcamera terms is BGR byte order, which is what OpenCV expects.
                self.picam.configure(self.picam.create_video_configuration(main={"size": size, "format": "RGB888"}))
                self.picam.start()
                self.log(f"camera: Raspberry Pi camera {size[0]}x{size[1]}")
                return
            except Exception as exc:
                self.picam = None
                if src == "picamera":
                    raise
                self.log(f"camera: no Pi camera ({type(exc).__name__}), trying USB")
        if src.startswith("file:"):
            self.cap = cv2.VideoCapture(src[5:])
            self.file_fps = self.cap.get(cv2.CAP_PROP_FPS) or 15
        else:
            # DirectShow opens webcams much faster than Windows' default backend
            api = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
            self.cap = cv2.VideoCapture(int(self.cfg["usb_index"]), api)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.cfg["width"]))
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.cfg["height"]))
        if not self.cap.isOpened():
            self.log(f"camera: could not open {src}; press G to greet manually")
            self.cap = None
        else:
            src = "webcam" if src == "auto" else src
            self.log(f"camera: {src} {int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")

    def _loop(self):
        while True:
            try:
                self._grab()
            except Exception as exc:   # never let the camera thread die silently
                self.log(f"camera error: {type(exc).__name__}: {exc}")
                time.sleep(1)

    def _grab(self):
        frame = None
        if self.picam is not None:
            frame = self.picam.capture_array()
        elif self.cap is not None:
            ok, frame = self.cap.read()
            if not ok and self.file_fps:  # loop test videos
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                return
            if self.file_fps:
                time.sleep(1 / self.file_fps)
        else:
            time.sleep(0.5)
            return
        if frame is None:
            return
        if frame.ndim == 3 and frame.shape[2] == 4:
            frame = frame[:, :, :3]
        if self.cfg["flip_horizontal"]:
            frame = cv2.flip(frame, 1)
        if self.cfg["flip_vertical"]:
            frame = cv2.flip(frame, 0)
        with self._lock:
            self.frame = frame
            self.frame_id += 1
            self.frame_time = time.time()

    def _watchdog(self):
        """If the Pi camera stops delivering frames, restart it."""
        while True:
            time.sleep(2)
            if self.picam is None or not self.frame_time or time.time() - self.frame_time < 6:
                continue
            self.log("camera: no frames for 6 s, restarting the camera")
            try:
                self.picam.stop()
                self.picam.start()
            except Exception as exc:
                self.log(f"camera: restart failed ({type(exc).__name__}: {exc}); reopening")
                try:
                    self.picam.close()
                except Exception:
                    pass
                try:
                    self._open()
                except Exception as exc2:
                    self.log(f"camera: reopen failed ({exc2})")
            self.frame_time = time.time()

    def read(self):
        with self._lock:
            return self.frame, self.frame_id


class PersonDetector:
    def __init__(self, cfg):
        Interpreter = _interpreter_class()
        self.it = Interpreter(model_path=str(resolve(cfg["model"])), num_threads=int(cfg["threads"]))
        self.it.allocate_tensors()
        inp = self.it.get_input_details()[0]
        self.input_index = inp["index"]
        self.input_size = (int(inp["shape"][2]), int(inp["shape"][1]))
        self.input_dtype = inp["dtype"]
        self.min_score = float(cfg["min_score"])
        self.min_height = float(cfg["min_height"])
        # Outputs of the TFLite detection post-process op: boxes [1,N,4], classes [1,N], scores [1,N], count [1].
        outs = self.it.get_output_details()
        self.boxes_i = next(o["index"] for o in outs if len(o["shape"]) == 3)
        self.count_i = next(o["index"] for o in outs if len(o["shape"]) == 1)
        pair = sorted((o for o in outs if len(o["shape"]) == 2), key=lambda o: o["name"])
        self.pair = [o["index"] for o in pair]
        self.classes_i = self.scores_i = None

    def detect(self, frame):
        rgb = cv2.cvtColor(cv2.resize(frame, self.input_size), cv2.COLOR_BGR2RGB)
        x = rgb[None].astype(self.input_dtype) if self.input_dtype == np.uint8 else (rgb[None].astype(np.float32) - 127.5) / 127.5
        self.it.set_tensor(self.input_index, x)
        self.it.invoke()
        boxes = self.it.get_tensor(self.boxes_i)[0]
        count = int(self.it.get_tensor(self.count_i)[0])
        if self.classes_i is None:
            # Tell classes from scores once: class ids are whole numbers, scores are fractions.
            a, b = (self.it.get_tensor(i)[0][:count] for i in self.pair)
            a_is_classes = np.allclose(a, np.round(a)) and not np.allclose(b, np.round(b))
            self.classes_i, self.scores_i = (self.pair if a_is_classes or count == 0 else self.pair[::-1])
        classes = self.it.get_tensor(self.classes_i)[0]
        scores = self.it.get_tensor(self.scores_i)[0]
        people = []
        for i in range(count):
            if int(classes[i]) != 0 or scores[i] < self.min_score:  # COCO class 0 = person
                continue
            ymin, xmin, ymax, xmax = np.clip(boxes[i], 0, 1)
            if ymax - ymin < self.min_height:
                continue
            people.append({"x": float(xmin), "y": float(ymin), "w": float(xmax - xmin), "h": float(ymax - ymin),
                           "score": float(scores[i])})
        return people


class Vision:
    """Runs detection on a thread and keeps track of who is there and who to look at."""

    def __init__(self, cam_cfg, det_cfg, log):
        self.log = log
        self.hfov = float(cam_cfg["hfov_deg"])
        self.fps = self.idle_fps = float(det_cfg["fps"])
        self.visit_fps = float(det_cfg.get("visit_fps", self.fps))
        self.nice = int(det_cfg.get("nice", 10))
        self.camera = Camera(cam_cfg, log)
        self._det_cfg = det_cfg
        self.detector = None
        self.people = []
        self.target = None
        self.first_seen = 0.0
        self.last_seen = 0.0
        self.preferred_bearing = None     # set from the mic array: look at the person who is talking
        self.preferred_until = 0.0
        self.last_frame = None
        self.detect_ms = 0.0
        self._lock = threading.Lock()
        self._detect_lock = threading.Lock()   # the detector is not thread-safe (look_now runs it too)
        self.paused = False
        # The detector is made on its own thread, so its worker threads get that thread's lower priority.
        self._ready = threading.Event()
        self._init_error = None
        threading.Thread(target=self._loop, daemon=True, name="vision").start()
        self._ready.wait()
        if self._init_error:
            raise self._init_error

    def pause(self, on):
        """Stop detecting for a moment (speech recognition wants every CPU core). Whoever was there counts
        as still there meanwhile."""
        with self._lock:
            if not on and self.paused and self.people:
                self.last_seen = time.time()
            self.paused = bool(on)

    def bearing(self, person):
        """Horizontal angle of a person from the camera axis, degrees; positive = right side of the image."""
        return (person["x"] + person["w"] / 2 - 0.5) * self.hfov

    def conversation(self, on):
        """During a conversation the voice comes first: detect less often (the eyes still follow)."""
        self.fps = self.visit_fps if on else self.idle_fps

    def set_rates(self, idle_fps, visit_fps):
        """How often to look for people between visits and during one; 0 = not at all (look_now still works)."""
        self.idle_fps, self.visit_fps = float(idle_fps), float(visit_fps)
        self.fps = self.idle_fps

    def look_now(self):
        """Detect people in a fresh camera frame right now (the costume snapshot when tracking is off).
        Returns the people found."""
        frame, _ = self.camera.read()
        if frame is None or self.detector is None:
            return []
        with self._detect_lock:
            people = self.detector.detect(frame)
        now = time.time()
        with self._lock:
            self.last_frame, self.people = frame, people
            if people:
                self.first_seen = self.first_seen if now - self.last_seen < 1.0 else now
                self.last_seen = now
                self.target = self._pick_target(people, now)
        return people

    def _loop(self):
        last_id = -1
        try:   # Linux: per-thread priority; the detector's worker threads started from here inherit it
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), self.nice)
        except Exception:
            pass
        try:
            self.detector = PersonDetector(self._det_cfg)
        except Exception as exc:
            self._init_error = exc
            return
        finally:
            self._ready.set()
        while True:
            if self.paused or self.fps <= 0:
                with self._lock:
                    if self.paused and self.people:   # (a pause is brief; whoever was there still is)
                        self.last_seen = time.time()
                time.sleep(0.02)
                continue
            started = time.time()
            frame, fid = self.camera.read()
            if frame is None or fid == last_id:
                time.sleep(0.02)
                continue
            last_id = fid
            with self._detect_lock:
                people = self.detector.detect(frame)
            self.detect_ms = (time.time() - started) * 1000
            now = time.time()
            with self._lock:
                self.last_frame = frame
                self.people = people
                if people:
                    if not self.last_seen or now - self.last_seen > 1.0:
                        self.first_seen = now
                    self.last_seen = now
                    self.target = self._pick_target(people, now)
                else:
                    self.target = None
            fps = self.fps   # (may drop to 0 at any moment: sound mode)
            time.sleep(max(0.0, 1 / fps - (time.time() - started)) if fps > 0 else 0.2)

    def _pick_target(self, people, now):
        if self.preferred_bearing is not None and now < self.preferred_until:
            # Someone is talking: look at the detected person closest to the voice's direction.
            return min(people, key=lambda p: abs(self.bearing(p) - self.preferred_bearing))
        biggest = max(people, key=lambda p: p["w"] * p["h"])
        prev = self._nearest(people, self.target) if self.target else None
        # Hysteresis: stay on the current person unless someone is clearly closer.
        return prev if prev and prev["w"] * prev["h"] * 1.3 > biggest["w"] * biggest["h"] else biggest

    @staticmethod
    def _nearest(people, t):
        best, best_d = None, 0.15
        for p in people:
            d = np.hypot(p["x"] + p["w"] / 2 - (t["x"] + t["w"] / 2), p["y"] + p["h"] / 2 - (t["y"] + t["h"] / 2))
            if d < best_d:
                best, best_d = p, d
        return best

    def prefer_bearing(self, bearing, hold_s=1.5):
        self.preferred_bearing = bearing
        self.preferred_until = time.time() + hold_s

    def state(self):
        with self._lock:
            return list(self.people), self.target, self.first_seen, self.last_seen

    def snapshot_b64(self):
        """JPEG of everyone in view (cropped to the group, max 1024 px) for Claude, as base64."""
        with self._lock:
            frame, people = self.last_frame, list(self.people)
        if frame is None:
            return ""
        H, W = frame.shape[:2]
        x0, y0, x1, y1 = 0.0, 0.0, 1.0, 1.0
        if people:
            x0 = min(p["x"] for p in people); y0 = min(p["y"] for p in people)
            x1 = max(p["x"] + p["w"] for p in people); y1 = max(p["y"] + p["h"] for p in people)
            mx, my = (x1 - x0) * 0.15, (y1 - y0) * 0.1
            x0, y0, x1, y1 = max(0, x0 - mx), max(0, y0 - my), min(1, x1 + mx), min(1, y1 + my)
        crop = frame[int(y0 * H):int(y1 * H), int(x0 * W):int(x1 * W)]
        scale = min(1.0, 1024 / max(crop.shape[:2]))
        if scale < 1:
            crop = cv2.resize(crop, (int(crop.shape[1] * scale), int(crop.shape[0] * scale)))
        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return base64.b64encode(buf.tobytes()).decode() if ok else ""

    def debug_jpeg(self, doa_bearing=None):
        """Camera view with boxes (orange = who the pumpkin is looking at), mirrored like a selfie view."""
        with self._lock:
            frame, people, target = self.last_frame, list(self.people), self.target
        if self.fps <= 0 or self.paused:   # not detecting: show the live picture (boxes from the last look)
            live, _ = self.camera.read()
            frame = live if live is not None else frame
        if frame is None:
            return None
        img = frame.copy()
        H, W = img.shape[:2]
        for p in people:
            color = (0, 140, 255) if p is target else (255, 170, 60)
            cv2.rectangle(img, (int(p["x"] * W), int(p["y"] * H)), (int((p["x"] + p["w"]) * W), int((p["y"] + p["h"]) * H)), color, 4)
        if doa_bearing is not None and abs(doa_bearing) <= self.hfov / 2:
            x = int((doa_bearing / self.hfov + 0.5) * W)
            cv2.line(img, (x, 0), (x, H), (80, 220, 80), 3)
        img = cv2.flip(img, 1)
        scale = 480 / max(H, W) if max(H, W) > 480 else 1
        if scale < 1:
            img = cv2.resize(img, (int(W * scale), int(H * scale)))
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return buf.tobytes() if ok else None
