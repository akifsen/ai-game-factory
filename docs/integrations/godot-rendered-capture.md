# Godot rendered capture

## Commands

```bash
gamefactory --project PROJECT init
gamefactory --project PROJECT run godot-capture --scenario visual-scenario.json
gamefactory --project PROJECT report --workflow WORKFLOW_ID
gamefactory --project PROJECT inspect WORKFLOW_ID
gamefactory --project PROJECT artifacts --workflow WORKFLOW_ID
gamefactory --project PROJECT approvals --workflow WORKFLOW_ID
gamefactory --project PROJECT approve APPROVAL_ID --comment "Reviewed"
gamefactory --project PROJECT reject APPROVAL_ID --comment "Changes needed"
gamefactory --project PROJECT resume WORKFLOW_ID
```

`run godot-verify` and its JSON/exit codes are unchanged. `--scenario` is valid for `godot-verify` and `godot-capture`.

Exit `3` means the workflow is blocked. After a technical PASS that block is the visual review, unless process execution was also configured to require approval earlier.

## Renderer

Requested profile: `gl_compatibility`, driver `opengl3`, windowed, audio `Dummy`.

Windows display driver is `windows`. Linux display driver is `x11` and requires `DISPLAY`. `XAUTHORITY` and `LIBGL_ALWAYS_SOFTWARE` are forwarded only when already set. The rest of the user environment is not copied.

The capture records the requested profile, the engine API adapter/driver/display values, and the matching process-log lines separately. Unknown adapter fields stay absent. A headless or dummy result is a failure, not a fallback success.

`doctor` does not launch Godot for this capability.

## Checkpoints

The reference scene stores HP, enemies, and score in the game script. `present_for_capture` paints the HUD from those fields. The harness does not receive expected assertion values.

Ticks for the fixture:

| Tick | HP | Enemies | Score |
| --- | --- | --- | --- |
| 0 | 100 | 3 | 0 |
| 30 | 80 | 3 | 0 |
| 60 | 80 | 2 | 100 |
| 90 | 40 | 2 | 100 |

Viewports: 1280×720 and 720×1280. Each profile is its own workflow. Portrait is not a phone test. PNG size must match the viewport. SDR RGB/RGBA only.

## Review page

Validation writes a static `review/index.html` plus `review/images/*.png`. The page has no script, remote images, or approve button. It is a snapshot. A later approval does not rewrite it; resume can add a separate receipt.

Open the HTML from disk. Image links are relative `images/` paths, so the folder can be copied.

## Limits

At most 8 captures. The fixture uses 4. Files must be regular files inside the attempt scratch, then registered under `.gamefactory/artifacts`. Region checks are fixture-specific and are not an aesthetic score.
