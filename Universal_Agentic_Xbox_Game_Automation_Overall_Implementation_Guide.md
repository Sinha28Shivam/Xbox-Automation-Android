# Universal Agentic Xbox / Android / xCloud Game Automation

## Overall Analysis, Required Changes, Implementation Plan, and Universal Design

**Project context:** Arduino Leonardo USB-HID controller + FT232RL
serial link + Android/xCloud + ADB observation +
LangGraph/LLM/Vision/RCA.

**Primary objective:** Evolve the current Minecraft Dungeons/xCloud
automation into a reusable agentic framework that can handle different
games, menus, launch flows, dialogs, and eventually real-time gameplay
without rewriting the core executor for every game.

------------------------------------------------------------------------

## 1. Executive Summary

The current project does **not** need to be rebuilt from scratch.

The existing foundation is valuable:

-   Arduino Leonardo USB-HID controller
-   FT232RL serial communication
-   Android ADB for observation
-   xCloud browser/PWA handling
-   LangGraph orchestration
-   Planner/Executor architecture
-   Screenshot + frame-difference observation
-   Vision/OCR
-   GLANCE + SETTLE observations
-   RCA
-   reporting
-   controller capability validation

The major problem is the **control philosophy**.

The current system can still behave approximately like:

``` text
Scenario
  ↓
LLM generates a sequence
  ↓
Executor executes the sequence
  ↓
Observe/Judge
```

That approach is fragile because game/UI state changes after every
input.

The target architecture is:

``` text
OBSERVE
  ↓
BUILD STRUCTURED STATE
  ↓
DECIDE ONE ACTION
  ↓
VALIDATE ACTION
  ↓
EXECUTE ONE ACTION
  ↓
GLANCE
  ↓
SETTLE
  ↓
BUILD NEW STATE
  ↓
VERIFY TRANSITION
  ↓
SUCCESS / CONTINUE / RECOVER / RCA
```

This is the most important change in the entire project.

The existing implementation plan already establishes this principle and
identifies one-action closed-loop control, structured GameState,
transition verification, action validation, recovery, confidence, fast
perception, and hierarchical gameplay control as the highest priorities.

------------------------------------------------------------------------

# 2. Evidence From the Current Minecraft Dungeons Run

The supplied run log is extremely useful because it proves several
important things.

## 2.1 Hardware initialization succeeded

The run opened the controller link successfully:

``` text
pad link OPEN
port=COM8
firmware=xcloudpad-usb-1.0
transport=usb
host_enumerated=True
```

Therefore the Leonardo/HID path was alive at the beginning of the run.

## 2.2 xCloud loaded correctly

The run successfully opened:

``` text
https://www.xbox.com/play
```

and the settled screenshot confirmed the xCloud interface with game
tiles.

## 2.3 Controller input reaches xCloud

The Guide/B probing produced visible reactions.

The run also showed:

``` text
hardware_ok=True
```

for the physical controller actions.

More importantly, visual observations showed reactions after the
commands.

## 2.4 RIGHT successfully focused Minecraft Dungeons

The run initially used a potentially multi-press navigation request, but
the executor capped it to one press:

``` text
Capping right times from 3 to 1
(enforcing single-step closed-loop navigation)
```

After the RIGHT action, the GLANCE frame showed a 19.14% visual change
and the Minecraft Dungeons tile had the visible white focus border.

The subsequent observation also confirmed that Minecraft Dungeons was
focused.

This is strong evidence that the closed-loop navigation direction is
correct.

## 2.5 A produced a major xCloud transition

The important step was:

``` text
STEP [10/23] select_minecraft_dungeons press a

intent:
Select the Minecraft Dungeons tile to open its detail page

expects:
Transition begins to Minecraft Dungeons detail page
```

The physical A command was accepted:

``` text
hardware_ok=True
```

Then:

``` text
glance changed=True ratio=47.48%
```

The screen became almost black and displayed the browser/full-screen
transition instruction.

The settled frame then showed a green/teal Xbox loading animation.

The Judge itself stated that both frames showed significant changes,
confirming that the input reached xCloud.

Therefore the important conclusion is:

> The Step 10 failure is not evidence that the A command failed at the
> Arduino/HID layer.

The stronger diagnosis is:

``` text
A input
  ↓
xCloud reacted
  ↓
fullscreen transition
  ↓
game loading
  ↓
Judge expected detail page
  ↓
Judge declared NOT MET
  ↓
RCA
```

The automation therefore failed partly because the **expected state
model was too rigid**.

------------------------------------------------------------------------

# 3. The Core Bug in the Current Minecraft Dungeons Scenario

The current scenario assumes:

``` text
Starting screen
  ↓
A
  ↓
Minecraft Dungeons detail page
  ↓
A
  ↓
Play
  ↓
Game loading
```

But the observed device behavior showed:

``` text
Starting screen
  ↓
Minecraft Dungeons focused
  ↓
A
  ↓
Fullscreen transition
  ↓
Xbox/game loading
```

The scenario therefore contains an assumption that must be removed:

> Pressing A on a game tile must open a detail page.

