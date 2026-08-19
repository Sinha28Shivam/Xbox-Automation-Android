"""Reusable Android text input backend.

The agent supplies text; this class owns Android-specific encoding and command
execution. LLMs never construct shell commands.
"""

from __future__ import annotations

from .android import AndroidTool


class TextInputManager:
    """Deliver text to the currently focused Android input field."""

    def __init__(self, android: AndroidTool):
        self.android = android

    @staticmethod
    def encode(value: str) -> str:
        value = value.replace("%", "%25")
        value = value.replace(" ", "%s")
        for char in ("&", "|", "<", ">", "(", ")", ";", "#"):
            value = value.replace(char, "\\" + char)
        value = value.replace("'", "\\'")
        value = value.replace('"', '\\"')
        return value

    def type_text(self, value: str, timeout: float = 15.0) -> tuple[bool, str]:
        if not value:
            return False, "text input value is empty"
        encoded = self.encode(value)
        return self.android.shell_checked(
            f"input text '{encoded}'", timeout=timeout
        )

    def submit(self, timeout: float = 10.0) -> tuple[bool, str]:
        """Submit the focused Android field using Enter."""
        return self.android.shell_checked("input keyevent 66", timeout=timeout)
