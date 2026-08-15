"""
tools - everything the agents are allowed to touch the world with.

    pad.py      ACTUATOR  - gamepad input, via the verified ../host/pad_link.py
    android.py  SENSOR    - adb: screenshots, logcat, focused window, PWA launch
    vision.py   ANALYSIS  - frame diff, OCR, vision-LLM screen reading

The split is the safety model. An agent cannot invent a capability: it can only
call what a tool exposes, and each tool refuses what config forbids. Nothing
here imports an agent, so the dependency direction stays one-way.
"""

from .android import AndroidTool
from .pad import PadTool
from .vision import VisionTool

__all__ = ["AndroidTool", "PadTool", "VisionTool"]