That may be true for some xCloud UI paths, but it is not safe as a
universal rule.

The universal agent must accept multiple valid transitions.

For example:

``` text
GAME_FOCUSED
    ↓ A
    ├── GAME_DETAIL
    ├── FULLSCREEN_TRANSITION
    ├── GAME_LOADING
    ├── LIVE_GAME_STREAM
    └── GAME_SPLASH
```

If `GAME_DETAIL` appears:

``` text
GAME_DETAIL
    ↓
find Play
    ↓
A
```

If `FULLSCREEN_TRANSITION` appears:

``` text
FULLSCREEN_TRANSITION
    ↓
WAIT
```

If `GAME_LOADING` appears:

``` text
GAME_LOADING
    ↓
WAIT
```

The agent must not send another A simply because the detail page did not
appear.

------------------------------------------------------------------------

# 4. Universal Architecture

The target system should be:

``` text
                         USER GOAL
                             │
                             ▼
                    ┌────────────────┐
                    │ Scenario Agent │
                    └───────┬────────┘
                            │
                            ▼
                    ┌────────────────┐
                    │ World Observer │
                    │                │
                    │ Screenshot     │
                    │ OCR            │
                    │ CV             │
                    │ Vision LLM     │
                    │ ADB            │
                    └───────┬────────┘
                            │
                            ▼
                    ┌────────────────┐
                    │ State Builder  │
                    └───────┬────────┘
                            │
                            ▼
                    ┌────────────────┐
                    │ Decision Agent │
                    │ ONE action     │
                    └───────┬────────┘
                            │
                            ▼
                    ┌────────────────┐
                    │ Action         │
                    │ Validator      │
                    └───────┬────────┘
                            │
                            ▼
                    ┌────────────────┐
                    │ Executor       │
                    └───────┬────────┘
                            │
                            ▼
                    Arduino Leonardo
                            │
                            ▼
                         USB HID
                            │
                            ▼
                         Android
                            │
                            ▼
                          xCloud
                            │
                            ▼
                    ┌────────────────┐
                    │ Re-observe     │
                    └───────┬────────┘
                            │
                            ▼
                    ┌────────────────┐
                    │ Verify         │
                    └───────┬────────┘
                            │
                 ┌──────────┴──────────┐
                 ▼                     ▼
              SUCCESS               FAILURE
                 │                     │
                 ▼                     ▼
            Goal Check               RCA
                                       │
                                       ▼
                                   Recovery
                                       │
                                       └──────► Observe
```

The LLM should **not** be the raw controller.

The roles should be:

``` text
LLM
= reasoning / planning / interpretation

Vision
= perception

StateBuilder
= world model

DecisionAgent
= next-action selection

Validator
= safety/capability boundary

Executor
= hardware control

Evaluator
= transition verification

RCA
= diagnosis

Recovery
= adaptation

Memory
= history/learning

Local Controller
= real-time gameplay
```

------------------------------------------------------------------------

# 5. One-Action Closed Loop --- P0 Requirement

This is the highest-priority implementation.

## Required

``` text
OBSERVE
  ↓
BUILD STATE
  ↓
DECIDE ONE ACTION
  ↓
VALIDATE
  ↓
EXECUTE
  ↓
GLANCE
  ↓
SETTLE
  ↓
BUILD NEW STATE
  ↓
VERIFY TRANSITION
  ↓
CONTINUE / RECOVER
```

## Do not

``` text
LLM
  ↓
RIGHT
RIGHT
DOWN
RIGHT
A
A
LEFT
...
  ↓
execute everything
  ↓
observe at the end
```

Unless the sequence is a previously verified deterministic macro with
verified preconditions.

## Why?

Suppose:

``` text
State A:
focus = Game A
```

After RIGHT:

``` text
State B:
focus = Game B
```

Another RIGHT may produce:

``` text
State C:
focus = Game C
```

But if the first action instead produced an unexpected overlay, the
second action could interact with the wrong UI.

Therefore:

> The observed state is the source of truth, not the LLM's previous
> prediction.

------------------------------------------------------------------------

# 6. How the Agent Knows "DOWN 4 Times / 5 Times / N Times"

Do not make the LLM guess a fixed count and execute it blindly.

Use closed-loop navigation.

Example:

``` text
Goal:
Select Minecraft Dungeons

Current:
A Plague Tale focused

Decision:
RIGHT
```

Execute:

``` text
RIGHT
```

Observe:

``` text
Minecraft Dungeons focused
```

Stop.

If it is still not focused:

``` text
Current:
Forza focused

Decision:
RIGHT
```

Then observe again.

So the real loop is:

``` text
while target_not_reached:

    observe()
    determine_focus()
    choose_direction()
    press_once()
    observe()
    verify_focus()
```

The number of actions is therefore an **output of observed state
transitions**, not a hardcoded input.

This is one of the most important properties required for different
screen layouts and different games.

------------------------------------------------------------------------

# 7. Structured GameState

Do not make:

``` json
{
  "screen_changed": true
}
```

the primary state.

Create a `GameState`.

Example:

