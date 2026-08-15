/*
 * leonardo_usb_gamepad.ino - ATmega32u4 as a USB HID gamepad for Android xCloud
 * =============================================================================
 *
 * FALLBACK / two-board path (approach 2.B in the README). Prefer the ESP32 BLE
 * sketch unless you specifically want to reuse the Leonardo you already own.
 *
 *   PC --USB serial--> Uno/Nano --UART--> THIS Leonardo --USB HID--> OTG --> phone
 *
 * WHY A SECOND BOARD IS NEEDED
 * ----------------------------
 * The 32u4 has exactly ONE USB port. Here it is plugged into the PHONE (that is
 * how it appears as a gamepad), so it cannot also be the PC's serial port at the
 * same time. Commands therefore arrive on the hardware UART (`Serial1`, pins
 * D0/D1) from a cheap Uno/Nano acting as a USB<->UART passthrough.
 *
 * Wiring:
 *      Uno TX (D1) --[1k]-- Leonardo RX (D0)
 *      Uno RX (D0) -------- Leonardo TX (D1)
 *      GND         -------- GND
 *      Leonardo USB -> USB-C OTG adapter -> phone   (powered hub = phone charges)
 *      Uno USB      -> PC                           (pad_link.py talks to this)
 *
 * Passthrough sketch for the Uno:
 *      void setup(){ Serial.begin(115200); }
 *      void loop(){ while(Serial.available()) Serial.write(Serial.read()); }
 *   ...on an Uno you actually need SoftwareSerial or just use a USB-TTL adapter
 *   (FTDI/CP2102) straight into D0/D1 - simplest option, no second sketch.
 *
 * THIS OVERWRITES THE GIMX FIRMWARE
 * ---------------------------------
 * Flashing this replaces EMUXONE on the board. To go back to console
 * automation, re-run Xbox-Automation-Python/flash-leonardo/flash_leonardo.ps1.
 *
 * LIBRARY
 * -------
 *   Joystick  by MHeironimus (ArduinoJoystickLibrary)  - Library Manager
 *
 * PROTOCOL
 * --------
 * Byte-for-byte the SAME line protocol as the ESP32 sketch, so host/pad_link.py
 * does not care which board is attached:
 *
 *   PING | B <name> <0|1> | T <lt> <rt> | S <lx> <ly> <rx> <ry> | H <angle|C>
 *   RESET | STATE | HELP
 *
 * Replies go to BOTH Serial1 (the host) and Serial (USB CDC, if a PC happens to
 * be watching) so you can debug the board on a bench without rewiring.
 */

#include <Joystick.h>

#define FW_VERSION "xcloudpad-usb-1.0"
#define BAUD       115200
#define MAX_LINE   64

/* 16 buttons, 1 hat, X/Y (left stick), Rx/Ry (right stick), Z/Rz (triggers).
 * No rudder/throttle/accelerator/brake/steering - Android would expose them as
 * phantom axes and some titles auto-bind to them. */
Joystick_ Joystick(
  JOYSTICK_DEFAULT_REPORT_ID, JOYSTICK_TYPE_GAMEPAD,
  16,     // button count
  1,      // hat switch count  (D-pad MUST be a hat for Android UI navigation)
  true,   // X  - left stick X
  true,   // Y  - left stick Y
  true,   // Z  - left trigger
  true,   // Rx - right stick X
  true,   // Ry - right stick Y
  true,   // Rz - right trigger
  false,  // rudder
  false,  // throttle
  false,  // accelerator
  false,  // brake
  false); // steering

// ---------------------------------------------------------------------------
// Button name -> Joystick button index (0-based here, unlike the ESP32 lib)
// Order matches the ESP32 table so both boards behave identically.
// ---------------------------------------------------------------------------
struct BtnMap { const char *name; uint8_t idx; };

