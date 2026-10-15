"""Project-wide authoring checks and the package dependency report.

``content_problems`` validates scenarios, item definitions, item pickups and
(optionally) bots across the maps given as open documents, returning
``scenario.Problem`` items.  ``dependency_report`` lists every resource the
project's content needs that a package does not embed (game scenes and
models, referenced by content ID only), with the source hash each one comes
from and whether the loaded catalogs resolve it; Studio and user resources
that the package embeds are listed separately.  Nothing in the report is a
filesystem path.
"""

from __future__ import annotations

from . import bots as bots_module
from . import items as items_module
from . import scenario as scenario_module
from .scenario import Problem

REPORT_SCHEMA = "btga-dependency-report"
REPORT_VERSION = 1
EMBEDDED = ("studio", "user")


def content_problems(project, library, documents: dict, profile: dict | None = None, include_bots: bool = True) -> list[Problem]:
    out: list[Problem] = []
    known_models = set(library._models) if library is not None else None
    known_textures = set(library._textures) if library is not None else None
    out += items_module.check_items(project.items, known_models, known_textures)
    for key, scenario in sorted(project.scenarios.items()):
        map_key = scenario.get("map") if isinstance(scenario, dict) else None
        ctx = scenario_module.context_for(project, key, documents.get(map_key), library, profile)
        out += scenario_module.check(key, scenario, ctx)
    for map_key, document in sorted(documents.items()):
        on_map = [s for s in project.scenarios.values() if isinstance(s, dict) and s.get("map") == map_key]
        for _, record in document.scene.records("markers"):
            if record.get("category") == "item_pickup" and not record.get("disabled"):
                out += items_module.check_pickup(record, project.items, on_map)
        if include_bots:
            out += bots_module.check_bots(document, library, profile)
    unique = {}
    for problem in out:
        unique.setdefault((problem.severity, problem.code, problem.target, problem.field, problem.message), problem)
    return sorted(unique.values(), key=lambda p: (p.severity != "error", p.target, p.code, p.field or "", p.message))


def _platform(identifier: str) -> str:
    return str(identifier).split(":", 1)[0]


def dependency_report(project, library, documents: dict) -> dict:
    """Every non-embedded resource the project needs, its source hash and resolution status, plus what is embedded."""

    needed: dict[str, dict] = {}
    embedded: dict[str, set] = {}

    def need(identifier, kind, where):
        if not identifier:
            return
        if _platform(identifier) in EMBEDDED:
            embedded.setdefault(identifier, set()).add(where)
            return
        item = needed.setdefault(identifier, {"id": identifier, "kind": kind, "platform": _platform(identifier), "used_by": set()})
        item["used_by"].add(where)

    for key, entry in sorted(project.maps.items()):
        if entry.kind == "imported":
            need(entry.scene, "scene", f"maps:{key}")
        document = documents.get(key)
        if document is None:
            continue
        for family, record in document.scene.records():
            if record.get("disabled"):
                continue
            if family == "instances" and record.get("source", {}).get("user_created"):
                need(record["model"], "model", f"maps:{key}:{record['id']}")
            elif family == "markers" and record.get("overlay"):
                for dependency in record.get("dependencies", []):
                    if dependency.get("kind") == "model":
                        need(dependency.get("id"), "model", f"maps:{key}:{record['id']}")
                if record.get("category") in ("bot_slot", "destructible_target"):
                    for name in ("model", "destroyed_model"):
                        need(bots_module.value(record, name), "model", f"maps:{key}:{record['id']}")
    for key, item in sorted(project.items.items()):
        need(item.get("model"), "model", f"items:{key}")
        icon = item.get("icon")
        if icon and not str(icon).startswith("studio:icon:"):
            need(icon, "texture", f"items:{key}")
    for key, tank in sorted(project.tanks.items()):
        need(tank.get("model"), "model", f"tanks:{key}")
    sources = {}
    for source in (library.catalog.get("sources", []) if library is not None else []):
        if source.get("rom_sha256") or source.get("file_sha256"):
            sources.setdefault(source.get("platform"), []).append(source)
    rows = []
    for identifier, item in sorted(needed.items()):
        status, source_hash = "unresolved", None
        if library is not None:
            if item["kind"] == "model" and identifier in library._models:
                status = "resolved"
                try:
                    provenance = library.model(identifier).provenance or {}
                    source_hash = provenance.get("rom_sha256") or provenance.get("file_sha256")
                except Exception:  # an unreadable model record stays resolved by ID but without its hash
                    source_hash = None
            elif item["kind"] == "scene" and identifier in library._levels:
                status = "resolved"
            elif item["kind"] == "texture" and identifier in library._textures:
                status = "resolved"
        if source_hash is None:
            candidates = sources.get(item["platform"], [])
            if len(candidates) == 1:
                source_hash = candidates[0].get("rom_sha256") or candidates[0].get("file_sha256")
        rows.append({**item, "used_by": sorted(item["used_by"]), "status": status, "source_sha256": source_hash})
    return {
        "schema": REPORT_SCHEMA, "schema_version": REPORT_VERSION,
        "project": {"id": project.project.get("id"), "title": project.project.get("title")},
        "sources": sorted(({k: s.get(k) for k in ("platform", "title", "rom_sha256", "file_sha256") if s.get(k)}
                           for s in project.dependencies.get("sources", [])), key=lambda s: (s.get("platform") or "", s.get("rom_sha256") or "")),
        "game_resources": rows,
        "unresolved": [row["id"] for row in rows if row["status"] != "resolved"],
        "embedded": [{"id": identifier, "used_by": sorted(where)} for identifier, where in sorted(embedded.items())],
    }
