// Talking pumpkin display: draws the face from state pushed by the brain (main.py) and offers a
// debug panel / remote control. Camera, speech and Claude all run in the brain, not in the browser.
import { PumpkinFace } from "./face.js";
import { AlignOverlay, fitElement } from "./warp.js";

const $ = (id) => document.getElementById(id);
const face = new PumpkinFace($("face"));
const logEl = $("log"), stateEl = $("state");
// A phone opened with ?debug is a remote control: it doesn't show the projector's alignment.
const REMOTE = new URLSearchParams(location.search).has("debug");
const overlay = new AlignOverlay();
let state = "connecting", lastFace = 0;

function log(text, cls = "info") {
  const div = document.createElement("div");
  div.className = cls;
  div.textContent = text;
  logEl.appendChild(div);
  while (logEl.childNodes.length > 200) logEl.removeChild(logEl.firstChild);
  logEl.scrollTop = logEl.scrollHeight;
}

// ---- Idle video (e.g. AtmosFX singing pumpkins) while he waits; the live face takes over for a visit ----
const idleEl = $("idle");
let idleOn = false, idleTimer = null, faceHidden = false, paused = false;   // faceHidden: skip drawing (the Pi's CPU goes to the video)
function idleVideo(ev) {
  const fade = `${ev.fade ?? 1}s`;
  idleEl.style.transition = $("face").style.transition = `opacity ${fade}`;
  clearTimeout(idleTimer);
  if (!ev.on) {
    idleOn = false;
    faceHidden = false;
    idleEl.style.opacity = 0;
    $("face").style.opacity = 1;
    idleTimer = setTimeout(() => { if (!idleOn) idleEl.pause(); }, (ev.fade ?? 1) * 1000 + 100);
    return;
  }
  idleOn = true;
  const src = `idle/video?clip=${ev.clip ?? 0}`;
  if (idleEl.getAttribute("src") !== src) { idleEl.setAttribute("src", src); idleEl.load(); }
  const start = () => {
    if (ev.start) {            // the brain plays the soundtrack: line the picture up with it
      idleEl.loop = false;
      const at = Date.now() / 1000 - ev.start;
      if (at < 0) { idleTimer = setTimeout(start, -at * 1000); return; }
      idleEl.currentTime = at;
    } else {
      idleEl.loop = true;      // no soundtrack: just loop the picture
    }
    if (!paused) idleEl.play().catch(() => {});
    idleEl.style.opacity = 1;
    $("face").style.opacity = 0;
    setTimeout(() => { if (idleOn) faceHidden = true; }, (ev.fade ?? 1) * 1000 + 100);
  };
  if (idleEl.readyState >= 1) start(); else idleEl.addEventListener("loadedmetadata", start, { once: true });
}

// ---- Live state from the brain ----
function connect() {
  const es = new EventSource("events");
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.t === "face") {
      lastFace = performance.now();
      face.awake = ev.awake;
      face.speaking = ev.speaking;
      face.mouthLevel = ev.mouth;
      if (ev.gaze) face.gazeTarget = { x: ev.gaze[0], y: ev.gaze[1] };
      else face.gazeTarget = { x: face.gazeTarget.x * 0.97, y: face.gazeTarget.y * 0.97 };
    } else if (ev.t === "calib") {
      Object.assign(face.calib, ev.calib);
    } else if (ev.t === "guide") {
      face.showGuide = ev.on;
    } else if (ev.t === "idle" && !REMOTE) {
      idleVideo(ev);
    } else if (ev.t === "pause") {
      paused = ev.on;
      if (idleOn) ev.on ? idleEl.pause() : (idleEl.loop && idleEl.play().catch(() => {}));   // a synced clip restarts from its own "idle" event
      const b = $("pauseBtn");
      if (b) b.textContent = ev.on ? "▶ Resume" : "⏸ Pause";
    } else if (ev.t === "scare") {
      face.scare(ev.dark_ms, ev.ms);
    } else if (ev.t === "surprise") {
      face.surprise();
    } else if (ev.t === "laugh") {
      face.laugh(ev.ms);
    } else if (ev.t === "show") {
      face.setVisible(ev.on);
    } else if (ev.t === "face_warp" && !REMOTE) {
      fitElement($("face"), ev.warp);
      face.resize();
    } else if (ev.t === "align_pattern" && ev.screen === "face" && !REMOTE) {
      ev.on ? overlay.pattern(ev.markers) : overlay.hide();
    } else if (ev.t === "align_outline" && ev.screen === "face" && !REMOTE) {
      overlay.outline(ev.quad);
    } else if (ev.t === "state") {
      state = ev.state;
      face.setState(ev.state);
    } else if (ev.t === "mood") {
      face.setMood(ev.name);
    } else if (ev.t === "expr") {
      face.setAccent(ev.name, ev.ms);
    } else if (ev.t === "log") {
      log(ev.text, ev.cls);
    }
  };
  es.onerror = () => { state = "reconnecting to the brain…"; };
}
connect();