``` json
{
  "application": "xcloud",
  "screen_type": "library",
  "visible_text": [
    "Minecraft",
    "Minecraft Dungeons",
    "Forza"
  ],
  "focus": {
    "element": "Minecraft Dungeons",
    "confidence": 0.95
  },
  "loading": false,
  "overlay_present": false,
  "controller_prompt": false,
  "error_present": false,
  "game_running": false,
  "confidence": 0.95
}
```

For a game:

``` json
{
  "application": "minecraft_dungeons",
  "screen_type": "main_menu",
  "visible_text": [
    "Play",
    "Options",
    "Marketplace"
  ],
  "focus": {
    "element": "Play",
    "confidence": 0.93
  },
  "loading": false,
  "error_present": false,
  "confidence": 0.94
}
```

For loading:

``` json
{
  "application": "xcloud",
  "screen_type": "game_loading",
  "loading": true,
  "game_target": "Minecraft Dungeons",
  "fullscreen": true,
  "error_present": false,
  "confidence": 0.91
}
```

------------------------------------------------------------------------

# 8. Universal State Taxonomy

The core framework should recognize generic states such as:

``` text
UNKNOWN

ANDROID_HOME
BROWSER
XCLOUD_HOME
XCLOUD_LIBRARY

GAME_FOCUSED
GAME_DETAIL

FULLSCREEN_TRANSITION
GAME_LOADING
GAME_CONNECTING

LIVE_GAME_STREAM
GAME_SPLASH
PRESS_ANY_BUTTON

GAME_MAIN_MENU
GAME_PAUSE_MENU
GAME_SETTINGS
GAME_INVENTORY

DIALOG
OVERLAY
KEYBOARD
TEXT_FIELD

CONTROLLER_PROMPT
LOGIN
SESSION_EXPIRED

QUEUE
NETWORK_WAIT
STREAM_ERROR

IN_GAME
```

Games can add specialized states later.

------------------------------------------------------------------------

# 9. State Transition Verification

Every action should be judged as:

``` text
STATE BEFORE
     +
ACTION
     +
STATE AFTER
     =
EXPECTED TRANSITION?
```

Example:

``` text
Before:
focus = A Plague Tale

Action:
RIGHT

After:
focus = Minecraft Dungeons

Expected:
focus moves right

Result:
SUCCESS
```

For A:

``` text
Before:
focus = Minecraft Dungeons

Action:
A

After:
screen = GAME_LOADING

Expected:
launch transition

Result:
SUCCESS / INTERMEDIATE
```

Not:

``` text
After != expected detail page
→ FAILURE
```

The evaluator must understand valid intermediate states.

------------------------------------------------------------------------

# 10. GLANCE + SETTLE

Keep the existing two-stage observation system.

``` text
ACTION
  ↓
GLANCE
  ↓
SETTLE
  ↓
VERIFY
```

This is already valuable.

The run proved that some controller reactions are visible in the GLANCE
frame and disappear by SETTLE.

For example, the RIGHT action had a visible 19.14% change in GLANCE,
while the settled frame had very little pixel change.

Therefore:

> A reaction that appears only in GLANCE is still valid evidence.

However, GLANCE + SETTLE must feed the StateBuilder.

It should not be used only as:

``` text
changed = true/false
```

------------------------------------------------------------------------

# 11. Do Not Use Frame Difference as the Main Success Signal

Frame difference tells you:

``` text
Did pixels change?
```

It does not tell you:

``` text
Did the requested action produce the required state transition?
```

Cloud gaming constantly produces pixel changes because of:

-   streaming
-   compression
-   animation
-   loading
-   game effects
-   camera movement

Therefore:

``` text
frame_changed = true
```

does not automatically mean:

``` text
action_success = true
```

Likewise:

``` text
frame_changed = false
```

does not automatically mean:

``` text
action_failed = true
```

The primary signal should be:

``` text
STATE BEFORE
→ ACTION
→ STATE AFTER
```

------------------------------------------------------------------------

# 12. Hardware Result Must Remain Separate From Application Result

Keep:

``` text
hardware_ok
```

separate from:

``` text
application_reacted
```

and:

``` text
expectation_met
```

Correct interpretation:

``` text
hardware_ok = true
application_reacted = false
```

means:

> The controller layer accepted the command, but the application did not
> show the expected transition.

This can indicate:

-   wrong focus
-   overlay
-   dialog
-   UI not ready
-   wrong action
-   timing issue
-   game state mismatch
-   controller ignored
-   perception error

It should not immediately trigger:

``` text
Arduino failure
```

------------------------------------------------------------------------

# 13. Universal Judge

The Judge should return something like:

``` json
{
  "classification": "INTERMEDIATE",
  "confidence": 0.91,
  "state_before": "GAME_FOCUSED",
  "action": "A",
  "state_after": "GAME_LOADING",
  "expected_transition": [
    "GAME_DETAIL",
    "FULLSCREEN_TRANSITION",
    "GAME_LOADING"
  ],
  "transition_valid": true,
  "goal_complete": false,
  "next_recommendation": "WAIT"
}
```

Possible classifications:

``` text
SUCCESS
INTERMEDIATE
FAILURE
UNKNOWN
```

This is much better than only:

``` text
MET
NOT_MET
```

------------------------------------------------------------------------

# 14. Recovery/RCA System

Create explicit failure classes:

``` text
ACTION_FAILED
INPUT_IGNORED
STATE_UNKNOWN
UI_NOT_READY
FOCUS_WRONG
OVERLAY_PRESENT
DIALOG_PRESENT
KEYBOARD_PRESENT
GAME_LOADING
NETWORK_WAIT
CONTROLLER_NOT_DETECTED
VISION_UNCERTAIN
WRONG_ACTION
GOAL_NOT_REACHED
STREAM_ERROR
SESSION_EXPIRED
QUEUE_TIMEOUT
```

Recovery examples:

``` text
UI_NOT_READY
  ↓
WAIT
  ↓
OBSERVE
```

``` text
STATE_UNKNOWN
  ↓
NEW SCREENSHOT
  ↓
VISION
```

``` text
FOCUS_WRONG
  ↓
DECIDE NAVIGATION
  ↓
ONE INPUT
```

``` text
GAME_LOADING
  ↓
WAIT
  ↓
OBSERVE
```

``` text
INPUT_IGNORED
  ↓
CHECK OVERLAY
  ↓
CHECK STATE
  ↓
RETRY ONCE
```

Do not retry indefinitely.

Recommended:

``` text
attempt 1
  ↓
verify
  ↓
recover
  ↓
attempt 2
  ↓
verify
  ↓
RCA / stop
```

------------------------------------------------------------------------

# 15. Confidence Model

Every state should have confidence.

Recommended starting thresholds:

``` text
>= 0.85
    act normally

0.60 - 0.84
    observe again / use stronger perception

< 0.60
    Vision LLM / additional evidence
```

Example:

``` text
focus = Minecraft Dungeons
confidence = 0.95
```

The agent can safely navigate.

But:

``` text
focus = unknown
confidence = 0.42
```

should cause another observation rather than a random controller action.

------------------------------------------------------------------------

# 16. Fast Perception + Vision LLM

The current run is slow.

The full run reached 517.8 seconds while only executing 10 steps and
making 32 LLM calls.

The deliberate waits were only about 31.6 seconds, approximately 6% of
the runtime.

This indicates that a major performance problem is the observation/LLM
pipeline, not only sleep/wait values.

Use two levels of perception.

## Level 1 --- Fast perception

``` text
Screenshot
  ↓
OCR
  ↓
CV/basic image analysis
  ↓
StateBuilder
```

Use it for:

-   OCR
-   visible text
-   focus border
-   loading screen
-   dialog
-   fullscreen transition
-   basic screen regions
-   large state changes

## Level 2 --- Vision LLM

Call only when:

-   confidence is low
-   screen is unfamiliar
-   multiple interpretations are possible
-   fast perception cannot classify the state

Do not send every screenshot to a Vision LLM.

------------------------------------------------------------------------

# 17. Performance Target

Current behavior:

``` text
controller action
  ↓
observation
  ↓
large LLM delay
```

For menu navigation, the desired future flow is closer to:

``` text
PRESS RIGHT
  ↓
~100-500 ms reaction window
  ↓
fast perception
  ↓
state update
  ↓
next action
```

Vision LLM should be the fallback, not the first detector for every
action.

For loading:

``` text
WAIT 2-3 seconds
OBSERVE
```

For a fast UI:

``` text
WAIT 100-500 ms
OBSERVE
```

For a game boot:

``` text
WAIT several seconds
OBSERVE
```

Timing should depend on the state/action rather than one global delay.

------------------------------------------------------------------------

# 18. Universal Scenario Design

A scenario should define:

### Goal

``` text
Reach Minecraft Dungeons main menu.
```

### Preconditions

``` text
controller connected
ADB available for observation
xCloud signed in
```

### Constraints

``` text
gamepad only
no ADB input
```

### Success

``` text
Minecraft Dungeons main menu detected
```

### Time budget

``` text
maximum runtime
```

The scenario should **not** define a long hardcoded controller route.

------------------------------------------------------------------------

# 19. Universal Minecraft Dungeons Scenario

A corrected scenario should allow both direct-launch and detail-page
paths.

Conceptually:

``` text
XCLOUD_HOME
   ↓
locate Minecraft Dungeons
   ↓
one directional action at a time
   ↓
GAME_FOCUSED
   ↓
A
   ↓
┌──────────────────────────────┐
│                              │
▼                              ▼
GAME_DETAIL              FULLSCREEN_TRANSITION
│                              │
A                              │
│                              ▼
└───────────────┐          GAME_LOADING
                │              │
                └──────┬───────┘
                       ▼
                 LIVE_GAME_STREAM
                       │
                       ▼
                  GAME_SPLASH
                       │
                    A if needed
                       │
                       ▼
               GAME_MAIN_MENU
                       │
                       ▼
                     PASS
```

The exact path is determined by observation.

------------------------------------------------------------------------

# 20. Never Use `A*2` Blindly

The current scenario has a concept like:

``` text
PRESS A*2
```

