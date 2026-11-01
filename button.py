"""The big glowing button that wakes the pumpkin, and the candy machine. Both optional: with nothing configured
(or off the Pi) every call is a no-op."""

import threading
import time


class WakeButton:
    """An arcade button on a GPIO pin (other leg to ground). Its LED breathes while the pumpkin waits, to draw
    kids in, and stays lit during a show."""

    def __init__(self, cfg, on_press, log):
        self.log, self.btn, self.led = log, None, None
        pin, led = int(cfg.get("pin") or 0), int(cfg.get("led_pin") or 0)
        if not pin and not led:
            return
        try:
            from gpiozero import Button, PWMLED
        except Exception as exc:
            log(f"button off (gpiozero unavailable: {exc})")
            return
        try:
            if pin:
                self.btn = Button(pin, pull_up=True, bounce_time=0.05)
                self.btn.when_pressed = lambda: on_press("button")
                log(f"wake button on GPIO{pin}")
            if led:
                self.led = PWMLED(led)
                log(f"button light on GPIO{led}")
        except Exception as exc:
            log(f"button off ({type(exc).__name__}: {exc})")
        self.waiting()

    def waiting(self):
        """Between shows: a slow glow that says "press me"."""
        if self.led:
            self.led.pulse(fade_in_time=1.2, fade_out_time=1.2)

    def showing(self):
        if self.led:
            self.led.on()


class Candy:
    """A cereal dispenser whose paddle wheel is turned by a continuous-rotation servo: each turn drops a treat
    down the chute."""

    def __init__(self, cfg, log):
        self.cfg, self.log, self.turn = cfg, log, None
        self._lock = threading.Lock()
        if not cfg.get("enabled"):
            return
        ch = int(cfg["channel"])
        try:
            if cfg["servo"] == "pca9685":
                from adafruit_servokit import ServoKit
                servo = ServoKit(channels=16).continuous_servo[ch]
                self.turn = lambda v: setattr(servo, "throttle", v)
            else:
                from gpiozero import Servo
                servo = Servo(ch)
                self.turn = lambda v: setattr(servo, "value", v)
            self.turn(0)
            log(f"candy machine on {cfg['servo']} {ch}")
        except Exception as exc:
            log(f"candy machine off ({type(exc).__name__}: {exc})")
            self.turn = None

    @property
    def ready(self):
        return self.turn is not None

    def dispense(self):
        """Drop one treat (in the background)."""
        if not self.turn:
            self.log("🍬 candy (no machine connected)")
            return False

        def run():
            with self._lock:
                try:
                    self.turn(max(-1.0, min(1.0, float(self.cfg["throttle"]))))
                    time.sleep(float(self.cfg["turn_s"]))
                finally:
                    self.turn(0)
        self.log("🍬 candy!")
        threading.Thread(target=run, daemon=True, name="candy").start()
        return True
