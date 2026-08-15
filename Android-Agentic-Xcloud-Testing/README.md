# Android-Agentic-Xcloud-Testing

Describe a test in plain words. Agents work out how to perform it, watch the
phone's screen, decide whether it really happened, diagnose it when it did not,
and write the report.

```bat
5-AGENTIC.bat "open xCloud and check the controller is detected"
```

Built on top of the working hardware in the folder above: PC → FT232RL → Arduino
Leonardo → USB-OTG → phone, where the Leonardo is a real USB HID gamepad.

---

## Why this exists

The parent README is honest about its own limitation:

> **Blind automation.** A firmware `OK` proves the HID report was queued, not
> that the game reacted.

That is a serious problem for testing, because `OK` and "it worked" look
identical in a log. The troubleshooting table even names the symptom directly —
*"Commands say ok but phone does nothing"* — and the only way to notice it was a
human watching the screen.

This layer closes that loop. It takes a screenshot after every step and compares
it to the one before, so it can report the one thing a firmware `OK` never can:

```
SILENT FAILURES at s5: the firmware accepted the command
and the screen did not change. Input is not reaching xCloud.
```

Everything else here — the planning, the verdicts, the root-cause analysis —
exists to make that observation useful.

---

## Install

```bat
cd Android-Agentic-Xcloud-Testing
pip install -r requirements.txt
```

Then give it an API key, in a `.env` beside the `.bat` files:

```
OPENAI_API_KEY=sk-...
```

Any provider in `config/agentic.yaml` works — OpenAI, Anthropic, Gemini, or a
local Ollama with no key at all. Switch with `--llm local`.

**No key?** It still runs. Every agent has a deterministic fallback, and the
report states plainly which conclusions were mechanical rather than reasoned.

Optional, but strongly recommended:

```bat
adb tcpip 5555 & adb connect <phone-ip>:5555   :: gives the rig EYES
```

Without adb there are no screenshots, so nothing can be *observed* — and every
verdict is capped at `inconclusive`. The phone's USB port is busy being an OTG
host for the Leonardo, so Wi-Fi is the way.

---

## Use

```bat
5-AGENTIC.bat --check                    :: is the rig ready? sends no input
5-AGENTIC.bat --capabilities             :: what can this run actually do?
5-AGENTIC.bat "your scenario in words"
5-AGENTIC.bat --scenario scenarios\controller_detected.yaml
5-AGENTIC.bat --scenario scenarios\ --all
5-AGENTIC.bat --dry-run "..."            :: plan and reason, touch no hardware
```

Start with `--check`. It answers three questions that are easy to conflate and
fail independently:

| Question | Meaning |
|---|---|
| Does the **board** answer? | `PING` → `PONG` over the FT232RL UART |
| Has a **host enumerated the pad**? | the phone is in OTG host mode |
| Can we **see** the phone? | adb — optional, but verdicts are weak without it |

Conflating the first two is exactly the bug the parent project lost time to: the
firmware answered `OK` to everything while its HID interface had never
enumerated.

### Exit codes

`0` pass · `1` fail · `2` blocked · `3` inconclusive · `4` error

`inconclusive` is deliberately **not** `0`. A run that proved nothing must not be
able to turn a pipeline green.

---

## The agents

| Agent | Question it answers |
|---|---|
| **Device** | Is the phone connected, and can we send it signals? |
| **Scenario** | Is this scenario testable *on this rig*? (may refuse) |
| **Planner** | Which steps produce evidence for each criterion? |
| **Executor** | Perform each step, observe, and judge it |
| **Evaluator** | Verdict against the acceptance criteria |
| **Root cause** | Which *layer* broke, and how could we disprove that? |
| **Reporter** | JSON, Markdown, HTML |

```
device → scenario → plan → execute ⟲ → evaluate → report
            │         │        │            │
            └─────────┴────────┴─→ rca → replan
                (halt)              or ↘ report
```

One step per graph tick, so the graph can divert to root-cause analysis the
moment a step's expectation fails — rather than grinding through the rest of a
plan that is already off the rails.

Two agents were added beyond the obvious set:

- **Evaluator**, separate from the executor, because "did the tile move?" and
  "can a user reach their library?" are different questions. Merging them lets a
  run with all-green steps claim a pass for something nothing ever checked.
- **Observation** (in `tools/vision.py`, called by the executor) — the eyes,
  without which none of the above means anything.

---

## Nothing is hardcoded

| What | Where it comes from |
|---|---|
| Which buttons, sticks, macros exist | `../config/controls.yaml`, read at runtime |
| What to test | `scenarios/` — YAML, markdown or a typed sentence |
| Which model, per agent | `config/agentic.yaml` |
| Timings | `../config/controls.yaml`, offered to the planner |

