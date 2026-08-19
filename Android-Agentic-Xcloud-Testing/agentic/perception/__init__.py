"""
perception - turning raw sensor readings into a structured world state.

The split from `tools/vision.py` is deliberate and is the point of this package:

    tools/vision.py     SENSORS.      Screenshot, frame diff, OCR, and a vision
                                      LLM description. Reports what is there.
                                      Knows nothing about xCloud or games.

    perception/         INTERPRETATION. Turns those readings into a `GameState`
                                      with ONE `screen_type` and a confidence.

Keeping them apart is what makes the fast/slow tiers possible at all. The old
design interpreted inside the sensor - `vision.observe()` ended with a block of
`if "minecraft" in combined:` substring tests - which meant every reading paid
for a vision-LLM call before anything could decide whether one was needed, and
the semantic result was true whenever the word appeared ANYWHERE, including in
the model's own sentence "there is no Minecraft tile visible".
"""

from .state_builder import StateBuilder

__all__ = ["StateBuilder"]
