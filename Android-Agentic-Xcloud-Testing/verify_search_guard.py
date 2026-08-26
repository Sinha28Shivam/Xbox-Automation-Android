"""Small dependency-light regression checks for the closed-loop search guard.

These checks intentionally use only the Pydantic schemas and DecisionAgent's
pure focus predicate. They do not touch a phone, controller, ADB, or an LLM.
Run from Android-Agentic-Xcloud-Testing with:

    python verify_search_guard.py
"""

from __future__ import annotations

from agentic.agents.decision import DecisionAgent
from agentic.schemas import Focus, GameState, Observation, ScreenType


def check(name: str, value: bool, expected: bool) -> None:
    if value is not expected:
        raise AssertionError(f"{name}: expected {expected}, got {value}")
    print(f"PASS  {name}")


def main() -> None:
    # Regression case: xCloud HOME commonly contains the word "Search". That
    # must NOT be enough to authorize ADB_TEXT.
    home = GameState(
        screen_type=ScreenType.XCLOUD_HOME,
        confidence=0.90,
        visible_text=["Search", "Jump back in", "Game Pass"],
        focus=Focus(element="Jump back in", confidence=0.9),
        observation=Observation(
            screen_text="Search\nJump back in\nGame Pass",
            focused_tile="Jump back in",
            sensors_used=["screenshot", "ocr", "ui_dump"],
        ),
    )
    check("home Search label is not input focus",
          DecisionAgent._search_field_ready(home), False)

    keyboard = GameState(
        screen_type=ScreenType.KEYBOARD,
        confidence=0.90,
        focus=Focus(element="Search", confidence=0.8),
        observation=Observation(
            screen_text="Search",
            focused_tile="Search",
            sensors_used=["screenshot", "ui_dump"],
        ),
    )
    check("explicit keyboard state permits typing",
          DecisionAgent._search_field_ready(keyboard), True)

    focused_input = GameState(
        screen_type=ScreenType.OVERLAY,
        confidence=0.85,
        focus=Focus(element="Search field", confidence=0.8),
        observation=Observation(
            screen_text="Search",
            focused_tile="Search field",
            sensors_used=["screenshot", "ui_dump"],
        ),
    )
    check("accessibility focus on Search field permits typing",
          DecisionAgent._search_field_ready(focused_input), True)

    vision_only_without_focus = GameState(
        screen_type=ScreenType.OVERLAY,
        confidence=0.80,
        observation=Observation(
            screen_description="A search field is visible, but it is not focused.",
            sensors_used=["screenshot", "vision_llm"],
        ),
    )
    check("search field without focus evidence stays blocked",
          DecisionAgent._search_field_ready(vision_only_without_focus), False)

    vision_focus = GameState(
        screen_type=ScreenType.OVERLAY,
        confidence=0.80,
        observation=Observation(
            screen_description="The search field is focused and the text cursor is visible.",
            sensors_used=["screenshot", "vision_llm"],
        ),
    )
    check("explicit vision focus evidence permits typing",
          DecisionAgent._search_field_ready(vision_focus), True)

    print("\nAll search guard checks passed.")


if __name__ == "__main__":
    main()