Add a macro to `controls.yaml` and the planner can use it on the next run with no
code change. Confirm with `--capabilities`, which prints exactly what the agents
will be told.

The reverse also holds, and matters more: `PlannerAgent._sanitise` **drops** any
step naming a control that does not exist. A hallucinated `press("options")`
never reaches the hardware. The prompt asks; the code enforces.

---

## How it avoids lying to you

A test framework that reports a confident wrong answer is worse than none. Three
mechanisms, all in code rather than in prompts:

1. **`expectation_met` is a tri-state.** `True` / `False` / `None`, where `None`
   means *we could not tell*. It is not a pass, and it caps the verdict.

2. **The verdict ceiling** (`EvaluatorAgent._ceiling`) can only ever *lower* a
   verdict, after the model has spoken. No sensors → `inconclusive`. Dry run →
   `inconclusive`. A silent failure → `fail`, whatever else looks fine.

3. **Pixels outrank prose.** If the firmware said `OK` and the screen did not
   change, the step is marked failed even if the model was optimistic about it.

---

## xCloud is a PWA — what that changes

Not an installed app. A web page in a browser, sometimes installed as a WebAPK.
So the usual Android-automation moves are unavailable:

- No package to launch → we send a **VIEW intent for a URL**.
- No activity to assert on → the foreground app is a **browser**, by design.
  Seeing an address bar or tabs in a screenshot is *correct*, not a defect.
- No local version to read → **the build under test is whatever the server
  served today.** It can change between two runs with no local change at all,
  which makes an unexplained new failure genuinely plausible rather than
  automatically our bug.

Browsers are *discovered* (`android.pwa.browser_hints` are hints, not
assumptions) and the choice is reported, never assumed.

Watch for anything that steals gamepad input: a permission dialog, an app
chooser, an on-screen keyboard. The observer is told to look for these, because
they produce a perfect impression of "the controller is broken".

---

## Root-cause analysis

One symptom — "the button did nothing" — has at least six causes with six
different owners:

| Cause | The check that would disprove it |
|---|---|
| `wiring` | `python host\pad_link.py --check` → a `PONG` rules it out |
| `host_mode` | Is the Leonardo's **ON LED lit**? Lit = the phone *is* a host |
| `hid_enumeration` | `python host\verify_hid_raw.py` on the PC → expect 8/8 |
| `pwa_not_loaded` | Does the browser have focus and show xCloud? |
| `timing` | Re-run with a longer observe delay |
| `app_defect` | Only when input demonstrably arrived and the app misbehaved |

Every hypothesis must come with a `discriminating_test` — a check capable of
saying **no**. That is not decoration; it is the discipline that cracked the
parent project, where `verify_hid_raw.py` broke a deadlock precisely because it
read real HID report bytes instead of trusting a firmware `OK`.

The RCA also decides `retryable`, but code has the final word: only causes in
`retry.retryable_causes` earn a replan. A wiring fault will not heal on retry,
and one clear "go check the ON LED" beats three identical failures.

---

## Reports

Written to `reports/` in three formats:

- **JSON** — the full state: every observation, judgement and agent action. The
  primary artefact, so a verdict can be re-examined months later without the
  hardware.
- **Markdown** — for a pull request.
- **HTML** — screenshots inline, for a human.

Screenshots land in `artifacts/<run-id>/screens/`.

---

## Configuration worth knowing

In `config/agentic.yaml`:

| Key | Why you would touch it |
|---|---|
| `execution.mode` | `adaptive` (default), `plan`, or `reactive` |
| `execution.observe_delay_seconds` | Raise it if steps look falsely failed |
| `vision.motion_threshold` | Fraction of changed pixels counting as a reaction |
| `retry.max_replans` | Budget for RCA → replan cycles |
| `safety.forbidden_controls` | e.g. `["guide"]` if Home hijacks your device |
| `safety.max_run_seconds` | Hard stop |

Any key can be overridden by environment variable: `XAT_EXECUTION_MODE=plan`.

---

## Known limitations

- **`guide` is unverified.** Some Android builds intercept the HID Home usage
  before the page sees it, so a negative result there is uninformative.
- **Not frame-accurate.** The stream adds 60–100 ms on top of UI animation. Menu
  automation is practical; measuring latency is not.
- **OCR needs the tesseract *binary*,** not just `pytesseract`.
- **A PWA can change under you.** Two identical runs can differ legitimately.
- **The frame diff is deliberately crude.** It answers "did the UI react", not
  "how". A pixel-exact comparison would call every frame of a compressed video
  stream different, and so could never say no — which would make it worthless.

---

## Programmatic use

```python
from agentic import Settings, run_test

settings = Settings()
settings.override("hardware.dry_run", True)

state = run_test("check the D-pad moves the highlight", settings)
print(state["verdict"], state["report"].executive_summary)
```
