"""Built-in help shown in the Help window (no internet or documentation folder needed)."""

TOPICS = (
    ("Getting started", """\
BattleTanx Studio makes maps and game content for BattleTanx without changing any game file.

1. Choose Create Original Project on the welcome screen. Pick a template (blank map, multiplayer arena,
   single-player mission or the starter example), a size and a name. Studio saves the project file at once.
2. Pick something in the Assets panel (bottom) and click in the viewport to place it. Press Esc to stop placing.
3. Click an object to select it. Use the toolbar's Move, Rotate and Scale tools, or the inspector on the right.
4. Edit spawns, pickups, objectives, navigation, missions, multiplayer and tanks in Tools > Gameplay editor.
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
)
