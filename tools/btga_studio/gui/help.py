"""Built-in help shown in the Help window (no internet or documentation folder needed)."""

TOPICS = (
    ("Getting started", """\
BattleTanx Studio makes maps and game content for BattleTanx without changing any game file.

1. Choose Create Original Project on the welcome screen. Pick a template (blank map, multiplayer arena,
   single-player mission or the starter example), a size and a name. Studio saves the project file at once.
2. Pick something in the Assets panel (bottom) and click in the viewport to place it. Press Esc to stop placing.
3. Click an object to select it. Use the toolbar's Move, Rotate and Scale tools, or the inspector on the right.
4. Edit spawns, pickups, objectives, navigation, missions, multiplayer and tanks in Tools > Gameplay editor, and
   write scenarios, items and bots in Tools > Scenario editor and Tools > Items and pickups.
5. Save with Ctrl+S. Export a content package with File > Export content package. Packages contain only your
   own work and Studio's original starter content.

Everything you do can be undone (Ctrl+Z) until you close the map. Studio also keeps a recovery copy of unsaved
work in case the program or computer stops unexpectedly."""),
    ("Using your own games", """\
Studio works without any game files. To use maps and models from BattleTanx games you own, open
File > Source Manager and add your copy:

- BattleTanx (USA), Nintendo 64: a cartridge ROM file (.z64, .v64 or .n64).
- BattleTanx: Global Assault (USA), Nintendo 64: a cartridge ROM file.
- BattleTanx: Global Assault (USA), PlayStation: a .cue/.bin pair, a .bin or .iso disc image, or a folder
  holding the files copied from the disc (the folder with SLUS_010.44).

Studio checks the file against known good copies before reading it, never changes it and never copies it into
projects or packages. It builds a catalog of models and maps in your local app data so the game file is
read only once. If you move the file later, use Relink in the Source Manager.

Other regions, revisions or modified copies are not supported and are reported as "Not recognised"."""),
    ("Where your files are", """\
- Projects: wherever you save them (by default Documents > BattleTanx Studio Projects).
- Settings: your Windows AppData (Roaming) folder.
- Catalogs, thumbnails, recovery copies and logs: your Windows AppData (Local) folder.

Tools > Settings shows each location and has buttons to open them. Removing a source from Studio never deletes
your game file."""),
    ("Mouse and keyboard", """\
Right mouse + move: look around       W A S D: move        Q / E: down / up        Shift: faster
Mouse wheel: movement speed           F: focus selection   Home: whole map
Click: select     Ctrl+click: add or remove     Drag: box select     Right-click (no drag): object menu
1 / 2 / 3: move / rotate / scale tool     Shift+drag a handle: duplicate while moving     Esc: cancel
Ctrl+Z / Ctrl+Y: undo / redo     Ctrl+D: duplicate     Del: delete     Ctrl+C / Ctrl+V: copy / paste
H: hide   I: isolate   U: show all   L: lock     Ctrl+S: save     Ctrl+O: open     Ctrl+N: new
Keyboard shortcuts never fire while you are typing in a text box."""),
    ("Reporting a problem", """\
Help > Report a problem saves a short report (version, system and recent log messages, with your folder names
removed) and opens the project's issue page, where you can attach it. Help > Open logs folder shows the logs."""),
    ("Importing models, pictures and real places", """\
File > Import models and textures (or Import... above the Assets panel) brings in your own work and anything
you are allowed to use, such as public-domain or Creative Commons collections:

- Models: OBJ (keep its .mtl file and pictures next to it) and glTF / GLB. Other 3D formats (FBX, 3DS, BLEND,
  DAE) can be saved as glTF from Blender (File > Export > glTF 2.0).
- Pictures: PNG, JPEG, BMP and TGA. A picture can stay a texture or become a ground tile, an upright panel,
  a box, or a sky panorama all around the map (choose it under Gameplay editor > Map > Sky panorama).

Studio checks each file first and explains any problem. Fill in "Credit and licence" so everyone who plays
your package can see who made it. Your imports appear in the Assets panel under "Imported: <group>". A
model's base sits on the ground; one metre is about 16 Studio units. Right-click a placed model and choose
"Make solid" to give it a collision box.

"Build a city from OpenStreetMap" turns a map area exported from openstreetmap.org into a playable map at real
scale: buildings with their heights, roads, rivers, parks, bridges, skyways and trees. Add a folder of
elevation tiles for real hills. Detail: High adds windows that match the real floors, roof parapets, gabled roofs,
street lamps, traffic signals, walls, fences, crosswalks and more trees, at the cost of a larger package.
The map data credit is added for you.

Projects that use imports need them on the computer that opens them. Packages carry them: Import window >
Your imported library > Add from a content package."""),
    ("Scenarios, items and bots", """\
Tools > Scenario editor writes what happens on a map: match rules, objectives and how they follow each other,
triggers, timers, counters, waves, groups, messages and how the scenario starts and ends.

- Objectives: the graph shows each objective as a box. An arrow means "requires": the box at the arrow's end
  starts when the ones before it are done. Boxes with no arrow from another objective start at once (in
  parallel). Dashed boxes are optional. "Repeat" makes an objective count several times or for the whole match.
- Triggers, timers and objectives run actions: a fixed list of things such as showing a message, starting a
  timer, adding to a counter, switching objects on or off, sending a wave or ending the scenario. There is no
  scripting language and nothing a package runs can be code.
- Pick map objects by name from the lists. Place trigger volumes, zones, destructible targets, bots and item
  pickups from the Map objects tab or the Create menu.
- Tools > Items and pickups defines items once for the whole project (repair kits, ammunition, weapons, cash,
  score, temporary boosts, keys, scripted effects) and places them as pickups.
- A bot is a tank or Queenlord placed where it starts. Its Inspector shows its AI slot, team, behaviour
  (attack, defend, patrol, capture, escort, support, boss) and the target its behaviour needs.

The Check tab and the Validation panel explain every problem: missing objects, loops, objectives that can never
be completed, places bots cannot reach, duplicate AI slots and missing models. Studio supports writing all of
this; whether a game build plays it is up to the PC runtime, which says which features it supports."""),
    ("Drive Preview and Test in Game", """\
Drive Preview (F5) drives a tank-sized body around the map so you can check scale, kerbs and slopes, blocked
paths, water, which way spawns face, whether navigation points connect, and where trigger areas, pickups and
bots are. It is an editor approximation: no game physics, AI, combat, destruction, pickups or scripting run in
it. Teleporter pads and pickups only show a notice; T is an editor jump to a pad's exit for checking it.

Test in Game starts the real PC runtime with your exported package. It is only available when a compatible
runtime program is chosen in Tools > Settings > Test in Game (with your cartridge file, which stays on this
computer and is never saved into projects or packages). Before starting, Studio checks the runtime version, that
the package matches your project, that the cartridge is the right one and that the runtime supports every
feature your content uses, and tells you exactly what is wrong if anything is. It never falls back to Drive
Preview."""),
)
