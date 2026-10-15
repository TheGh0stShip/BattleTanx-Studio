BattleTanx Studio for Windows
=============================

BattleTanx Studio makes maps, missions and multiplayer arenas for BattleTanx
and BattleTanx: Global Assault. It needs no installation of Python or any
other software, and it never changes your game files.

Getting started
---------------
1. Run BattleTanxStudioSetup.exe, or unzip the portable download anywhere you like.
2. Start BattleTanx Studio from the Start menu (or double-click BattleTanxStudio.exe in the unzipped folder).
3. On the welcome screen choose "Create Original Project" to start straight
   away with Studio's own content, or "Open Example Project" to look around.

Help is built in: press F1 or open the Help menu. Help > Guided tour walks you
through your first map.

Installing
----------
Run BattleTanxStudioSetup.exe. It installs for your user account (no
administrator rights), adds a Start menu entry and, if you like, a desktop
shortcut, and lists BattleTanx Studio in Windows Settings > Apps for removal.
Running it again repairs the installation or upgrades an older version.
If you use the portable ZIP instead, Tools > Settings > "Install for this
user" does the same, or simply keep using the unzipped folder.

The programs are not digitally signed, so Windows may warn before running
them. Check the download against SHA256SUMS.txt from the Releases page.

Using your own games
--------------------
Studio works fully without game files. To use maps and models from games you
own, open File > Source Manager and add your copy:
- BattleTanx (USA) for Nintendo 64: a ROM file (.z64, .v64 or .n64)
- BattleTanx: Global Assault (USA) for Nintendo 64: a ROM file
- BattleTanx: Global Assault (USA) for PlayStation: a .cue/.bin pair, a disc
  image (.bin or .iso), or a folder with the files copied from the disc
Studio checks the copy, reads it only on this computer, and never copies it
into projects or exported packages. No game data is included in this download.

Your own models, pictures and real places
-----------------------------------------
File > Import models and textures brings in OBJ and glTF models and PNG,
JPEG, BMP and TGA pictures, and can build a city from OpenStreetMap data.
See Help > Importing models, pictures and real places.

Where things are kept
---------------------
- Your projects: where you save them (default: Documents\BattleTanx Studio Projects)
- Settings: %APPDATA%\BattleTanx Studio
- Catalogs, your imported library, thumbnails, recovery copies and logs:
  %LOCALAPPDATA%\BattleTanx Studio
Tools > Settings shows these folders and can open them.

Portable use: put an empty file named portable.txt next to
BattleTanxStudio.exe to keep everything inside this folder instead.

Something wrong?
----------------
Help > Report a problem saves a short report (with your folder names removed)
and opens the project's issue page. Updates are published on the Releases page
(Help > Check for updates); Studio never updates itself.

Included in this folder
-----------------------
BattleTanxStudio.exe            the program
_internal\                      runtime files the program needs
samples\                        a synthetic sample and the starter example project
COPYING.txt                     BattleTanx Studio licence
THIRD-PARTY-NOTICES.txt         licences of bundled components
