# Global Runtime Input

The closed-loop xCloud runner now supports a reusable `$input` token for text
that must be entered into a focused Android field.

## Set the value

The settings layer maps dotted keys to `XAT_` environment variables. Therefore:

```text
XAT_RUNTIME_INPUT=Minecraft Dungeons
```

or in a shell:

```bash
XAT_RUNTIME_INPUT="Minecraft Dungeons" python main.py --case minecraft_dungeons_launch
```

The scenario can use:

```yaml
target: "$input"
```

The executor passes that token to `AndroidTool.input_text()`, which resolves it
to `runtime.input`. The LLM never constructs an ADB command.

## Important limitation

`adb shell input text` requires Android input-injection permission on some
phones. If the device returns `SecurityException`/`INJECT_EVENTS`, the search
fixture is blocked. Do not interpret that as a gamepad failure.

## Search-first policy

For game launch the preferred order is:

1. Open xCloud.
2. Find/focus search with the physical controller.
3. Enter `$input` through ADB text.
4. Verify the search field/results.
5. Select the result with physical gamepad A.
6. Launch with physical gamepad A.
7. Observe and wait through fullscreen/loading/splash.
8. Verify the game's main menu.

Minecraft-specific library/D-pad navigation is no longer part of the Minecraft
launch case.
