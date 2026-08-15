# Navigate to the game library with the controller

Deliberately written as plain markdown to show that no schema is required. The
ScenarioAgent reads this and produces the same kind of specification it would
from a YAML file.

## What I want to know

Starting from the xCloud home page, can a user move around the interface using
only the gamepad — no touchscreen at all — and open the games library?

## What should be true afterwards

- Pressing the D-pad moves the on-screen highlight, and the movement is visible.
- Pressing A activates whatever is highlighted rather than doing nothing.
- Pressing B goes back one level instead of leaving the page entirely.
- At some point a screen appears that is clearly a library or game list.

## What I do not care about here

Which game is highlighted, how the tiles are ordered, or whether any game
actually launches. Those are separate tests. This one is only about whether
gamepad navigation works.

## Things worth knowing before you plan this

The exact layout of xCloud's home page changes: it is a PWA served fresh from
the network, so the number of D-pad presses to reach the library is not fixed
and must not be assumed. Watch the screen and adapt rather than pressing a
predetermined count.

Menus animate, and the stream adds 60–100 ms on top, so leave time between
inputs. Presses sent too quickly are dropped by the UI and will look like a
failure of navigation when they are really a failure of pacing.