This is unsafe for a universal agent.

Instead:

``` text
PRESS A
OBSERVE
```

Then decide.

Why?

If:

``` text
Minecraft Dungeons focused
```

and A causes:

``` text
GAME_LOADING
```

a second A might interact with the loading/game screen.

The correct behavior is:

``` text
A
 ↓
GAME_LOADING
 ↓
WAIT
```

not:

``` text
A
A
```

------------------------------------------------------------------------

# 21. Text Input Problem

The controller is a gamepad. A standard gamepad does not inherently
provide arbitrary text characters.

Therefore the framework needs an **input modality layer**.

Possible capabilities:

``` text
GAMEPAD
KEYBOARD
TOUCH
TEXT
```

For a controller-only scenario:

``` text
TEXT_FIELD detected
  ↓
controller-compatible virtual keyboard
  ↓
navigate characters with D-pad
  ↓
A to select
```

This works but can be slow.

A better hardware option, if the project permits it, is a Leonardo
composite HID implementation exposing:

``` text
Gamepad HID
+
Keyboard HID
```

Then the agent can choose:

``` text
GAMEPAD → menus/gameplay
KEYBOARD → text fields
```

But this must be an explicit capability and scenario constraint.

Do not secretly use ADB text injection when the scenario claims to
validate controller-only behavior.

------------------------------------------------------------------------

# 22. Action Schema

The LLM should never generate raw serial/HID protocol commands.

Use a canonical schema:

``` json
{
  "type": "PRESS",
  "control": "dpad_right",
  "duration": null
}
```

Other examples:

``` json
{
  "type": "PRESS",
  "control": "a"
}
```

``` json
{
  "type": "HOLD",
  "control": "lt",
  "duration": 0.5
}
```

``` json
{
  "type": "STICK",
  "control": "left_stick",
  "x": 0.75,
  "y": 0.0,
  "duration": 0.25
}
```

``` json
{
  "type": "WAIT",
  "duration": 2.0
}
```

``` json
{
  "type": "OBSERVE"
}
```

------------------------------------------------------------------------

# 23. Action Validator

Every LLM action must pass:

``` text
LLM action
  ↓
Schema validation
  ↓
Capability validation
  ↓
Safety validation
  ↓
Timing validation
  ↓
Executor
```

Validate:

-   action type
-   control name
-   hardware capability
-   stick range
-   trigger range
-   duration
-   prohibited controls
-   current scenario restrictions
-   maximum action duration

------------------------------------------------------------------------

# 24. Macro System

Macros are allowed only after state verification.

Bad:

``` text
RIGHT
RIGHT
DOWN
RIGHT
A
```

Good:

``` text
IF:
screen = known_menu
focus = known_item

THEN:
execute verified macro
```

Every macro needs:

``` text
precondition
actions
postcondition
timeout
failure handling
```

If the postcondition fails:

``` text
STOP MACRO
  ↓
OBSERVE
  ↓
RECOVER
```

------------------------------------------------------------------------

# 25. Memory

Add short-term action memory.

Store:

``` text
previous_state
action
result
new_state
confidence
```

Example:

``` text
State A:
focus = A Plague Tale

Action:
RIGHT

State B:
focus = Minecraft Dungeons

Confidence:
0.93
```

This can help the agent learn UI transitions without hardcoding
coordinates.

Long-term learned policies should be added only after the generic
closed-loop system is stable.

------------------------------------------------------------------------

# 26. Game Adapters

The core system should remain generic.

Optional adapters can add specialized knowledge:

``` text
generic
minecraft
racing
fps
platformer
```

Example:

``` text
Generic:
object detected at upper-right

FPS adapter:
enemy detected at upper-right
```

The adapter adds semantics.

It must not replace the universal controller, observation, validation,
or recovery architecture.

------------------------------------------------------------------------

# 27. Real-Time Gameplay Requires a Different Control Layer

The universal UI/game-launch agent can use an LLM at each decision.

Real-time gameplay is different.

Do NOT do:

``` text
LLM
 ↓
LEFT STICK
 ↓
wait
 ↓
LLM
 ↓
LEFT STICK
```

at every frame.

LLM latency is too high for:

-   FPS aiming
-   racing
-   platformers
-   combat
-   dodging
-   steering
-   continuous movement

Use hierarchical control:

``` text
LLM
 ↓
High-level objective
"Move toward the door"
 ↓
Game State
 ↓
Local Controller
 ↓
left stick / buttons
 ↓
observe
 ↓
local adjustment
```

The LLM handles strategy.

The local controller handles fast movement.

------------------------------------------------------------------------

# 28. Universal LangGraph Target

Recommended graph:

``` text
START
  ↓
DEVICE_CHECK
  ↓
SCENARIO_CHECK
  ↓
INITIAL_OBSERVATION
  ↓
BUILD_STATE
  ↓
GOAL_CHECK
  │
  ├── complete → REPORT
  │
  └── incomplete
          ↓
     DECIDE_ACTION
          ↓
     VALIDATE_ACTION
          ↓
     EXECUTE_ACTION
          ↓
        GLANCE
          ↓
        SETTLE
          ↓
     BUILD_NEW_STATE
          ↓
       VERIFY
       /     \
   success   failure
      │         │
      ▼         ▼
 GOAL_CHECK    RCA
                 ↓
              RECOVERY
                 ↓
              OBSERVE
                 │
                 └────→ DECIDE_ACTION
```

