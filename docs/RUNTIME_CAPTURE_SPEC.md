# In-game capture specification (for the PC runtime)

The animated demonstration of Studio content must come from the real PC
runtime once its package loader exists, never from Studio's Drive Preview.
This specification makes that capture repeatable and reviewable. Studio
requests it through the launch request's `capture` block
(`RUNTIME_LAUNCH.md`); the runtime owns recording.

## Output

| Property | Value |
|---|---|
| Resolution | 1280 x 960 internal render (4:3, the game's aspect), no scaling filter; encode at 640 x 480 for the GIF |
| Frame rate | capture every rendered frame at the game's native rate; encode the GIF at 15 frames per second (every 4th frame of 60, or 2nd of 30) |
| Duration | 30 seconds of gameplay after the first controllable frame, plus 2 seconds of the loading-to-play transition |
| Palette | 256 colours per frame with an optimised global palette; no dithering on HUD text |
| Files | `session-<session_id>/frames/NNNNNN.png`, `capture.gif`, `capture.json` (below) in `capture.directory` |

`capture.json` records the session ID, package SHA-256, cartridge SHA-256,
runtime name and version, map and scenario keys, render resolution, measured
frame rate, frames captured and dropped, and the input script's hash.

## Input sequence (Minneapolis + Global Assault, scenario "Defend the Queenlord")

Deterministic: fixed random seed (0), scripted input only, no human input.

| Time (s) | Input |
|---|---|
| 0.0 | spawn at team 0's player spawn, facing the IDS Center |
| 0.0 - 6.0 | accelerate forward along the street (navigation route from the spawn) |
| 6.0 - 8.0 | turn right while moving |
| 8.0 - 16.0 | forward towards the Commons square; enter the capture zone |
| 16.0 - 22.0 | hold inside the zone, rotating the turret (capture progress visible) |
| 22.0 - 26.0 | fire the main gun at the nearest tank-trap barrier |
| 26.0 - 30.0 | reverse and turn to show a game tank bot and the zone ring |

## Success criteria (each checked and recorded in `capture.json`)

1. The map is the packaged Minneapolis map: city geometry and the Global
   Assault objects (team base, Queenlord, tanks, crates, tank traps,
   destructible props) are visible in at least one frame each.
2. The player's tank is the game's tank and moves under game physics
   (speed, collision and ground following come from the runtime).
3. Scenario state appears in the HUD: the capture objective's progress
   changes while inside the zone.
4. No frame is blank, and fewer than 1% of frames are dropped.
5. The GIF is at most 15 MB and loops cleanly.
6. A reviewer watches the GIF and confirms 1-5 before it is published; the
   review verdict is added to `capture.json`.

If any criterion fails, the GIF is not used, and the failure is recorded instead.
