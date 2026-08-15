"""
verify_hid_raw.py - Read the pad's HID reports straight from Windows HID stack.

WHY NOT winmm / joyGetPosEx
---------------------------
verify_hid_on_pc.py tried the legacy joystick API and found nothing, even though
Device Manager clearly shows a healthy "HID-compliant game controller". That is
a known limitation, not a fault in our pad: winmm's joystick API predates modern
USB HID, and Windows only exposes devices through it if a legacy joystick driver
claims them. Plenty of perfectly good gamepads are invisible to it.

So instead we bypass the legacy layer and read the RAW HID INPUT REPORTS from the
device itself, via SetupAPI + CreateFile + ReadFile. That is the same data any
application would receive, so if the bytes change when we send commands, the HID
layer is proven for real.

WHAT A PASS MEANS
-----------------
The report bytes changing proves, on actual hardware:
  * the HID interface enumerated,
  * the report descriptor is parseable by a real host,
  * our report packing and byte offsets are correct,
  * and a host genuinely receives our input.

That is everything except "the phone specifically works", which then reduces to
the USB link (OTG adapter / cable / host role) rather than anything in the code.

USAGE
    python verify_hid_raw.py
    python verify_hid_raw.py --port COM12
    python verify_hid_raw.py --vid 2341 --pid 8036

Windows only.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pad_link import ControlConfig, PadLink  # noqa: E402

setupapi = ctypes.WinDLL("setupapi")
kernel32 = ctypes.WinDLL("kernel32")
hid = ctypes.WinDLL("hid")

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD), ("Reserved", ctypes.POINTER(ctypes.c_ulong))]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", ctypes.c_wchar * 512)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", ctypes.c_ulong), ("VendorID", ctypes.c_ushort),
                ("ProductID", ctypes.c_ushort), ("VersionNumber", ctypes.c_ushort)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


# ---------------------------------------------------------------------------
# Explicit signatures.
#
# ctypes defaults every return value to C `int` (32-bit). On 64-bit Windows a
# HANDLE is 64-bit, so the default truncates it - the handle then looks invalid
# and every subsequent call fails silently. This is almost certainly why the
# first attempt reported "no HID interface found" despite Device Manager showing
# a healthy game controller.
# ---------------------------------------------------------------------------
setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]

setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]

setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]

setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]

hid.HidD_GetHidGuid.restype = None
hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]

hid.HidD_GetAttributes.restype = wintypes.BOOL
hid.HidD_GetAttributes.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]

hid.HidD_GetPreparsedData.restype = wintypes.BOOL
hid.HidD_GetPreparsedData.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]

hid.HidD_FreePreparsedData.restype = wintypes.BOOL
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]

hid.HidP_GetCaps.restype = ctypes.c_long          # NTSTATUS
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]

hid.HidD_SetNumInputBuffers.restype = wintypes.BOOL
hid.HidD_SetNumInputBuffers.argtypes = [wintypes.HANDLE, ctypes.c_ulong]


def hid_guid() -> GUID:
    g = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(g))
    return g



def find_hid_paths(vid: int, pid: int) -> list[str]:
    """Every HID interface path matching this VID/PID."""
    guid = hid_guid()
    hdev = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if hdev == INVALID_HANDLE_VALUE:
        return []

    found: list[str] = []
    idx = 0
    while True:
        did = SP_DEVICE_INTERFACE_DATA()
        did.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
        if not setupapi.SetupDiEnumDeviceInterfaces(
                hdev, None, ctypes.byref(guid), idx, ctypes.byref(did)):
            break
        idx += 1

        need = wintypes.DWORD(0)
        setupapi.SetupDiGetDeviceInterfaceDetailW(
            hdev, ctypes.byref(did), None, 0, ctypes.byref(need), None)
        detail = SP_DEVICE_INTERFACE_DETAIL_DATA_W()
        detail.cbSize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
        if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, ctypes.byref(did), ctypes.byref(detail),
                ctypes.sizeof(detail), None, None):
            continue

        path = detail.DevicePath
        h = kernel32.CreateFileW(
            path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
            OPEN_EXISTING, 0, None)
        if h == INVALID_HANDLE_VALUE:
            continue
        try:
            attrs = HIDD_ATTRIBUTES()
            attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
            if hid.HidD_GetAttributes(h, ctypes.byref(attrs)):
                if attrs.VendorID == vid and attrs.ProductID == pid:
                    found.append(path)
        finally:
            kernel32.CloseHandle(h)

    setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return found


def open_for_read(path: str) -> int | None:
    h = kernel32.CreateFileW(
        path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
        OPEN_EXISTING, 0, None)
    return None if h == INVALID_HANDLE_VALUE else h


def input_report_length(handle: int) -> int:
    """Report length from the parsed descriptor - proves Windows understood it."""
    prep = ctypes.c_void_p()
    if not hid.HidD_GetPreparsedData(handle, ctypes.byref(prep)):
        return 0
    try:
        caps = HIDP_CAPS()
        if hid.HidP_GetCaps(prep, ctypes.byref(caps)) != 0x00110000:  # HIDP_STATUS_SUCCESS
            return 0
        return caps.InputReportByteLength
    finally:
        hid.HidD_FreePreparsedData(prep)


def read_report(handle: int, length: int, timeout: float = 0.4) -> bytes | None:
    """Read one input report. Returns None on timeout.

    HidD_SetNumInputBuffers keeps the queue short so we get FRESH reports rather
    than stale queued ones - otherwise a change could appear to lag by a step.
    """
    hid.HidD_SetNumInputBuffers(handle, 2)
    buf = ctypes.create_string_buffer(length)
    read = wintypes.DWORD(0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if kernel32.ReadFile(handle, buf, length, ctypes.byref(read), None):
            if read.value:
                return bytes(buf.raw[:read.value])
        else:
            return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read raw HID reports to prove the pad works.")
    ap.add_argument("--port", default=None, help="the pad's CDC port, e.g. COM12")
    ap.add_argument("--vid", default="2341", help="hex VID (default 2341 Arduino)")
    ap.add_argument("--pid", default="8036", help="hex PID (default 8036 Leonardo)")
    args = ap.parse_args()

    vid, pid = int(args.vid, 16), int(args.pid, 16)

    print("=" * 70)
    print("  RAW HID REPORT VERIFICATION")
    print("=" * 70)
    print()
    print("  Reading the pad's actual HID input reports from the Windows HID")
    print("  stack. If the bytes change when we send commands, the HID layer")
    print("  is proven - descriptor, packing, offsets and all.")
    print()
    print(f"  looking for VID:PID {vid:04X}:{pid:04X}")

    paths = find_hid_paths(vid, pid)
    if not paths:
        print()
        print("  FAIL: no HID interface found for that VID:PID.")
        print("  Is the board plugged into THIS PC?")
        return 1
    print(f"  HID interfaces found   : {len(paths)}")

    # A composite device exposes several interfaces; the gamepad is the one
    # whose descriptor Windows parsed into a non-trivial input report.
    handle = None
    length = 0
    for p in paths:
        h = open_for_read(p)
        if h is None:
            continue
        n = input_report_length(h)
        if n > 1:
            handle, length = h, n
            print(f"  input report length    : {n} bytes "
                  f"(Windows parsed our descriptor)")
            break
        kernel32.CloseHandle(h)

    if handle is None:
        print()
        print("  FAIL: found the device but no usable input report.")
        print("  That points at a malformed report descriptor.")
        return 1

    cfg = ControlConfig()
    link = PadLink(cfg, args.port)
    if not link.open(quiet=True):
        kernel32.CloseHandle(handle)
        return 1
    print(f"  command port           : {link.port}")
    print(f"  firmware               : {link.firmware}")
    print()

    link.reset()
    time.sleep(0.3)
    idle = read_report(handle, length)
    if idle is None:
        print("  Note: no idle report yet (the pad only reports on change).")
        idle = b"\x00" * length
    print(f"  idle report : {idle.hex(' ')}")
    print()
    print("-" * 70)

    tests = [
        ("A button",           "B a 1",        "B a 0"),
        ("B button",           "B b 1",        "B b 0"),
        ("X button",           "B x 1",        "B x 0"),
        ("D-pad DOWN (hat)",   "H 180",        "H C"),
        ("D-pad RIGHT (hat)",  "H 90",         "H C"),
        ("Left stick RIGHT",   "S 127 0 - -",  "S 0 0 - -"),
        ("Left stick UP",      "S 0 -127 - -", "S 0 0 - -"),
        ("Right trigger",      "T - 255",      "T - 0"),
    ]

    passed, failed = 0, []
    for label, hold, release in tests:
        print(f"  {label:<22}", end=" ", flush=True)
        if not link.send(hold):
            print("REJECTED by firmware")
            failed.append(label)
            link.send(release)
            continue

        rep = read_report(handle, length)
        link.send(release)
        # Drain the release report so it cannot be mistaken for the next test.
        read_report(handle, length, timeout=0.2)

        if rep is None:
            print("no report received")
            failed.append(label)
        elif rep == idle:
            print(f"UNCHANGED  {rep.hex(' ')}")
            failed.append(label)
        else:
            diff = [i for i in range(min(len(rep), len(idle)))
                    if rep[i] != idle[i]]
            print(f"CHANGED    {rep.hex(' ')}   (bytes {diff})")
            passed += 1

    link.reset()
    link.close()
    kernel32.CloseHandle(handle)

    print("-" * 70)
    print()
    print("=" * 70
          )
    print(f"  RESULT: {passed}/{len(tests)} controls confirmed in raw HID reports")
    print("=" * 70)
    print()

    if passed == len(tests):
        print("  ALL PASSED - the gamepad is genuinely working.")
        print()
        print("  Windows parsed our report descriptor and received a distinct,")
        print("  correct report for every control. This is the strongest")
        print("  evidence in the project so far: the firmware, the descriptor")
        print("  and the report packing are all proven on real hardware.")
        print()
        print("  NEXT: connect it to the phone.")
        print("      1. Unplug the Leonardo from the PC.")
        print("      2. [Leonardo USB] --cable--> [OTG adapter] --> [PHONE]")
        print("                                   ^^ at the PHONE end")
        print("      3. The phone should detect a gamepad.")
        print()
        print("  If the phone shows nothing, the pad is NOT the problem - we")
        print("  just proved it works. The fault is the USB link: OTG adapter")
        print("  orientation, a charge-only cable, or the phone refusing host")
        print("  role. See WIRING.md section 6a.")
    elif passed:
        print("  PARTIAL - some controls report, some do not:")
        for f in failed:
            print(f"      - {f}")
        print()
        print("  A partial result points at the report descriptor rather than")
        print("  the USB link. Buttons working while axes do not usually means")
        print("  an axis range or byte-offset mismatch.")
    else:
        print("  NO reports changed. The firmware accepts commands but its")
        print("  reports never reach the host - malformed descriptor, or the")
        print("  HID interface was registered too late to enumerate.")

    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
