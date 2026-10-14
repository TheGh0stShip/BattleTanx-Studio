BTGA Studio (Windows x64)

Start BTGAStudio.exe. File > Open catalog... and choose a folder exported by
tools/export_neutral_assets.py, or try samples\sample-catalog (synthetic data)
and File > Open project... samples\sample.btgaproj.

samples\starter-example.btgaproj is an original, ROM-free example project
(three maps, a campaign, multiplayer, a minigame, tanks and gangs) that opens
with the built-in starter content; File > New project > Open the starter
example builds the same project.

Projects are non-destructive overlays: no ROM or disc image is modified.

Settings live in %APPDATA%\BTGAStudio.  For a portable install, create an
empty file named portable.txt next to BTGAStudio.exe (settings then go to
BTGAStudioData\ beside it), or start with --portable.