// Status line in the debug panel
setInterval(async () => {
  if (!$("debug").classList.contains("on")) return;
  try {
    const s = await (await fetch("api/status")).json();
    let line = `state: ${s.state} · people: ${s.people} · detect ${s.detect_ms} ms`;
    if (s.doa_raw !== undefined && s.doa_raw !== null) line += ` · voice ${s.doa_raw}°` + (s.doa_bearing !== null ? ` (${Math.round(s.doa_bearing)}° from center)` : "");
    if (s.creature) line += ` · ${s.creature} in ${s.creature_window}`;
    $("windowRow").hidden = !(s.windows && s.windows.length);   // side show add-on is off
    if (s.windows && s.windows.length) {
      line += ` · windows on: ${s.windows_on.join(", ") || "none"}`;
      fillWindows(s.windows, s.windows_on);
    }
    if (s.volume !== undefined && !volDragging) { $("vol").value = s.volume; $("volVal").textContent = `${s.volume}%`; }
    if (s.wake_bar !== undefined && !wakeDragging) {
      $("wake").value = s.wake_bar;
      $("wakeVal").textContent = s.wake_bar ? `${s.wake_bar}` : `auto (${s.wake_auto})`;
    }
    if ($("hearBtn")) $("hearBtn").textContent = s.hearing_test ? "🎤 Listening… say trick or treat" : "🎤 Hearing test";
    if (s.mic_level !== undefined) {
      // a log scale, so a whisper and a shout both fit: 10 .. 5000
      const pos = (v) => Math.max(0, Math.min(100, (Math.log10(Math.max(v, 10)) - 1) / (Math.log10(5000) - 1) * 100));
      $("meterFill").style.width = `${pos(s.mic_level)}%`;
      const loud = s.mic_need && s.mic_level > s.mic_need;
      $("meterFill").style.background = loud ? (s.voice_light ? "#4c4" : "#ca3") : "#468";
      $("meterNeed").style.display = s.mic_need ? "block" : "none";
      if (s.mic_need) $("meterNeed").style.left = `${pos(s.mic_need)}%`;
      $("meterVal").textContent = s.mic_need ? `${s.mic_level} / ${s.mic_need}` : `${s.mic_level}`;
      $("voiceDot").style.background = s.voice_light ? "#4c4" : "#333";
    }
    if (s.mic_muted) line += " · MIC MUTED";
    if (s.paused) line += " · PAUSED";
    if (s.paused !== undefined && $("pauseBtn")) $("pauseBtn").textContent = s.paused ? "▶ Resume" : "⏸ Pause";
    if (s.fake) line += " · FAKE MODE";
    if (s.version) line += ` · version ${s.version}`;
    stateEl.textContent = line;
  } catch { stateEl.textContent = state; }
}, 250);

