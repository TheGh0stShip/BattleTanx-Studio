"""Global Assault models Studio can offer by role, with how each identification is known.

Every entry is a content ID from a catalog the Source Manager builds from the
USA Rev 0 cartridge (SHA-256 88268ce7...6221); none of the model data is
here, only identifiers.  A model is offered only when a loaded catalog has
it.  ``evidence`` uses the scene schema's classes:

    proven     a decoded record names the model in this role
    inferred   chosen from how the game uses it plus how it looks; not stated by code
    visual     chosen by appearance only; its game role is not decoded

``parts(model_id)`` lists the models drawn together for one choice (the
game's tanks are a hull plus a turret).
"""

from __future__ import annotations

GA = "n64_btga:model:"
TANK_HULL = GA + "b32641b1e730ba4a2ba02cdc"  # shared bank model 16
TANK_TURRET = GA + "f702655b8fd63dc41d195d64"  # shared bank model 9
QUEENLORD = GA + "f8830632f94a40d9ba851c07"
BASE_STATES = [GA + h for h in ("0481963cec2c669a98d9ce07", "7a3b9ebd526a619c6a714802", "4c3aebe8bc12b42d2489cc41", "1bf30e389b27717bd8637147",
                                "b80c7ad5d0bdca51a5efa9d1", "be52b77da581848725fd39e7", "a8cbf6b3f32cb4816bd199af", "0d4883f13e13c8709e528c9a")]
POWERUP_PAD = [GA + h for h in ("060182b55e512982e52eb25b", "ffb9c504231597f9b50b72f6", "69ed872ff5d8e4de255a5866")]

COMPANIONS = {TANK_HULL: [TANK_TURRET]}

ENTRIES = [
    {"id": TANK_HULL, "label": "Tank (hull and turret)", "role": "tank", "evidence": "inferred",
     "note": "shared-bank models 16 and 9 render as a hull and its matching turret; the game's tank table that pairs them is not decoded"},
    {"id": GA + "40f778fe0b9ad39940484e34", "label": "Tank hull (bank 20)", "role": "tank", "evidence": "visual", "note": "hull only"},
    {"id": GA + "c95be8b22eb9abace620597a", "label": "Tank hull (bank 24)", "role": "tank", "evidence": "visual", "note": "hull only"},
    {"id": QUEENLORD, "label": "Queenlord (portrait card)", "role": "queenlord", "evidence": "inferred",
     "note": "the multiplayer base record looks this model up only in game mode 11 (func_800DF758, proven); the game's multiplayer "
             "objectives keep Queenlords at the base; the model is a woman's portrait card. Mode 11 being the Queenlord mode is not proven"},
    {"id": BASE_STATES[0], "label": "Team base (intact)", "role": "base", "evidence": "proven",
     "note": "model 0 of the multiplayer base structure (kind 31 marker); models 1-5 are its damage states, 6-7 debris"},
    *[{"id": model, "label": f"Team base (damage state {n})", "role": "base_damaged", "evidence": "proven", "note": "multiplayer base structure"}
      for n, model in enumerate(BASE_STATES[1:6], 1)],
    {"id": POWERUP_PAD[0], "label": "Power-up pad", "role": "pad", "evidence": "proven", "note": "model 0 of the game's power-up pad records"},
    {"id": GA + "844240dbb66306f4a6903f9f", "label": "Crate: red cross", "role": "crate", "evidence": "visual", "note": "shared-bank model 202"},
    {"id": GA + "632cff80003168c14f35d2a5", "label": "Crate: star", "role": "crate", "evidence": "visual", "note": "shared-bank model 205"},
    {"id": GA + "64355cdbbd2715d32abe4002", "label": "Crate: shells", "role": "crate", "evidence": "visual", "note": "shared-bank model 200"},
    {"id": GA + "7a75b6584bd0b8d99dfcc993", "label": "Crate: target", "role": "crate", "evidence": "visual", "note": "shared-bank model 201"},
    {"id": GA + "2d0f9ee93aa49d966b39bb87", "label": "Crate: lightning", "role": "crate", "evidence": "visual", "note": "shared-bank model 203"},
    {"id": GA + "47deacc750c6a273c54b4f61", "label": "Crate: green zigzag", "role": "crate", "evidence": "visual", "note": "shared-bank model 204"},
    {"id": GA + "9667e5acd2c5887c2f7fa678", "label": "Crate: burst", "role": "crate", "evidence": "visual", "note": "shared-bank model 206"},
    {"id": GA + "12d0529d34558d3ed071261c", "label": "Crate: radiation", "role": "crate", "evidence": "visual", "note": "shared-bank model 121"},
    {"id": GA + "dea0481f7fca503b86113028", "label": "Warning beacon", "role": "prop", "evidence": "visual", "note": "shared-bank model 214"},
    # Destructible props: model_0 / model_1 of the game's kind-10 destructible-prop records (func_800ED320, proven).  Which of the
    # two is the wreck is judged by appearance.
    *[{"id": GA + intact, "label": f"{name} (intact)", "role": "destructible", "evidence": "proven",
       "note": f"model 0 of the game's destructible-prop records ({where}); the wreck is model 1", "wreck": GA + wreck}
      for name, intact, wreck, where in (("Shed", "e68e984dc377e772db82aba0", "602d478b0048da63452a82c3", "Arizona - Truck Stop"),
                                         ("Watchtower", "b7d6482ae1f024e5f6a3433b", "0adf11bc869efe8b36ff3ad7", "Texas - Slave Fortress"),
                                         ("Fence", "437dbc84855129fe99199539", "8be4d3ce25a02609838e82a2", "London - Parliament"),
                                         ("Spire", "c5cd896ccafd9b825c4c806b", "087ceeda5fb39ccd205ea42a", "Washington DC - Mall"))],
    *[{"id": GA + wreck, "label": f"{name} (wreck)", "role": "destructible_wreck", "evidence": "inferred",
       "note": "model 1 of the same destructible-prop records; identified as the wreck by appearance"}
      for name, wreck in (("Shed", "602d478b0048da63452a82c3"), ("Watchtower", "0adf11bc869efe8b36ff3ad7"), ("Fence", "8be4d3ce25a02609838e82a2"),
                          ("Spire", "087ceeda5fb39ccd205ea42a"))],
    # Level dressing placed as scenery in several original levels (role by appearance).
    *[{"id": GA + model, "label": name, "role": "barrier", "evidence": "visual", "note": f"scenery model placed {uses} times across the levels"}
      for name, model, uses in (("Tank traps (row)", "d2945483a04e80462a99b4b6", 239), ("Tank traps (pair)", "f18e7eb5c6d4096a8370d49d", 280),
                                ("Tank trap", "83cbbc47b86d066c7ecafb7d", 263), ("Concrete barrier", "fdcb77b52c11a23be1b80929", 197),
                                ("Concrete wall block", "87211c867b52aebc6e581e3f", 198), ("Guard rail", "e70de276d4b5c53eeb7cffeb", 2223))],
]
BY_ID = {entry["id"]: entry for entry in ENTRIES}


def parts(model_id: str) -> list[str]:
    return [model_id] + COMPANIONS.get(model_id, []) if model_id else []


def offered(library, roles=None) -> list[dict]:
    """Entries whose model a loaded catalog has, optionally only these roles."""

    if library is None:
        return []
    return [entry for entry in ENTRIES if entry["id"] in library._models and (roles is None or entry["role"] in roles)]
