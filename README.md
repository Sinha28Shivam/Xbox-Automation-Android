# xCloud Android Controller — WORKING setup

Control **Xbox Cloud Gaming on your Android phone** from Python, using an
Arduino Leonardo as a real USB gamepad.

**Status: working.** xCloud recognises the controller and every control has been
verified on hardware.

This folder contains *only* the configuration that works. The research and all
the paths that did not work live in `../xCloud-Android-Arduino/`.

---

## Quick start — four numbered files, run them in order

| Double-click | What it does | When |
|---|---|---|
| **`1-FLASH.bat`** | Flashes the Leonardo. Installs the core and library if needed. | Once (or after editing firmware) |
| **`2-CHECK.bat`** | Confirms everything is connected and talking | Any time |
| **`3-TEST.bat`** | Presses every control so you can watch the phone | To verify |
| **`4-RUN.bat`** | Interactive control — type commands, phone responds | Everyday use |

`1-FLASH.bat` is genuinely one click: it checks for the AVR core, installs the
correct Joystick library if it is missing, compiles, waits for you to tap RESET,
and flashes the moment the board appears.

---

## The hardware

```
   ┌────────┐   USB (COM8)   ┌──────────┐   3 jumpers   ┌──────────┐
   │   PC   ├───────────────▶│ FT232RL  ├──────────────▶│ LEONARDO │
   │        │  pad_link.py   │          │   UART D0/D1  │          │
   └────────┘                └──────────┘               └────┬─────┘
                                                             │ USB + OTG
                                                             ▼
                                                        ┌─────────┐
                                                        │  PHONE  │
                                                        │ xCloud  │
                                                        └─────────┘
```

### Wiring — three jumper wires

| FT232RL | → | Leonardo | Note |
|---|---|---|---|
| **TX** | → | **RX (D0)** | TX and RX must **cross** |
| **RX** | → | **TX (D1)** | |
| **GND** | → | **GND** | essential — no common ground means garbage data |
| VCC | → | *nothing* | each board has its own USB power |

Also: the FT232RL's voltage jumper must be on **5V**, not 3.3V.

### Why the FT232RL is needed at all

The Leonardo has only **one** USB port, and it must go to the phone — that is
how it appears as a controller. So the PC cannot reach it that way, and commands
arrive over the hardware UART instead. That is the FT232RL's whole job.

### Cables

* **Leonardo → phone:** micro-USB cable + **OTG adapter at the PHONE end**.
  At the board end it would tell the *Leonardo* to be a host, so both ends would
  be hosts and nothing would enumerate — silently, with no error anywhere.
* **FT232RL → PC:** any USB data cable.

---

## Startup order

1. **FT232RL → PC.** It should appear as COM8 (`0403:6001`).
2. **Jumpers** between FT232RL and Leonardo.
3. **Leonardo → phone** via cable + OTG adapter.
4. **Check the Leonardo's ON LED is lit.** This is the single most useful signal:
   USB power flows host → device, so a lit LED proves the phone entered host mode
   and is powering the board. Dark LED means no host mode, and nothing can work.
5. Run `2-CHECK.bat` → expect `firmware : xcloudpad-usb-1.0`.
6. Run `3-TEST.bat`. The first input clears xCloud's "connect a controller".

---

## Commands

`4-RUN.bat` gives you an interactive prompt, but you can script it directly:

```bat
python host\pad_link.py press a
python host\pad_link.py press down*3 right a
python host\pad_link.py press down --times 5 --interval 0.5
python host\pad_link.py hold guide 2.0
python host\pad_link.py stick left_stick right --duration 1.5
python host\pad_link.py trigger rt 255
python host\pad_link.py macro nav_test
python host\pad_link.py reset          :: release everything
python host\pad_link.py --list         :: every control name and macro
```

No `--port` or `--transport` needed — `config/controls.yaml` already defaults to
COM8 and the Leonardo.

### From your own Python

```python
import sys
sys.path.insert(0, r"host")           # or the absolute path
from pad_link import AndroidPad

with AndroidPad() as pad:
    pad.press("a")
    pad.press_times("down", 3, interval=0.4)
    pad.stick("left_stick", "right", duration=1.5)
    pad.trigger("rt", 255, duration=0.5)
    pad.run_macro("nav_test")
```

The `with` block matters: it always sends `RESET` on exit, so a crash cannot
leave a stick deflected and your character walking into a wall forever.

### Control names

`a b x y` · `up down left right` · `menu view guide` · `lb rb` · `ls rs` ·
`lt rt` · sticks `left_stick` / `right_stick` with `left/right/up/down`.

