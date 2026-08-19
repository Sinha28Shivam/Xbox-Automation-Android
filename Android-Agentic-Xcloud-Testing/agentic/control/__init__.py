"""
control - the wall between what a model asks for and what the hardware is told.

`validator.py` holds the checks that used to live inside `planner._sanitise`.
Moving them here is not tidying: the closed loop introduced a SECOND producer of
actions (the decision agent), and a fence that only one producer walks through is
not a fence. Both now call the same code, so a control that the planner could
not use cannot be smuggled in one decision at a time.
"""

from .validator import ActionValidator, ValidationResult

__all__ = ["ActionValidator", "ValidationResult"]
