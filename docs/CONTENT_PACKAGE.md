# BattleTanx Studio content packages

Studio exports custom work as a deterministic `btga-content-package` ZIP. It
does not modify a cartridge ROM or PlayStation disc image. Packages contain
only project metadata, map edits, Studio-created resources, hashes, and
references to game-derived resources extracted locally by the user.

## Version 1 layout

```
manifest.json
project.json
maps/<map-key>.json
resources/models/<content-id>.json
resources/textures/<content-id>.rgba
```

The manifest records schema versions, required source hashes, referenced game
resources, and every member's size and SHA-256. Archive names, timestamps,
ordering, permissions, and compression settings are fixed so identical inputs
produce identical bytes.

Texture resources begin with little-endian `u16 width` and `u16 height`, then
contain exactly `width * height * 4` RGBA8 bytes. Model resources use the
neutral mesh representation documented in `PS1_RESOURCE_CONTRACT.md`.

Imported maps store a source scene ID and fingerprint plus a versioned overlay.
Blank maps store complete instance, marker, and collision records. Both forms
include bounds, environment, minimap configuration, and navigation edits.

## Reader requirements

A runtime reader must fail closed when:

- the schema is newer than it supports;
- a member is missing, duplicated, unlisted, unsafe, or has the wrong hash;
- a symbolic link or unsupported compression method is present;
- a member or the total expanded archive exceeds configured limits;
- a resource has invalid dimensions;
- a placed record is malformed or lies outside map bounds; or
- required local source hashes do not match.

The reference implementation is `tools/btga_studio/core/package.py`. Studio
currently writes packages but does not patch or deploy them into the game.