------------------------------------------------------------------------

# 29. Recommended File Structure

``` text
agentic/
│
├── agents/
│   ├── scenario.py
│   ├── observer.py
│   ├── decision.py
│   ├── evaluator.py
│   ├── recovery.py
│   ├── rca.py
│   └── reporter.py
│
├── perception/
│   ├── screenshot.py
│   ├── ocr.py
│   ├── vision.py
│   ├── state_builder.py
│   └── focus_detector.py
│
├── control/
│   ├── actions.py
│   ├── validator.py
│   ├── policy.py
│   └── controller.py
│
├── hardware/
│   ├── pad.py
│   ├── pad_link.py
│   └── android.py
│
├── memory/
│   ├── state_history.py
│   └── action_history.py
│
├── scenarios/
│   └── cases/
│
├── adapters/
│   ├── generic.py
│   ├── minecraft.py
│   ├── racing.py
│   └── fps.py
│
├── graph.py
└── cli.py
```

Do not rewrite the hardware layer unless a real hardware defect is
demonstrated.

------------------------------------------------------------------------

# 30. Logging Requirements

Every action should generate a complete record.

Example:

``` json
{
  "timestamp": "...",
  "state_before": "...",
  "action": {
    "type": "PRESS",
    "control": "dpad_right"
  },
  "hardware_ok": true,
  "glance_state": "...",
  "settled_state": "...",
  "expected_transition": "...",
  "actual_transition": "...",
  "result": "success",
  "confidence": 0.94,
  "duration": 0.63,
  "retry_count": 0
}
```

This makes manual-vs-agent debugging possible.

------------------------------------------------------------------------

# 31. Manual-vs-Agent Diagnostic Mode

Add a diagnostic mode.

Test:

``` text
manual:
RIGHT
```

Record:

``` text
before
RIGHT
after
```

Then:

``` text
agent:
RIGHT
```

Record:

``` text
before
RIGHT
after
```

Compare:

``` text
same hardware action
different behavior
```

Then inspect:

-   preceding state
-   timing
-   UI readiness
-   queued actions
-   focus
-   overlay
-   observation timing
-   planner output

This is especially important because the original issue was:

``` text
manual command works
LLM command does not appear to work
```

------------------------------------------------------------------------

# 32. Current Run Performance Problem

The run ended after approximately:

``` text
517.8 seconds
```

with:

``` text
32 LLM calls
10 steps
0 replans
```

Only about:

``` text
31.6 seconds
```

were deliberate waits.

Therefore, do not simply increase/decrease sleeps as the primary
optimization.

The architecture needs:

1.  fast perception
2.  fewer unnecessary LLM calls
3.  structured state
4.  state caching
5.  LLM only for ambiguity
6.  action-specific observation windows
7.  state-aware waiting

The current run spent large amounts of time in
observation/Judge/RCA/evaluator/reporting.

The system should not run full RCA immediately after a valid
intermediate transition.

------------------------------------------------------------------------

# 33. Correct Handling of the Current Step 10

Current:

``` text
STATE:
GAME_FOCUSED

ACTION:
A

GLANCE:
FULLSCREEN_TRANSITION

SETTLE:
GAME_LOADING

CURRENT JUDGE:
NOT_MET
```

Correct:

``` text
STATE:
GAME_FOCUSED

ACTION:
A

GLANCE:
FULLSCREEN_TRANSITION

SETTLE:
GAME_LOADING

TRANSITION:
VALID

CLASSIFICATION:
INTERMEDIATE

NEXT ACTION:
WAIT

DO NOT:
RCA
DO NOT:
press A again
```

Then:

``` text
WAIT
 ↓
OBSERVE
 ↓
GAME_LOADING?
  ├── yes → wait again
  ├── GAME_SPLASH → continue
  ├── GAME_MAIN_MENU → PASS
  └── STREAM_ERROR → RCA
```

This is the direct fix for the failure shown in the supplied run.

------------------------------------------------------------------------

# 34. Revised Universal Minecraft Dungeons Scenario

The scenario should conceptually be:

``` yaml
id: minecraft_dungeons_launch

goal:
  type: reach_main_menu
  game: Minecraft Dungeons

constraints:
  controller_input_only: true
  adb_input: false
  adb_observation: true
  typing_required: false

preconditions:
  - controller_connected
  - phone_awake
  - xcloud_available
  - account_ready

target:
  game: Minecraft Dungeons

navigation:
  method: closed_loop
  maximum_consecutive_navigation_actions: 1

launch:
  allowed_initial_transitions:
    - GAME_DETAIL
    - FULLSCREEN_TRANSITION
    - GAME_LOADING
    - LIVE_GAME_STREAM
    - GAME_SPLASH

success:
  state: GAME_MAIN_MENU

failure:
  - STREAM_ERROR
  - SESSION_EXPIRED
  - QUEUE_TIMEOUT

recovery:
  max_attempts: 2
```