static const BtnMap BTN_TABLE[] = {
  { "a",      0  },
  { "b",      1  },
  { "x",      3  },
  { "y",      4  },
  { "lb",     6  },
  { "rb",     7  },
  { "select", 10 },
  { "start",  11 },
  { "home",   12 },   // Guide - see YAML note, unverified on Android
  { "ls",     13 },
  { "rs",     14 },
};
static const uint8_t BTN_COUNT = sizeof(BTN_TABLE) / sizeof(BTN_TABLE[0]);

int16_t lx = 0, ly = 0, rx = 0, ry = 0;   // -128..127 host convention
uint8_t lt = 0, rt = 0;                   // 0..255
int16_t hat = -1;
uint32_t heldMask = 0;

char line[MAX_LINE];
uint8_t lineLen = 0;

// ---------------------------------------------------------------------------
// Reply to both links so the board is debuggable either way
// ---------------------------------------------------------------------------
static void say(const char *s)  { Serial1.println(s); Serial.println(s); }
static void sayNoNl(const char *s) { Serial1.print(s); Serial.print(s); }
static void sayNum(long v)      { Serial1.print(v);   Serial.print(v); }
static void sayEnd()            { Serial1.println();  Serial.println(); }

static int findButton(const char *name) {
  for (uint8_t i = 0; i < BTN_COUNT; i++) {
    if (strcasecmp(name, BTN_TABLE[i].name) == 0) return (int)i;
  }
  return -1;
}

static int16_t clampAxis(long v) { return v < -128 ? -128 : (v > 127 ? 127 : (int16_t)v); }
static uint8_t clampTrig(long v) { return v < 0 ? 0 : (v > 255 ? 255 : (uint8_t)v); }

/* Host: -128..127, -128 = up/left. Joystick library default range: -127..127.
 * Clamp -128 to -127 rather than rescaling, so full deflection stays full. */
static int16_t axisToHid(int16_t v) { return v < -127 ? -127 : v; }

/* Triggers are unsigned 0..255 on the host but the library axis is signed. */
static int16_t trigToHid(uint8_t v) { return (int16_t)v - 128; }

static void pushReport() {
  Joystick.setXAxis (axisToHid(lx));
  Joystick.setYAxis (axisToHid(ly));
  Joystick.setRxAxis(axisToHid(rx));
  Joystick.setRyAxis(axisToHid(ry));
  Joystick.setZAxis (trigToHid(lt));
  Joystick.setRzAxis(trigToHid(rt));
  Joystick.setHatSwitch(0, hat);        // -1 = centred, else degrees
  Joystick.sendState();
}

static void resetAll() {
  for (uint8_t i = 0; i < BTN_COUNT; i++) Joystick.releaseButton(BTN_TABLE[i].idx);
  heldMask = 0;
  lx = ly = rx = ry = 0;
  lt = rt = 0;
  hat = -1;
  pushReport();
}

static uint8_t tokenize(char *s, char *tok[], uint8_t maxTok) {
  uint8_t n = 0;
  char *p = strtok(s, " \t");
  while (p && n < maxTok) { tok[n++] = p; p = strtok(NULL, " \t"); }
  return n;
}

static bool isKeep(const char *t) { return t[0] == '-' && t[1] == '\0'; }

