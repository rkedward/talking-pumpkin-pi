// Animated jack-o'-lantern face, drawn on a canvas for projection onto a pumpkin.
// Black pixels = no projector light, so only the glowing eyes, nose and mouth land on the pumpkin.

const TAU = Math.PI * 2;
const lerp = (a, b, t) => a + (b - a) * t;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// Expressions as targets for the face's "muscles". The face eases from one to the next, so changes look natural.
//  brow: brows up (+) / down (-)       browTilt: inner ends up (+, worried/sad) / down (-, sly/cross)
//  browAsym: one brow up, one down     eye: eye size        cheek: cheeks push the eyes into happy crescents
//  lidTilt: angled upper lids (sly)    lid: extra droop     smile: mouth corners up (+) / down (-)
//  round: "O" mouth                    smirk: lopsided mouth  pupil: pupil size
//  look: [x, y] gaze offset            tilt: head tilt (radians)  lean: head scale (leaning in)
//  Silly ones: tongue: sticks out and waggles   cross: cross-eyed   wink: one eye shut
//  spin: pupils whirl (dizzy)   wobble: head wobbles side to side   shake: shivers   pucker: small kissy lips
const BASE = { brow: 0, browTilt: 0, browAsym: 0, eye: 1, cheek: 0, lidTilt: 0, lid: 0, smile: 0.35, round: 0,
               smirk: 0, pupil: 1, lookX: 0, lookY: 0, tilt: 0, lean: 1,
               tongue: 0, cross: 0, wink: 0, spin: 0, wobble: 0, shake: 0, pucker: 0 };
export const EXPRESSIONS = {
  neutral: {},
  happy: { brow: 0.3, cheek: 0.35, smile: 0.85 },
  excited: { brow: 0.85, eye: 1.12, cheek: 0.2, smile: 1, pupil: 1.12, lean: 1.03 },
  laughing: { brow: 0.6, cheek: 1, smile: 1 },
  surprised: { brow: 1, eye: 1.2, smile: 0, round: 1, pupil: 0.72, lean: 1.04 },
  curious: { brow: 0.35, browAsym: 0.8, tilt: 0.12, smile: 0.25, eye: 1.05 },
  thinking: { brow: 0.15, browAsym: 0.55, browTilt: -0.15, lookX: 0.55, lookY: -0.75, smile: 0.1, smirk: 0.7,
              tilt: -0.06 },
  sly: { brow: -0.2, browTilt: -0.8, lid: 0.18, lidTilt: 0.6, smile: 0.6, smirk: 0.8, pupil: 0.85, tilt: 0.05 },
  sad: { brow: 0.1, browTilt: 0.85, eye: 0.95, lid: 0.22, smile: -0.7, lookY: 0.35, tilt: -0.08 },
  spooky: { brow: -0.4, browTilt: -0.9, lid: 0.25, lidTilt: 0.4, smile: 0.45, pupil: 0.7, lean: 1.05 },
  proud: { brow: 0.45, cheek: 0.25, smile: 0.75, lid: 0.15, tilt: -0.07 },
  listening: { brow: 0.45, eye: 1.05, smile: 0.45, tilt: 0.1, lean: 1.03 },
  sleepy: { brow: -0.4, smile: 0.2 },
  // Silly faces for playing around with the kids
  silly: { brow: 0.5, browAsym: 0.9, cheek: 0.25, smile: 1, smirk: 0.8, round: 0.2, cross: 1, wobble: 0.8,
           tilt: 0.08 },   // cross-eyed with a big crooked goofy grin, one brow way up
  tongue: { brow: 0.45, smile: 0.55, tongue: 1, lid: 0.2, wobble: 0.35 },
  crosseyed: { brow: 0.55, cross: 1, smile: 0.5, smirk: 0.4, tilt: 0.06 },
  wink: { brow: 0.3, browAsym: -0.6, wink: 1, smile: 0.85, smirk: 0.6, cheek: 0.3 },
  dizzy: { brow: 0.2, browTilt: 0.4, spin: 1, smile: 0.1, round: 0.4, wobble: 1 },
  // The jump scare's monster face: huge, glaring, mouth gaping (see scare())
  monster: { brow: -1, browTilt: -1.1, eye: 1.4, pupil: 0.3, lidTilt: 0.75, lid: 0.12, smile: 0.1, round: 0.35,
             shake: 1.4, lean: 1.5 },
  scared: { brow: 0.9, browTilt: 0.9, eye: 1.15, pupil: 0.55, smile: -0.4, round: 0.5, shake: 1, lean: 0.96 },
};
// Silly faces are a quick flash to get a reaction (FLASH_MS), then he goes back to a normal happy face.
const SILLY = new Set(["silly", "tongue", "crosseyed", "wink", "dizzy", "scared"]);
const FLASH_MS = 1500;
EXPRESSIONS.quizzical = EXPRESSIONS.curious;