The exact button sequence should be generated from state, not hardcoded
into this file.

------------------------------------------------------------------------

# 35. What Must NOT Be Done

## Do not hardcode routes

Bad:

``` text
RIGHT RIGHT DOWN RIGHT A
```

## Do not hardcode coordinates

Bad:

``` text
click(723, 442)
```

## Do not treat frame difference as success

Bad:

``` text
screen_changed = true
→ PASS
```

## Do not trust firmware OK as game success

Bad:

``` text
hardware_ok = true
→ PASS
```

## Do not generate long uncontrolled sequences

Bad:

``` text
RIGHT RIGHT RIGHT DOWN DOWN A A B...
```

## Do not blindly retry

Bad:

``` text
RIGHT
failed
RIGHT
failed
RIGHT
failed
...
```

## Do not use ADB input in a controller-only test

ADB can be used for observation, but not for controller behavior
validation.

## Do not let the LLM directly control raw hardware protocol

LLM → validated action schema → executor.

## Do not ignore uncertainty

Low confidence must trigger more observation.

## Do not hide the failure layer

Failures should identify whether the likely layer is:

``` text
vision
decision
timing
hardware
application
network
game
```

------------------------------------------------------------------------

# 36. What MUST Be Done

Always:

``` text
Observe before acting when state is unknown.

Send one action in closed-loop mode.

Observe after important actions.

Verify the expected transition.

Keep hardware and application results separate.

Use confidence.

Use recovery instead of blind retry.

Stop on repeated uncertainty.

Use verified macros only.

Use local controllers for real-time gameplay.

Keep scenario logic separate from hardware logic.

Log every decision and observation.
```

------------------------------------------------------------------------

# 37. Implementation Priority

## P0 --- Must implement first

### P0.1 One-action closed loop

Replace multi-action execution with:

``` text
observe
→ decide
→ execute one
→ observe
```

### P0.2 Structured GameState

Create:

``` text
Observation → GameState
```

### P0.3 State transition evaluator

Implement:

``` text
before_state
+
action
+
after_state
```

returning:

``` text
SUCCESS
INTERMEDIATE
FAILURE
UNKNOWN
```

### P0.4 Action schema + validator

LLM cannot generate raw controller commands.

------------------------------------------------------------------------

## P1 --- Reliability

### P1.1 Recovery/RCA integration

RCA should happen only after transition classification says a genuine
failure occurred.

### P1.2 Confidence

Every state gets confidence.

### P1.3 Fast perception

CV/OCR first.

Vision LLM only when needed.

### P1.4 Manual-vs-agent diagnostic mode

Directly compare physical actions.

------------------------------------------------------------------------

## P2 --- Optimization

### P2.1 Verified macros

Only after preconditions are known.

### P2.2 Memory

Remember state/action transitions.

### P2.3 Local gameplay controller

For continuous gameplay.

------------------------------------------------------------------------

## P3 --- Specialization

### P3.1 Game adapters

Examples:

``` text
Minecraft
FPS
Racing
Platformer
```

### P3.2 Long-term learned policies

Only after the generic system is reliable.

------------------------------------------------------------------------

# 38. Acceptance Tests

## Test 1 --- Menu navigation

Goal:

``` text
Open Settings
```

Must work without fixed coordinates.

## Test 2 --- Minecraft Dungeons

Goal:

``` text
Reach Minecraft Dungeons main menu
```

Controller input must use the Leonardo HID path.

## Test 3 --- Unknown menu

The agent should navigate an unfamiliar menu using observation.

## Test 4 --- Dialog interruption

``` text
detect dialog
→ handle dialog
→ return to goal
```

## Test 5 --- Loading screen

``` text
detect loading
→ WAIT
```

Do not repeatedly press controls.

## Test 6 --- Ignored input

If:

``` text
hardware_ok = true
```

but no expected transition occurs:

``` text
do not immediately declare hardware failure
```

Run recovery/RCA.

## Test 7 --- Vision uncertainty

If confidence is low:

``` text
re-observe
```

## Test 8 --- Game video

Frame changes must not automatically be treated as input success.

## Test 9 --- Direct-launch path

If selecting a game goes directly to fullscreen/loading:

``` text
accept it as a valid launch transition
```

## Test 10 --- Detail-page path

If selecting a game opens a detail page:

``` text
detect Play
→ A
→ loading
```

Both paths must work with the same core agent.

------------------------------------------------------------------------

# 39. Definition of "Universal"

The framework is successful when:

### Game independence

A new game can be added without rewriting the core executor.

### Scenario independence

A new goal can be described without hardcoding controller sequences.

### Hardware abstraction

The agent operates through capabilities.

### Observation abstraction

The system can combine:

``` text
OCR
CV
Vision LLM
ADB
```

without depending exclusively on one sensor.

### Recovery

The system can recover from common failures without restarting
everything.

### Evidence

Every decision is explainable from:

``` text
state
action
observation
transition
```