// ---------------------------------------------------------------------------
static void handleLine(char *raw) {
  char *tok[8];
  uint8_t n = tokenize(raw, tok, 8);
  if (n == 0) return;
  const char *cmd = tok[0];

  if (strcasecmp(cmd, "PING") == 0) {
    // USB HID has no "is the host listening?" signal we can trust, so we always
    // report connected=1. Unlike BLE, if the cable is in, the pad exists.
    say("PONG " FW_VERSION " usb 1");
    return;
  }

  if (strcasecmp(cmd, "HELP") == 0) {
    say("CMDS PING|B <name> <0|1>|T <lt> <rt>|S <lx> <ly> <rx> <ry>|"
        "H <angle|C>|RESET|STATE");
    return;
  }

  if (strcasecmp(cmd, "RESET") == 0) { resetAll(); say("OK"); return; }

  if (strcasecmp(cmd, "STATE") == 0) {
    sayNoNl("STATE conn=1 btn=0x"); Serial1.print(heldMask, HEX); Serial.print(heldMask, HEX);
    sayNoNl(" l="); sayNum(lx); sayNoNl(","); sayNum(ly);
    sayNoNl(" r="); sayNum(rx); sayNoNl(","); sayNum(ry);
    sayNoNl(" t="); sayNum(lt); sayNoNl(","); sayNum(rt);
    sayNoNl(" hat="); sayNum(hat); sayEnd();
    return;
  }

  if (strcasecmp(cmd, "B") == 0) {
    if (n < 3) { say("ERR usage: B <name> <0|1>"); return; }
    int idx = findButton(tok[1]);
    if (idx < 0) {
      sayNoNl("ERR unknown button '"); sayNoNl(tok[1]); say("'");
      return;
    }
    if (atol(tok[2]) != 0) { Joystick.pressButton(BTN_TABLE[idx].idx);   heldMask |=  (1UL << idx); }
    else                   { Joystick.releaseButton(BTN_TABLE[idx].idx); heldMask &= ~(1UL << idx); }
    pushReport();
    say("OK");
    return;
  }

  if (strcasecmp(cmd, "T") == 0) {
    if (n < 3) { say("ERR usage: T <lt> <rt>"); return; }
    if (!isKeep(tok[1])) lt = clampTrig(atol(tok[1]));
    if (!isKeep(tok[2])) rt = clampTrig(atol(tok[2]));
    pushReport(); say("OK");
    return;
  }

  if (strcasecmp(cmd, "S") == 0) {
    if (n < 5) { say("ERR usage: S <lx> <ly> <rx> <ry>"); return; }
    if (!isKeep(tok[1])) lx = clampAxis(atol(tok[1]));
    if (!isKeep(tok[2])) ly = clampAxis(atol(tok[2]));
    if (!isKeep(tok[3])) rx = clampAxis(atol(tok[3]));
    if (!isKeep(tok[4])) ry = clampAxis(atol(tok[4]));
    pushReport(); say("OK");
    return;
  }

  if (strcasecmp(cmd, "H") == 0) {
    if (n < 2) { say("ERR usage: H <angle|C>"); return; }
    if (tok[1][0] == 'C' || tok[1][0] == 'c') {
      hat = -1;
    } else {
      long a = atol(tok[1]);
      if (a % 45 != 0 || a < 0 || a > 315) {
        say("ERR hat must be C or a multiple of 45 in 0..315");
        return;
      }
      hat = (int16_t)a;
    }
    pushReport(); say("OK");
    return;
  }

  sayNoNl("ERR unknown command '"); sayNoNl(cmd); say("'");
}

// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(BAUD);     // USB CDC - bench debugging only
  Serial1.begin(BAUD);    // D0/D1 - the real command link

  Joystick.setXAxisRange(-127, 127);
  Joystick.setYAxisRange(-127, 127);
  Joystick.setRxAxisRange(-127, 127);
  Joystick.setRyAxisRange(-127, 127);
  Joystick.setZAxisRange(-127, 127);
  Joystick.setRzAxisRange(-127, 127);

  Joystick.begin(false);  // false = we call sendState() ourselves (one report)
  resetAll();

  say("READY " FW_VERSION " - USB HID gamepad, commands on Serial1 (D0/D1)");
}

void loop() {
  // Accept commands from either link; Serial1 is the intended one.
  while (Serial1.available() || Serial.available()) {
    char c = Serial1.available() ? (char)Serial1.read() : (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      line[lineLen] = '\0';
      handleLine(line);
      lineLen = 0;
    } else if (lineLen < MAX_LINE - 1) {
      line[lineLen++] = c;
    } else {
      lineLen = 0;
      say("ERR line too long");
    }
  }
}
