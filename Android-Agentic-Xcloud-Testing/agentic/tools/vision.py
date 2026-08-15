"""
vision.py - turning a screenshot into evidence.

This module is the answer to the parent project's headline limitation:

    "Blind automation. A firmware OK proves the HID report was queued, not
     that the game reacted."

Three independent readings, cheapest first, each able to say NO:

  1. FRAME DIFF (numpy)  - did anything change at all? Purely mechanical, no
     model, no API cost. It is the one check that reliably catches "the command
     was accepted and absolutely nothing happened", which is precisely the trap
     that a firmware `OK` hides.
  2. OCR (pytesseract)   - literal on-screen text. Good for "Starting your
     game", error codes, "connect a controller".
  3. VISION LLM          - a described screen, for anything layout-dependent.

Pillow, numpy, pytesseract and cv2 are all optional. Each missing one removes a
reading and is recorded in `degraded_reasons` so the report can say which senses
were actually available - never silently pretending a check ran.
"""

from __future__ import annotations

import base64
import io
import time
from pathlib import Path
from typing import Any

from ..llm import LLMFactory, LLMUnavailable
from ..schemas import Observation
from ..settings import Settings

# Optional imports, probed once at module load.
try:
    from PIL import Image
    _PIL = True
except ImportError:
    Image = None            # type: ignore[assignment]
    _PIL = False

try:
    import numpy as np
    _NUMPY = True
except ImportError:
    np = None               # type: ignore[assignment]
    _NUMPY = False

try:
    import pytesseract
    _TESS = True
except ImportError:
    pytesseract = None      # type: ignore[assignment]
    _TESS = False


