# BattleTanx Studio content packages

Studio exports custom work as a deterministic `btga-content-package` ZIP. It
does not modify a cartridge ROM or PlayStation disc image. Packages contain
only project metadata, map edits, Studio-created resources, hashes, and
references to game-derived resources extracted locally by the user.

## Version 1 layout

```
manifest.json
project.json
dependencies.json
maps/<map-key>.json
resources/models/<content-id>.json
resources/textures/<content-id>.rgba
```

`manifest.json` and `project.json` are indented JSON; map documents and model
resources are compact JSON (no whitespace). All JSON has sorted keys and
UTF-8 text. The manifest records schema versions, required source hashes, referenced game
resources, and every member's size and SHA-256. Archive names, timestamps,
ordering, permissions, and compression settings are fixed so identical inputs
produce identical bytes.

`project.json` carries the project's campaigns, missions, multiplayer
setups, minigames, tanks, gangs, **scenarios** and **item definitions**
(`STUDIO_AUTHORING.md`). `dependencies.json` (`btga-dependency-report` v1)
lists every game resource the content needs and the package does not embed:
scenes and models by content ID, the records and definitions that use each,
whether the exporting Studio resolved it, and the cartridge SHA-256 it comes
from; plus the Studio and user resources that are embedded. An unresolved game
resource is a validation error, so an exported package never lists one as
unresolved.

The manifest adds `features_required` (every runtime feature the content
uses, see `RUNTIME_FEATURES.md`) and `playability`: `"unverified"` when no
runtime declared its capabilities to the exporting Studio, `"supported"` when
the configured runtime declares every feature, `"unsupported"` with the
missing features otherwise. A package is never marked playable without a
runtime declaring it.

Texture resources begin with little-endian `u16 width` and `u16 height`, then
contain exactly `width * height * 4` RGBA8 bytes. Model resources use the
neutral mesh representation documented in `PS1_RESOURCE_CONTRACT.md`.

Models and textures the user imported (`user:` content IDs: OBJ, glTF and
picture imports, and cities built from OpenStreetMap) are embedded like
Studio-original ones. Their model resources carry the import record
(`source.import`: file name, SHA-256, credit and licence text), and the
manifest lists every embedded user resource under `credits` and every user
texture's label, wrap mode and sky layout under `user_textures`, so another
computer can restore them into its library (Import window > Your imported
library > Add from a content package).

A blank map's first environment record may carry a sky panorama:
`"sky": {"texture", "north", "horizon", "vertical_degrees"}` (see
`tools/btga_studio/core/sky.py`): the picture wraps once around the viewer
at infinity, left edge facing north, rows in equal steps of elevation with
the horizon at row fraction `horizon`.

Teleporters are Studio-defined markers: `studio_teleporter` (fields `exit`,
a reference to a `studio_teleport_exit` marker, `radius`, `cooldown_seconds`,
`keep_speed`) and `studio_teleport_exit` (its position and yaw are where and
which way a vehicle arrives). A runtime that does not implement them must
ignore them; Studio validation refuses a teleporter without a valid exit.

Collision boxes generated for a city carry `"allow_overlap": true`; overlaps
between two such boxes are expected approximations and are not reported.

Imported maps store a source scene ID and fingerprint plus a versioned overlay.
Blank maps store complete instance, marker, and collision records. Both forms
include bounds, environment, minimap configuration, and navigation edits.

## Reader requirements

A runtime reader must fail closed when:

- the schema is newer than it supports, or the schema version is not a
  positive integer (booleans, strings and zero are refused);
- the manifest, project, a map document, a record or a model resource is not
  a JSON object of the expected shape (a typed `PackageError` names the map,
  record or file; no other exception type escapes);
- a member is missing, duplicated, unlisted, unsafe, has the wrong hash, or
  fails the archive's own CRC check;
- a placed instance references a Studio-original or user-imported model
  whose resource is not in the package, a packaged model references a texture
  that is not in it, or a map's sky picture is missing;
- a symbolic link or unsupported compression method is present;
- a member or the total expanded archive exceeds configured limits;
- a resource has invalid dimensions;
- a placed record is malformed, belongs to an unknown record family, or lies
  outside map bounds; or
- required local source hashes do not match; or
- `dependencies.json` is present but not a dependency report, or
  `project.json`'s `scenarios` or `items` is not an object.

A runtime must also refuse to play content whose `features_required` it does
not implement, instead of ignoring the parts it does not understand.

The reference implementation is `tools/btga_studio/core/package.py`. Studio
currently writes packages but does not patch or deploy them into the game.