// ---- Render loop ----
let lastFrame = performance.now();
function frame(now) {
  const dt = Math.min(0.1, (now - lastFrame) / 1000);
  lastFrame = now;
  if (now - lastFace > 2000) { face.awake = false; face.speaking = false; face.mouthLevel = null; }
  if (!faceHidden) {
    face.update(now, dt);
    face.draw(now);
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ---- Controls ----
const post = (cmd, body = {}) => fetch(`api/${cmd}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// Camera view: fetch one frame at a time (only while the panel is open). Each frame asks for the next, and a
// timer restarts the chain if a frame ever gets lost, so the picture can't freeze for good.
let videoTimer = null, videoLast = 0;
function nextFrame() {
  clearTimeout(videoTimer);
  if (!$("debug").classList.contains("on")) return;
  videoLast = Date.now();
  $("video").src = `frame.jpg?t=${videoLast}`;
  videoTimer = setTimeout(nextFrame, 3000);   // watchdog: nothing came back in 3 s, ask again
}
$("video").addEventListener("load", () => { clearTimeout(videoTimer); videoTimer = setTimeout(nextFrame, 120); });
$("video").addEventListener("error", () => { clearTimeout(videoTimer); videoTimer = setTimeout(nextFrame, 1000); });

function toggleDebug() {
  const on = $("debug").classList.toggle("on");
  if (on) nextFrame(); else { clearTimeout(videoTimer); $("video").removeAttribute("src"); }
}

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  if (face.adjust(e.key)) return post("calib", { calib: face.calib });
  switch (e.key.toLowerCase()) {
    case "d": toggleDebug(); break;
    case "h": $("help").classList.toggle("on"); break;
    case "g": post("greet"); break;
    case "escape": post("end"); break;
    case "f": post("flip"); break;
    case "m": post("mute"); break;
    case "p": post("pause"); break;
    case "c": post("guide"); break;
    case "enter": document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen(); break;
  }
});

// Phone-friendly buttons in the debug panel (tap D-panel buttons instead of keys).
document.querySelectorAll("[data-cmd]").forEach((b) => b.addEventListener("click", () => {
  const cmd = b.dataset.cmd;
  post(cmd).then((r) => r.json()).then((j) => { if (j && j.error) log(j.error, "error"); }).catch(() => {});
}));

// Hearing threshold slider (0 = automatic); the hearing test sets it too.
let wakeDragging = false;
$("wake").addEventListener("input", (e) => { wakeDragging = true; $("wakeVal").textContent = e.target.value > 0 ? e.target.value : "auto"; });
$("wake").addEventListener("change", (e) => { wakeDragging = false; post("wake_bar", { value: Number(e.target.value) }); });

// Volume slider: sends the new level when you let go (the label follows while dragging).
let volDragging = false;
$("vol").addEventListener("input", (e) => { volDragging = true; $("volVal").textContent = `${e.target.value}%`; });
$("vol").addEventListener("change", (e) => { volDragging = false; post("volume", { percent: Number(e.target.value) }); });

// Nudge buttons, so the face can be lined up from a phone.
document.querySelectorAll("[data-key]").forEach((b) => b.addEventListener("click", () => {
  if (face.adjust(b.dataset.key)) post("calib", { calib: face.calib });
}));

// Side-show windows: pick one, then align it (stored by the brain) or summon a test creature into it.
const wsel = $("wscreen");
function fillWindows(all, on) {
  const want = all.map((n) => `${n}${on.includes(n) ? "" : " (off)"}`).join("|");
  if (wsel.dataset.list === want) return;
  const keep = wsel.value;
  wsel.dataset.list = want;
  wsel.innerHTML = all.map((n) => `<option value="${n}">${n}${on.includes(n) ? "" : " (off)"}</option>`).join("");
  wsel.value = all.includes(keep) ? keep : (on[0] || all[0]);
  const at = $("atarget"), akeep = at.value;
  at.innerHTML = `<option value="face">Align: face</option>` + all.map((n) => `<option value="${n}">Align: ${n} window</option>`).join("");
  at.value = ["face", ...all].includes(akeep) ? akeep : "face";
}

// ---- Auto-align a projector: show a pattern, photograph it from where the kids stand, let the Pi fit it ----
const aMsg = $("aMsg"), aImg = $("aImg"), aWrap = $("aWrap");
let handles = [];
async function align(cmd, body = {}) {
  aMsg.textContent = cmd === "align_photo" ? "Working on it…" : aMsg.textContent;
  try {
    const r = await (await post(cmd, { target: $("atarget").value, ...body })).json();
    aMsg.textContent = r.error || r.msg || "";
    if (r.preview) showPreview(r.preview, r.corners);
    return r;
  } catch (e) {
    aMsg.textContent = `Couldn't reach the pumpkin (${e.message}).`;
  }
}
function showPreview(src, corners) {
  aImg.src = src;
  $("aPrev").hidden = false;
  handles.forEach((h) => h.remove());
  handles = corners.map(([x, y]) => {
    const h = document.createElement("div");
    h.className = "handle";
    h.style.left = `${x * 100}%`; h.style.top = `${y * 100}%`;
    h.addEventListener("pointerdown", (e) => {
      h.setPointerCapture(e.pointerId);
      const move = (ev) => {
        const r = aWrap.getBoundingClientRect();
        h.style.left = `${Math.min(100, Math.max(0, (ev.clientX - r.left) / r.width * 100))}%`;
        h.style.top = `${Math.min(100, Math.max(0, (ev.clientY - r.top) / r.height * 100))}%`;
      };
      h.addEventListener("pointermove", move);
      h.addEventListener("pointerup", () => h.removeEventListener("pointermove", move), { once: true });
    });
    aWrap.appendChild(h);
    return h;
  });
}
async function shrink(file) {
  // phone photos are large; 1600 px is plenty to find the markers
  const bmp = await createImageBitmap(file);
  const k = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
  const c = document.createElement("canvas");
  c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
  c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.88);
}
$("aStart").addEventListener("click", () => { $("aPrev").hidden = true; align("align_start"); });
$("aPhoto").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (file) align("align_photo", { image: await shrink(file) });
});
$("aUse").addEventListener("click", () => align("align_corners", {
  corners: handles.map((h) => [parseFloat(h.style.left) / 100, parseFloat(h.style.top) / 100]) }));
$("aDone").addEventListener("click", () => { $("aPrev").hidden = true; align("align_done"); });
$("aUndo").addEventListener("click", () => { $("aPrev").hidden = true; align("align_undo"); });
document.querySelectorAll("[data-wkey]").forEach((b) => b.addEventListener("click", () => post("window_adjust", { key: b.dataset.wkey, screen: wsel.value })));
document.querySelectorAll("[data-wcmd]").forEach((b) => b.addEventListener("click", () => post(b.dataset.wcmd, { screen: wsel.value })));

$("ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("askText").value.trim();
  $("askText").value = "";
  if (text) post("ask", { text });
});

// Tap/click the top-left corner to open the debug panel on a touch screen or phone.
$("corner").addEventListener("click", toggleDebug);
if (new URLSearchParams(location.search).has("debug")) toggleDebug();

let idleCursor;
document.addEventListener("mousemove", () => {
  document.body.classList.remove("hide-cursor");
  clearTimeout(idleCursor);
  idleCursor = setTimeout(() => document.body.classList.add("hide-cursor"), 2000);
});
document.body.classList.add("hide-cursor");
