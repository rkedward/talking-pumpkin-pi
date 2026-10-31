# Talking Pumpkin: parts list (Raspberry Pi 5 build)

Prices checked 2026-10-05 on the linked pages (US). Adafruit carries most of the Pi parts, so one
order covers them. Raspberry Pi parts are also sold by other official resellers (the "Buy now" button on
[raspberrypi.com](https://www.raspberrypi.com/products/raspberry-pi-5/) lists them).

## Core pumpkin (needed)

| # | Part | Link | Price |
|---|---|---|---|
| 1 | Raspberry Pi 5, 8 GB (4 GB, $130, also works) | [Adafruit 5813](https://www.adafruit.com/product/5813) | $200.00 |
| 2 | Official Active Cooler | [Adafruit 5815](https://www.adafruit.com/product/5815) | $13.50 |
| 3 | Official 27 W USB-C power supply | [Adafruit 5814](https://www.adafruit.com/product/5814) | $14.04 |
| 4 | Official A2 microSD card, 32 GB | [Adafruit 6010](https://www.adafruit.com/product/6010) | see page |
| 5 | Camera Module 3 (66° view; get the Wide version, $38.50, [Adafruit 5658](https://www.adafruit.com/product/5658), if kids stand off to the sides) | [Adafruit 5657](https://www.adafruit.com/product/5657) | $29.25 |
| 6 | Pi 5 camera cable, 300 mm (200 mm / 500 mm options on the same page) | [Adafruit 5819](https://www.adafruit.com/product/5819) | $3.16 |
| 7 | reSpeaker XVF3800 4-Mic Array with XIAO ESP32S3 (SKU 114993700, ordered). Needs its USB firmware loaded once, see README. The ReSpeaker USB Mic Array v2.0 also works. | [Seeed Studio](https://www.seeedstudio.com/ReSpeaker-Mic-Array-v2-0.html) (v2.0) | ordered |
| 8 | Official micro-HDMI to HDMI cable, 1 m | [Adafruit 4302](https://www.adafruit.com/product/4302) | $11.81 |
| 9 | USB speaker (plays through USB, no headphone jack needed; louder powered USB speakers also work). For barge-in (kids can interrupt him) get a powered speaker with a 3.5 mm input instead, plugged into the mic array, see README 1e | [Adafruit 3369](https://www.adafruit.com/product/3369) | $12.50 |
| 10 | Projector: small LED/"mini" projector with HDMI, 200+ ANSI lumens, short throw if it sits close | [Amazon search](https://www.amazon.com/s?k=mini+projector+hdmi+ansi+lumens) | ~$60-150 |
| 11 | Pumpkin: uncarved foam craft pumpkin, white or pale orange, 10-13 in | [Amazon search](https://www.amazon.com/s?k=white+foam+craft+pumpkin+large) | ~$15-25 |

Core total: roughly **$440-530** depending on the projector (about $360 for items 1-9).

Everything below is an add-on: optional, independent of the others, and fine to add after the core
pumpkin is working.

## Add-on: window side show

| # | Part | Link | Price |
|---|---|---|---|
| 19 | 1 or 2 of your Magcubic HY300 Pro+ smart projectors. On Wi-Fi they need nothing else (see README, "Window side show") | already owned | - |
| 20 | Only for a projector wired to the Pi: official micro-HDMI to HDMI cable for the Pi's second port | [Adafruit 4302](https://www.adafruit.com/product/4302) | $11.81 |
| 21 | Only for a wired projector: long HDMI extension to reach it (length to suit) | [Amazon search](https://www.amazon.com/s?k=hdmi+extension+cable+female+to+male+25+ft) | ~$10-20 |

## Add-on: doorbell and Home Assistant

| # | Part | Link | Price |
|---|---|---|---|
| 22 | Raspberry Pi 4 running Home Assistant OS | already owned | - |
| 23 | A doorbell and porch light Home Assistant supports locally (Alarm.com can't share them), e.g. a Reolink video doorbell | [Reolink doorbells](https://reolink.com/product-category/doorbell/) | see page |

## Add-on: moving eyebrows

| # | Part | Link | Price |
|---|---|---|---|
| 12 | 16-Channel PWM/Servo HAT (PCA9685), light soldering needed | [Adafruit 2327](https://www.adafruit.com/product/2327) | $17.50 |
| 13 | 2x micro servos (SG92R) | [Adafruit 169](https://www.adafruit.com/product/169) | $5.95 each |
| 14 | 5 V 2 A power supply for the servos | [Adafruit 276](https://www.adafruit.com/product/276) | $7.95 |
| 15 | Barrel jack to screw terminal adapter (connects 14 to the HAT) | [Adafruit 368](https://www.adafruit.com/product/368) | $2.00 |

Plus a bit of black craft foam for the brows, glued to the servo arms above the projected eyes.

## Add-on: fog and lights

| # | Part | Link | Price |
|---|---|---|---|
| 16 | Controllable four-outlet power relay: the Pi switches plug-in lights safely, no mains wiring | [Adafruit 2935](https://www.adafruit.com/product/2935) | $39.95 |
| 17 | Fog machine with a wired remote (e.g. Froggy's Fog 400 W) | [Amazon](https://www.amazon.com/Froggys-Fog-Halloween-400-Watt-Wireless/dp/B003U02JAQ) | see page |
| 18 | Small relay to press the fog remote's button | [Adafruit 4409](https://www.adafruit.com/product/4409) | $6.95 |

Fog warning: many cheap fog machine remotes carry mains voltage on the button. Only wire item 18 across
the button if you've confirmed with a meter that the remote is low voltage. Otherwise, run the fog machine
on its own timer and skip 18; the pumpkin works fine without it.

## Windows laptop instead of the Pi

A Windows 10 or 11 laptop can replace the Pi (see README, "Windows laptop version"). It needs:

- No items 1-6 or 8: the laptop replaces the Pi, cooler, power supply and card, and a USB webcam replaces
  the camera and its cable. A Logitech C920 (78° view) or any 1080p USB webcam works; it needs a USB
  extension (active, up to 5 m) if the laptop sits away from the door.
- The laptop's own HDMI cable to the projector (item 10). If the laptop only has USB-C, a USB-C to HDMI
  adapter.
- Items 7, 9, 10 and 11 as listed. Load the mic's firmware from any Pi or Mac/Linux machine once (see README).
- A laptop with 8 GB of memory and a recent Intel Core i5 / Ryzen 5 or better; no graphics card needed.

The moving eyebrows and fog/light relays plug into the Pi's pins, so a laptop can't drive them. Use smart
plugs through Home Assistant for lights and fog instead (README, "Doorbell and Home Assistant").

## Nice to have
- Tripod or clamp mount for the projector, outdoor extension cord, a clear plastic tub as a rain cover.
- A small sign by the pumpkin: "This pumpkin talks and uses a camera."