export class PumpkinFace {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    // Inputs (set from outside)
    this.gazeTarget = { x: 0, y: 0 }; // -1..1, in the face's screen space
    this.speaking = false;
    this.mouthLevel = null; // 0..1 from the real audio loudness; null = animate procedurally while speaking
    this.awake = false; // eyes half closed while nobody is around
    // Animated state
    this.gaze = { x: 0, y: 0 };
    this.mouth = 0;
    this.mouthKick = 0;
    this.lid = 0.55; // 0 = wide open, 1 = closed
    this.blinkUntil = 0;
    this.nextBlink = performance.now() + 2000;
    this.surpriseUntil = 0;
    this.laughUntil = 0;
    this.visible = true; // false = the pumpkin is dark (hide_when_idle); fades in when it wakes
    this.vis = 1;
    this.squint = 0; // 0..1, cheeks pushing the eyes into happy crescents while laughing
    this.wander = { x: 0, y: 0, next: 0 };
    this.ex = { ...BASE };           // current blend of all expression "muscles"
    this.mood = "neutral";           // the feeling of the whole reply (from the brain)
    this.accent = null;              // a short expression for one sentence: { name, until }
    this.state = "idle";             // idle | greeting | listening | hearing | thinking | speaking
    this.browFlash = 0;              // brows pop up on loud, stressed words
    this.nod = 0;                    // 0..1 nod in progress
    this.nextNod = 0;
    this.lastMouth = 0;
    this.calib = defaultCalibration(); // the brain stores it and sends it, so a phone can adjust the projector view
    this.showGuide = false;
    this.resize();
    window.addEventListener("resize", () => this.resize());
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    // the element's own size: full screen, or the auto-aligned box (see warp.js)
    this.canvas.width = Math.round((this.canvas.clientWidth || innerWidth) * dpr);
    this.canvas.height = Math.round((this.canvas.clientHeight || innerHeight) * dpr);
    this.dpr = dpr;
  }

  // Called on each word boundary from speech synthesis.
  kick() { this.mouthKick = 1; }
  surprise(ms = 900) { this.surpriseUntil = performance.now() + ms; }
  // Jump scare: the face goes dark for darkMs, then a huge glaring red monster face lunges out for ms.
  scare(darkMs = 700, ms = 1600) {
    const now = performance.now();
    this.scareDark = [now, now + darkMs];
    this.scareUntil = now + darkMs + ms;
  }
  laugh(ms = 1500) { this.laughUntil = performance.now() + ms; }
  setVisible(on) { this.visible = on; }
  setMood(name) {
    this.mood = EXPRESSIONS[name] ? name : "neutral";
    if (SILLY.has(this.mood)) {
      this.flash = { name: this.mood, until: performance.now() + FLASH_MS };
      this.mood = "happy";
    }
  }
  setAccent(name, ms = 1500) { if (EXPRESSIONS[name]) this.accent = { name, until: performance.now() + ms }; }
  setState(state) {
    if (state !== this.state && (state === "speaking" || state === "listening")) this.nod = 1;   // a little nod
    this.state = state;
  }

  // Which expression to show right now.
  target(now) {
    if (now < (this.scareUntil || 0) && now >= this.scareDark[1]) return "monster";
    if (!this.awake) return "sleepy";
    if (now < this.surpriseUntil) return "surprised";
    if (now < this.laughUntil) return "laughing";
    if (this.flash && now < this.flash.until) return this.flash.name;
    if (this.state === "thinking") return "thinking";
    if (this.state === "listening" || this.state === "hearing") return "listening";
    if (this.accent && now < this.accent.until) return this.accent.name;
    return this.mood;
  }

  adjust(key) {
    const c = this.calib;
    const step = 0.01;
    const moves = {
      ArrowLeft: () => (c.x -= step), ArrowRight: () => (c.x += step),
      ArrowUp: () => (c.y -= step), ArrowDown: () => (c.y += step),
      "+": () => (c.scale *= 1.03), "=": () => (c.scale *= 1.03), "-": () => (c.scale /= 1.03),
      "[": () => (c.rot -= 0.02), "]": () => (c.rot += 0.02),
      "{": () => (c.stretch /= 1.03), "}": () => (c.stretch *= 1.03),
      "0": () => Object.assign(c, defaultCalibration()),
    };
    if (!moves[key]) return false;
    moves[key]();
    return true;
  }

  update(now, dt) {
    // Gaze: follow the target when awake, otherwise drift around lazily.
    let tx = this.gazeTarget.x, ty = this.gazeTarget.y;
    if (!this.awake) {
      if (now > this.wander.next) {
        this.wander = { x: (Math.random() * 2 - 1) * 0.7, y: (Math.random() * 2 - 1) * 0.4,
                        next: now + 1500 + Math.random() * 3000 };
      }
      tx = this.wander.x; ty = this.wander.y;
    }
    // Expression: ease every "muscle" toward the target expression.
    const goal = { ...BASE, ...EXPRESSIONS[this.target(now)] };
    const scaring = now < (this.scareUntil || 0);
    const bam = scaring && now >= this.scareDark[1];
    if (bam && !this.scareHit) { this.scareHit = true; Object.assign(this.ex, goal); }   // no easing: it jumps
    if (!scaring) this.scareHit = false;
    this.red = lerp(this.red || 0, bam ? 1 : 0, 1 - Math.exp(-dt * (bam ? 40 : 3)));
    const e = 1 - Math.exp(-dt * 7);
    for (const k in goal) this.ex[k] = lerp(this.ex[k], goal[k], e);
    // Thinking eyes drift up and aside; others glance only slightly away from the person.
    tx += this.ex.lookX; ty += this.ex.lookY;

    const g = 1 - Math.exp(-dt * (this.awake ? 9 : 2.5));
    this.gaze.x = lerp(this.gaze.x, clamp(tx, -1, 1), g);
    this.gaze.y = lerp(this.gaze.y, clamp(ty, -1, 1), g);

    // Little nods while someone is talking to it, as if to say "uh-huh".
    if (this.state === "hearing" && now > this.nextNod) { this.nod = 1; this.nextNod = now + 1100 + Math.random() * 900; }
    this.nod = Math.max(0, this.nod - dt * 2.2);

    // Blinking
    if (now > this.nextBlink) {
      this.blinkUntil = now + 140;
      this.nextBlink = now + 2500 + Math.random() * 4000;
    }
    const surprised = now < this.surpriseUntil;
    let lidTarget = this.awake ? 0.08 : 0.55;
    if (surprised) lidTarget = -0.15;
    const laughing = now < this.laughUntil;
    if (laughing) lidTarget = 0;
    else if (now < this.blinkUntil) lidTarget = 1;
    this.squint = lerp(this.squint, laughing ? 1 : 0, 1 - Math.exp(-dt * 12));
    const calm = this.scareDark && now >= this.scareDark[0] && now < this.scareDark[1];
    if (calm) this.vis = lerp(this.vis, 0, 1 - Math.exp(-dt * 12));          // lights out...
    else if (scaring) this.vis = 1;                                           // ...BOO
    else this.vis = lerp(this.vis, this.visible ? 1 : 0, 1 - Math.exp(-dt * (this.visible ? 3 : 1.5)));
    this.lid = lerp(this.lid, lidTarget, 1 - Math.exp(-dt * (now < this.blinkUntil ? 40 : 8)));

    // Mouth: follows the audio loudness sent by the brain; otherwise syllable-rate flapping while speaking.
    let mTarget = 0;
    if (this.mouthLevel !== null) {
      mTarget = this.mouthLevel;
    } else if (this.speaking) {
      const syll = Math.abs(Math.sin(now / 1000 * 10.5)) * (0.55 + 0.45 * Math.sin(now / 1000 * 3.1 + 1));
      mTarget = 0.15 + 0.65 * syll + 0.4 * this.mouthKick;
    }
    if (now < this.laughUntil) mTarget = Math.max(mTarget, 0.25);   // keep a big open grin while laughing
    if (bam) mTarget = Math.max(mTarget, 0.9);                       // roaring
    this.mouthKick = Math.max(0, this.mouthKick - dt * 5);
    this.mouth = lerp(this.mouth, clamp(mTarget, 0, 1), 1 - Math.exp(-dt * 30));
    // Stressed, loud words make the brows pop up, like people do when they talk.
    if (this.mouth > 0.75 && this.lastMouth <= 0.75) this.browFlash = 1;
    this.lastMouth = this.mouth;
    this.browFlash = Math.max(0, this.browFlash - dt * 3);
  }

  draw(now) {
    const { ctx, canvas, calib } = this;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    const s = Math.min(canvas.width, canvas.height) / 1000 * calib.scale;
    ctx.translate(canvas.width * (0.5 + calib.x), canvas.height * (0.5 + calib.y));
    ctx.rotate(calib.rot);
    ctx.scale(s, s * calib.stretch);

    if (this.showGuide) this.drawGuide();
    if (this.vis < 0.005) return;   // dark until someone arrives
    ctx.globalAlpha = this.vis;

    // Candle flicker
    const t = now / 1000;
    const flicker = 0.88 + 0.07 * Math.sin(t * 7.3) + 0.05 * Math.sin(t * 13.1 + 2) * Math.sin(t * 3.7);
    const brightness = (this.awake ? 1 : 0.7) * flicker;

    // Laughing: the whole face bounces with each "ho!"
    if (this.squint > 0.01) ctx.translate(0, -Math.abs(Math.sin(t * 13)) * 16 * this.squint);
    // Head motion: a slow, breathing sway, the expression's tilt, a bob with the voice and the odd nod.
    const ex = this.ex;
    const sway = this.awake ? 0.018 * Math.sin(t * 0.7) + 0.008 * Math.sin(t * 1.9 + 1) : 0.01 * Math.sin(t * 0.4);
    const nodY = Math.sin(this.nod * Math.PI) * 22;
    ctx.translate(0, nodY - this.mouth * 10 + 4 * Math.sin(t * 1.3));
    const wobble = ex.wobble * 0.12 * Math.sin(t * 5.5);
    if (ex.shake > 0.01) ctx.translate(ex.shake * 6 * Math.sin(t * 61), ex.shake * 3 * Math.sin(t * 47 + 1));
    ctx.rotate(ex.tilt + sway + wobble);
    const breathe = 1 + 0.008 * Math.sin(t * 1.3);
    ctx.scale(ex.lean * breathe, ex.lean * breathe);

    this.drawBrow(-190, -110, 1, brightness);
    this.drawBrow(190, -110, -1, brightness);
    this.drawEye(-190, -110, 1, brightness);
    this.drawEye(190, -110, -1, brightness);
    this.drawNose(brightness);
    this.drawMouth(brightness);
    ctx.globalAlpha = 1;
  }

  glow(x, y, r, brightness) {
    const grad = this.ctx.createRadialGradient(x, y, r * 0.05, x, y, r);
    const a = clamp(brightness, 0, 1), red = this.red || 0;   // red: the jump scare's glare
    grad.addColorStop(0, `rgba(255, ${Math.round(250 - 120 * red)}, ${Math.round(200 - 170 * red)}, ${a})`);
    grad.addColorStop(0.35, `rgba(255, ${Math.round(196 - 156 * red)}, ${Math.round(40 - 30 * red)}, ${a})`);
    grad.addColorStop(1, `rgba(${Math.round(255 - 55 * red)}, ${Math.round(96 - 86 * red)}, 0, ${a})`);
    return grad;
  }

  // side = 1 for the face's right eye (screen left), -1 for its left eye; shapes mirror.
  drawEye(cx, cy, side, brightness) {
    const ctx = this.ctx;
    const ex = this.ex;
    const k = ex.eye;
    // Rounded triangle, apex tilted toward the nose, classic jack-o'-lantern style.
    const pts = [
      [cx - 105 * k, cy + 62 * k],
      [cx + 105 * k, cy + 62 * k],
      [cx + side * 28 * k, cy - 92 * k],
    ];
    ctx.save();
    ctx.beginPath();
    roundedPolygon(ctx, pts, 26);
    ctx.shadowColor = "rgba(255, 120, 0, 0.9)";
    ctx.shadowBlur = 50;
    ctx.fillStyle = this.glow(cx, cy + 10, 150, brightness);
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.clip();

    // Pupil
    const whirl = t => [Math.cos(t * 9 * side) * 40 * ex.spin, Math.sin(t * 9 * side) * 22 * ex.spin];
    const [wx, wy] = ex.spin > 0.01 ? whirl(performance.now() / 1000) : [0, 0];
    const px = cx + this.gaze.x * 58 * (1 - ex.cross) + side * (4 + 62 * ex.cross) + wx;   // toward the nose
    const py = cy + 18 + this.gaze.y * 26 * (1 - ex.cross) + 8 * ex.cross + wy;
    ctx.fillStyle = "#000";
    ctx.beginPath();
    ctx.arc(px, py, 34 * ex.pupil, 0, TAU);
    ctx.fill();
    // Tiny highlight makes the pupil read as an eye from a distance.
    ctx.fillStyle = `rgba(255, 245, 210, ${0.9 * brightness})`;
    ctx.beginPath();
    ctx.arc(px - 11, py - 12, 7, 0, TAU);
    ctx.fill();

    // Eyelid: black band sliding down from the top.
    const top = cy - 100, bottom = cy + 70;
    let lid = clamp(this.lid + ex.lid * (1 - this.lid), 0, 1);
    if (side > 0) lid = lerp(lid, 0.93, ex.wink);   // wink: the face's right eye (screen left) shuts
    ctx.fillStyle = "#000";
    // Upper lid: a black band sliding down, angled (inner end lower) for a sly or spooky look.
    const lidY = top - 10 + (bottom - top + 10) * lid;
    const slope = ex.lidTilt * 120;   // how much lower the inner corner sits
    ctx.beginPath();
    ctx.moveTo(cx - 150, top - 20);
    ctx.lineTo(cx + 150, top - 20);
    ctx.lineTo(cx + 150, lidY + (side < 0 ? slope : 0));
    ctx.lineTo(cx - 150, lidY + (side > 0 ? slope : 0));
    ctx.closePath();
    ctx.fill();
    // Cheeks: a dark curve rising from below leaves a happy upside-down-U eye (smiling, laughing).
    const cheek = Math.max(this.squint, ex.cheek);
    if (cheek > 0.01) {
      ctx.beginPath();
      ctx.ellipse(cx, bottom + 40, 150, 40 + 80 * cheek, 0, 0, TAU);
      ctx.fill();
    }
    ctx.restore();
  }

  // Carved brows: a glowing slit above each eye that rises, tilts and pops with the expression.
  drawBrow(cx, cy, side, brightness) {
    const ctx = this.ctx, ex = this.ex;
    const up = (ex.brow + 0.45 * this.browFlash + (side > 0 ? ex.browAsym : -ex.browAsym * 0.4)) * 38;
    const inner = ex.browTilt * 55;               // inner end (toward the nose) raised by this much
    const y = cy - 158 - up;
    const xi = cx + side * 70, xo = cx - side * 95;   // inner and outer ends
    ctx.save();
    ctx.lineCap = "round";
    ctx.lineWidth = 22;
    ctx.shadowColor = "rgba(255, 120, 0, 0.9)";
    ctx.shadowBlur = 35;
    ctx.strokeStyle = this.glow(cx, y, 120, brightness * 0.9);
    ctx.beginPath();
    ctx.moveTo(xo, y + 14);
    ctx.quadraticCurveTo(cx, y - 18 - inner * 0.5, xi, y + 6 - inner);
    ctx.stroke();
    ctx.restore();
  }

  drawNose(brightness) {
    const ctx = this.ctx;
    ctx.save();
    ctx.beginPath();
    roundedPolygon(ctx, [[-42, 40], [42, 40], [0, -22]], 10);
    ctx.shadowColor = "rgba(255, 120, 0, 0.8)";
    ctx.shadowBlur = 30;
    ctx.fillStyle = this.glow(0, 25, 70, brightness * 0.9);
    ctx.fill();
    ctx.restore();
  }

  drawMouth(brightness) {
    const ctx = this.ctx, ex = this.ex;
    const pk = ex.pucker;
    const open = this.mouth * (1 - 0.6 * pk);
    const W = 290 * (1 - 0.5 * ex.round) * (1 - 0.15 * Math.abs(ex.smirk)) * (1 - 0.75 * pk);
    // Corners rise for a smile and drop for a frown; a smirk lifts one side.
    const lift = (ex.smile - 0.35) * 70;
    const yL = 110 - lift - ex.smirk * 35, yR = 110 - lift + ex.smirk * 25;
    const frown = Math.max(0, -ex.smile);     // a frown bends both lips upward into an arch
    let upperC = 215 - open * 25 - ex.round * 60 - frown * 160;
    let lowerC = Math.max(upperC + 20 + open * 150, 255 + open * 190 + ex.round * 90 - frown * 200);
    if (pk > 0.01) {                           // puckered: a small round "o" of lips
      upperC = lerp(upperC, yL - 95, pk);
      lowerC = lerp(lowerC, yL + 95 + open * 60, pk);
    }
    const curveY = (x, c) => {                 // quadratic with control at x=0: x is linear in t
      const t = (x + W) / (2 * W);
      return (1 - t) ** 2 * yL + 2 * t * (1 - t) * c + t * t * yR;
    };
    ctx.save();
    ctx.translate(ex.smirk * 40, 0);
    ctx.beginPath();
    ctx.moveTo(-W, yL);
    ctx.quadraticCurveTo(0, upperC, W, yR);
    ctx.quadraticCurveTo(0, lowerC, -W, yL);
    ctx.closePath();
    ctx.shadowColor = "rgba(255, 120, 0, 0.9)";
    ctx.shadowBlur = 50;
    ctx.fillStyle = this.glow(0, 200, 330, brightness);
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.clip();

    // Teeth are uncarved pumpkin, so they're black blocks inside the glowing mouth (none when puckered).
    ctx.fillStyle = pk > 0.5 ? "rgba(0,0,0,0)" : "#000";
    if (pk > 0.3) {                            // the line between puckered lips
      ctx.save();
      ctx.fillStyle = "#000";
      ctx.beginPath();
      ctx.ellipse(0, curveY(0, (upperC + lowerC) / 2), W * 0.75, 6 + 30 * open, 0, 0, TAU);
      ctx.fill();
      ctx.restore();
    }
    const sx = W / 290;
    for (const [x0, x1] of [[-150 * sx, -95 * sx], [70 * sx, 125 * sx]]) {     // upper teeth hang down
      const y = Math.min(curveY(x0, upperC), curveY(x1, upperC));
      ctx.fillRect(x0, y - 20, x1 - x0, 20 + 46);
    }
    for (const [x0, x1] of [[-40 * sx, 15 * sx]]) {                             // lower tooth sticks up
      const y = Math.max(curveY(x0, lowerC), curveY(x1, lowerC));
      ctx.fillRect(x0, y - 48, x1 - x0, 48 + 20);
    }
    ctx.restore();
    if (ex.tongue > 0.02) this.drawTongue(curveY(0, lowerC), brightness);
  }

  // A glowing tongue poking out under the bottom lip, waggling.
  drawTongue(lipY, brightness) {
    const ctx = this.ctx, ex = this.ex, k = ex.tongue;
    const t = performance.now() / 1000;
    const len = 40 + 150 * k, half = 72 * Math.min(1, k * 1.5);
    ctx.save();
    ctx.translate(ex.smirk * 40, lipY - 30);
    ctx.rotate(0.22 * Math.sin(t * 6) * k);
    ctx.beginPath();
    ctx.moveTo(-half, 0);
    ctx.lineTo(-half, len * 0.55);
    ctx.quadraticCurveTo(-half, len + 18, 0, len + 18);
    ctx.quadraticCurveTo(half, len + 18, half, len * 0.55);
    ctx.lineTo(half, 0);
    ctx.closePath();
    ctx.shadowColor = "rgba(255, 90, 40, 0.9)";
    ctx.shadowBlur = 40;
    const grad = ctx.createRadialGradient(0, len * 0.6, 5, 0, len * 0.6, len);
    const a = clamp(brightness, 0, 1);
    grad.addColorStop(0, `rgba(255, 200, 170, ${a})`);
    grad.addColorStop(1, `rgba(255, 80, 40, ${a})`);
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.strokeStyle = "#000";          // the groove down the middle
    ctx.lineWidth = 9;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(0, 25);
    ctx.lineTo(0, len * 0.7);
    ctx.stroke();
    ctx.restore();
  }

  drawGuide() {
    const ctx = this.ctx;
    ctx.save();
    ctx.strokeStyle = "rgba(80, 160, 255, 0.8)";
    ctx.lineWidth = 4;
    ctx.beginPath();
    ctx.ellipse(0, 40, 470, 420, 0, 0, TAU);
    ctx.moveTo(-500, 0); ctx.lineTo(500, 0);
    ctx.moveTo(0, -460); ctx.lineTo(0, 500);
    ctx.stroke();
    ctx.restore();
  }
}

function roundedPolygon(ctx, pts, r) {
  const n = pts.length;
  const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  const start = mid(pts[n - 1], pts[0]);
  ctx.moveTo(start[0], start[1]);
  for (let i = 0; i < n; i++) {
    const p = pts[i], next = pts[(i + 1) % n];
    const m = mid(p, next);
    ctx.arcTo(p[0], p[1], m[0], m[1], r);
  }
  ctx.closePath();
}

function defaultCalibration() {
  return { x: 0, y: 0, scale: 0.9, rot: 0, stretch: 1 };
}