Aliases work too: `cross`, `circle`, `square`, `triangle`, `start`, `select`,
`xbox`, `l1`, `r2`, and so on. Run `--list` for the full map.

### Adding your own macros

Edit `config/controls.yaml`:

```yaml
macros:
  my_sequence:
    description: "What it does"
    steps:
      - { button: "a" }
      - { wait: 1.0 }
      - { button: "down", times: 3, interval: 0.4 }
      - { stick: "left_stick", direction: "right", duration: 1.5 }
      - { trigger: "rt", value: 255, duration: 0.5 }
```

Then `python host\pad_link.py macro my_sequence`. No code changes needed.

---

## Files

```
1-FLASH.bat  2-CHECK.bat  3-TEST.bat  4-RUN.bat     <- start here
config/controls.yaml            buttons, timings, macros
firmware/leonardo_gamepad/      the Arduino sketch
host/flash.py                   one-click flasher
host/pad_link.py                the controller API + CLI
host/verify_hid_raw.py          proves the pad works, using Windows as a host
requirements.txt                pyserial, pyyaml
```

### `verify_hid_raw.py` — the tool worth knowing about

Plug the Leonardo into the **PC** (not the phone) and run:

```bat
python host\verify_hid_raw.py
```

It reads the pad's **actual HID input reports** from the Windows HID stack and
reports which bytes change per control. Expected result: **8/8**.

This is the check that finally cracked the project. Every other test could only
say *"the firmware accepted the command"* — and that was exactly the trap,
because a bug had the firmware answering `OK` to everything while its HID
interface had never enumerated at all. Reading the real report bytes gives you a
check that is *capable of saying no*.

Use it whenever the phone stops responding: if it still reports 8/8, the pad is
fine and the fault is in the USB link to the phone.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no PONG from the board` | **Try once more first.** If the board rebooted moments earlier its `READY` banner is still in flight and the first attempt can miss it. If it persists, it is the UART link: TX→D0 and RX→D1 must **cross**; GND connected; FT232RL jumper on **5V**; Arduino Serial Monitor closed |

| `cannot open COM8` | Something else holds the port — close the Serial Monitor or another `pad_link.py` |
| No COM8 at all | FT232RL unplugged, or a charge-only cable. Run `python host\pad_link.py ports` |
| Commands say `ok` but phone does nothing | Leonardo's ON LED dark? Phone is not in host mode. OTG adapter must be at the **phone** end, and the cable must carry data |
| Flash fails: `No device found` | The ~8 s bootloader window closed. Rerun `1-FLASH.bat` and tap RESET when prompted. The board is **not** bricked — the bootloader cannot be erased |
| Compile error `'Joystick_' does not name a type` | Wrong Joystick library. `arduino-cli lib uninstall Joystick`, then rerun `1-FLASH.bat` — it installs the right one from GitHub |
| A/B or X/Y swapped in a game | We enumerate as VID `2341` (Arduino), so Android applies a *generic* layout. Fix `BTN_TABLE` in the sketch — there is a marked comment at exactly that spot |
| Stick stuck, character keeps walking | `python host\pad_link.py reset` |
| Presses skipped in menus | Raise the interval: `--interval 0.5`. xCloud adds 60–100 ms network latency on top of its UI animations |
| Phone battery draining | Expected — the phone powers the board in OTG host mode. Use a powered USB hub in that path |

---

## Known limitations

* **Blind automation.** A firmware `OK` proves the HID report was queued, not
  that the game reacted. Closing that loop means mirroring the phone screen
  (scrcpy + OpenCV) — the role a capture card plays in the console project.
* **Not frame-accurate.** xCloud's 60–100 ms network round trip dominates
  everything; the adapter is no longer the bottleneck. Menu automation and timed
  holds are entirely practical.
* **No charging while playing** unless you add a powered hub.
* **`guide` is unverified** — some Android builds intercept the HID Home usage
  before the app sees it.

---

## Switching the Leonardo back to Xbox console duty

This firmware replaced GIMX EMUXONE. To restore it:

```bat
..\xCloud-Android-Arduino\run\leonardo-to-CONSOLE.bat
```

Then come back to Android with `1-FLASH.bat`. Both directions work indefinitely —
the bootloader cannot be erased, so you can never strand yourself. Details in
`../xCloud-Android-Arduino/SWITCHING.md`.

One asymmetry to expect: after flashing GIMX the board **stops presenting an
Arduino serial port**, which looks alarming but is correct. Tap RESET to get a
bootloader window before reflashing.