------------------------------------------------------------------------

# 40. Final Recommended Architecture

``` text
                  ┌──────────────────┐
                  │      GOAL        │
                  └────────┬─────────┘
                           ↓
                  ┌──────────────────┐
                  │       LLM        │
                  │ reasoning/goal   │
                  └────────┬─────────┘
                           ↓
                  ┌──────────────────┐
                  │   WORLD STATE    │
                  └────────┬─────────┘
                           ↓
                  ┌──────────────────┐
                  │    ONE ACTION    │
                  └────────┬─────────┘
                           ↓
                  ┌──────────────────┐
                  │    VALIDATOR     │
                  └────────┬─────────┘
                           ↓
                  ┌──────────────────┐
                  │ LOCAL CONTROLLER │
                  └────────┬─────────┘
                           ↓
                  ┌──────────────────┐
                  │ ARDUINO / HID    │
                  └────────┬─────────┘
                           ↓
                          GAME
                           ↓
                  ┌──────────────────┐
                  │    OBSERVE       │
                  └────────┬─────────┘
                           ↓
                  ┌──────────────────┐
                  │ VERIFY TRANSITION│
                  └────────┬─────────┘
                           ↓
                    ┌──────┴──────┐
                    │             │
                 SUCCESS        FAILURE
                    │             │
                    ↓             ↓
                NEXT GOAL         RCA
                                  ↓
                               RECOVER
                                  ↓
                               OBSERVE
```

------------------------------------------------------------------------

# 41. Final Decision

## Do not rebuild:

-   Arduino Leonardo HID
-   FT232RL
-   USB transport
-   Android ADB screenshot mechanism
-   xCloud launch mechanism
-   LangGraph
-   existing GLANCE + SETTLE concept
-   existing RCA/reporting foundation

## Change:

``` text
OLD:
LLM → many actions → execute → observe

NEW:
Observe → State → ONE action → Execute → Observe → State → Verify
```

## Add:

``` text
Structured GameState
+
Transition Evaluator
+
Intermediate States
+
Action Validator
+
Confidence
+
Recovery
+
Memory
+
Fast Perception
+
Hierarchical Gameplay Controller
+
Scenario/Hardware separation
```

------------------------------------------------------------------------

# 42. Immediate Changes for the Next Code Revision

Implement these in this exact order:

1.  **Stop treating "expected detail page" as the only valid result of
    A.**
2.  Add `FULLSCREEN_TRANSITION`.
3.  Add `GAME_LOADING`.
4.  Add `GAME_SPLASH`.
5.  Change Judge result from only MET/NOT_MET to:
    -   SUCCESS
    -   INTERMEDIATE
    -   FAILURE
    -   UNKNOWN
6.  If A produces `GAME_LOADING`, perform WAIT instead of RCA.
7.  Remove blind `A*2`.
8.  Keep navigation to one directional action at a time.
9.  Add `GameState`.
10. Add before/action/after transition verification.
11. Add confidence to state detection.
12. Add fast perception before Vision LLM.
13. Do not run full RCA on valid intermediate states.
14. Add action retry limits.
15. Add manual-vs-agent diagnostic mode.
16. Make scenario definitions describe goals and valid states rather
    than fixed button sequences.
17. Add the input modality abstraction for future text-entry cases.
18. Keep real-time gameplay control separate from high-level LLM
    reasoning.
19. Add verified macros only after the closed-loop system works.
20. Test Minecraft Dungeons again before adding another game.

------------------------------------------------------------------------

# 43. Bottom Line

The current project is already much closer to the target than the failed
run makes it appear.

The run demonstrated:

``` text
Controller initialized       ✓
xCloud loaded                ✓
Guide/B input reacted        ✓
RIGHT navigation worked      ✓
Minecraft Dungeons focused   ✓
A reached xCloud             ✓
Fullscreen transition        ✓
Game loading detected        ✓
Correct final-state handling ✗
```

The main architectural weakness exposed by the run is therefore **not
simply controller input**.

It is:

``` text
perception
   +
state representation
   +
transition verification
   +
goal-aware judging
```

The most important design rule for the next implementation is:

> **Never ask only "Did the screen look like what I expected?" Ask
> "Given the state before, the action sent, and the state after, was
> this a valid transition toward the goal?"**

That rule is what allows the same core agent to handle Minecraft
Dungeons, another xCloud game, an unknown menu, a dialog, a loading
screen, or a different launch path without rewriting the controller
logic.

------------------------------------------------------------------------

# 44. Source Basis

This guide consolidates:

1.  The existing
    `Universal_Agentic_Game_Automation_Implementation_Plan.md`.
2.  The supplied `run.log` from the Minecraft Dungeons execution.
3.  The Minecraft Dungeons scenario prompt supplied during this
    conversation.
4.  The observed screenshot sequence showing xCloud home, Minecraft
    Dungeons focus, fullscreen transition, and game loading.
5.  The design discussion from the current conversation about text
    input, N-times navigation, Judge behavior, state tracking, and
    universal game automation.

Where the current run provides direct evidence, the run evidence is
preferred over assumptions about how xCloud should behave.