class VisionTool:
    """Screenshot analysis. Every method degrades instead of raising."""

    def __init__(self, settings: Settings, llm: LLMFactory, android: Any):
        self.s = settings
        self.llm = llm
        self.android = android
        self.degraded_reasons: list[str] = []

        if not _PIL:
            self.degraded_reasons.append(
                "pillow missing: no image decode, so no OCR and no frame diff "
                "(pip install pillow)")
        if not _NUMPY:
            self.degraded_reasons.append(
                "numpy missing: cannot compute frame differences, so 'the input "
                "did nothing' cannot be detected mechanically (pip install numpy)")
        if not _TESS:
            self.degraded_reasons.append(
                "pytesseract missing: no on-screen text extraction. Note it also "
                "needs the tesseract BINARY, not just the python package.")

    # -- availability ------------------------------------------------------
    @property
    def can_screenshot(self) -> bool:
        return bool(self.android and self.android.status.adb_available
                    and self.android.status.device_state == "device")

    @property
    def can_ocr(self) -> bool:
        return _TESS and _PIL and bool(self.s.get("vision.ocr.enabled", True))

    @property
    def can_diff(self) -> bool:
        return _NUMPY and _PIL

    # -- capture -----------------------------------------------------------
    def capture(self, run_id: str, label: str) -> tuple[str | None, str]:
        """Take a screenshot into the run's artifact folder."""
        if not self.can_screenshot:
            return None, "no adb device, so no screenshot"
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        dest = (self.s.artifact_dir(run_id, "screens")
                / f"{int(time.time() * 1000)}_{safe}.png")
        ok, detail = self.android.screencap(dest)
        return (str(dest), detail) if ok else (None, detail)

    # -- reading 1: frame diff --------------------------------------------
    def diff(self, current: str | None, previous: str | None) -> float | None:
        """Fraction of pixels that changed. None = could not tell.

        Grayscale + a per-pixel threshold, deliberately crude: we are asking
        "did the UI react", not "how did it react". Being crude also makes it
        robust to the video compression artefacts a cloud stream is full of - a
        pixel-exact comparison would call every frame different and thus never
        say no, which would make the check worthless.
        """
        if not self.can_diff or not current or not previous:
            return None
        try:
            with Image.open(current) as img_a, Image.open(previous) as img_b:
                a = img_a.convert("L")
                b = img_b.convert("L")
                if a.size != b.size:
                    # Rotation or a resolution change: unambiguously a change.
                    return 1.0
                # Downscale first - 100x smaller comparison, same verdict, and
                # it further suppresses compression noise.
                small = (max(1, a.size[0] // 8), max(1, a.size[1] // 8))
                arr_a = np.asarray(a.resize(small), dtype=np.int16)
                arr_b = np.asarray(b.resize(small), dtype=np.int16)
            delta = np.abs(arr_a - arr_b)
            changed = float((delta > 12).sum())
            return changed / float(delta.size or 1)
        except Exception as exc:                     # noqa: BLE001
            self.degraded_reasons.append(f"frame diff failed: {exc}")
            return None

    # -- reading 2: OCR ----------------------------------------------------
    def ocr(self, path: str | None) -> str:
        if not self.can_ocr or not path:
            return ""
        try:
            lang = str(self.s.get("vision.ocr.lang", "eng"))
            with Image.open(path) as img:
                return pytesseract.image_to_string(img, lang=lang).strip()
        except Exception as exc:                     # noqa: BLE001
            # Most often "tesseract is not installed or it's not in your PATH":
            # the python package alone is not enough.
            self.degraded_reasons.append(f"OCR failed: {exc}")
            return ""

    # -- reading 3: vision LLM --------------------------------------------
    def _encode(self, path: str) -> tuple[str, str] | None:
        """Read a PNG, downscale, return (mime, base64). None on failure."""
        try:
            raw = Path(path).read_bytes()
            max_width = int(self.s.get("vision.max_width", 1280))
            if not _PIL or max_width <= 0:
                return "image/png", base64.b64encode(raw).decode("ascii")
            with Image.open(io.BytesIO(raw)) as img:
                img = img.convert("RGB")
                if img.width > max_width:
                    ratio = max_width / float(img.width)
                    img = img.resize((max_width, int(img.height * ratio)))
                buf = io.BytesIO()
                img.save(buf, format="JPEG",
                         quality=int(self.s.get("vision.jpeg_quality", 70)))
            return "image/jpeg", base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:                     # noqa: BLE001
            self.degraded_reasons.append(f"cannot encode {path}: {exc}")
            return None

    def describe(self, path: str | None, question: str) -> str:
        """Ask a vision model what is on screen.

        `question` carries the step's expectation, so the model is asked
        something specific and answerable rather than "describe this".
        """
        if not path or not self.s.get("vision.llm_screen_reading", True):
            return ""
        if not self.llm.supports_vision("observer"):
            self.degraded_reasons.append(
                "the configured observer LLM profile is not marked "
                "supports_vision, so screenshots were not sent to it")
            return ""
        encoded = self._encode(path)
        if encoded is None:
            return ""
        mime, b64 = encoded

        system = (
            "You are a QA observer looking at ONE screenshot of an Android "
            "phone. Report only what is VISIBLE. Never infer that an action "
            "succeeded because it 'should have'.\n"
            "Context you must account for: the screen shows Xbox Cloud Gaming "
            "(xCloud), which is a PWA running inside a mobile browser - not an "
            "installed app. Browser chrome, an address bar, a tab strip or a "
            "URL are therefore NORMAL and worth mentioning, as are overlays "
            "that would steal gamepad input (permission dialogs, an app "
            "chooser, a keyboard).\n"
            "If the screen is black, blank, or a stream placeholder, say so "
            "plainly - that is a finding, not a failure to describe."
        )
        try:
            model = self.llm.get("observer")
            from langchain_core.messages import HumanMessage, SystemMessage
            self.llm.calls += 1
            response = model.invoke([
                SystemMessage(content=system),
                HumanMessage(content=[
                    {"type": "text", "text": question},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]),
            ])
            content = getattr(response, "content", "")
            if isinstance(content, list):
                return "\n".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content).strip()
            return str(content).strip()
        except (LLMUnavailable, Exception) as exc:    # noqa: BLE001
            self.degraded_reasons.append(f"vision LLM call failed: {exc}")
            return ""

    # -- the composite observation ----------------------------------------
    def observe(self, run_id: str, label: str, question: str,
                previous_frame: str | None,
                include_logs: bool = True) -> Observation:
        """One full look at the world. Always returns an Observation - a blind
        one if every sensor is unavailable, with `degraded` set so no downstream
        agent mistakes silence for evidence."""
        obs = Observation(step_id=label, timestamp=time.time())
        sensors: list[str] = []

        path, detail = self.capture(run_id, label)
        obs.screenshot_path = path
        if path:
            sensors.append("screenshot")
        else:
            obs.notes.append(detail)

        ratio = self.diff(path, previous_frame)
        if ratio is not None:
            obs.change_ratio = round(ratio, 5)
            threshold = float(self.s.get("vision.motion_threshold", 0.01))
            obs.screen_changed = ratio >= threshold
            sensors.append("frame_diff")
            if not obs.screen_changed:
                # The finding a firmware OK can never give you.
                obs.notes.append(
                    f"screen changed by only {ratio:.4%} (threshold "
                    f"{threshold:.2%}) - the UI appears NOT to have reacted")
        elif previous_frame is None:
            obs.notes.append("no earlier frame to compare against (first look)")

        text = self.ocr(path)
        if text:
            obs.screen_text = text
            sensors.append("ocr")

        described = self.describe(path, question)
        if described:
            obs.screen_description = described
            sensors.append("vision_llm")

        if self.android and self.android.status.adb_available:
            obs.focused_window = self.android.focused_window()
            if obs.focused_window:
                sensors.append("focused_window")
            if include_logs and self.s.get("logs.logcat_enabled", True):
                raw = self.android.logcat()
                if raw:
                    lines = self.android.relevant_log_lines(raw)
                    obs.log_excerpt = "\n".join(lines)
                    sensors.append("logcat")

        obs.sensors_used = sensors
        # "Degraded" specifically means: we wanted a sensor and did not get it.
        obs.degraded = not path or (self.can_ocr and not text and bool(path))
        if not sensors:
            obs.degraded = True
            obs.notes.append(
                "NO sensors were available: this run is blind, so no claim "
                "about what the phone displayed can be made from it")
        return obs
