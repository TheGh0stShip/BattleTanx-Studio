#!/usr/bin/env python3
"""Prove the BTGA Studio editor tests catch deliberately broken editor-core logic.

Each mutation rewrites one snippet in a temporary copy of the tools and
tests, runs tests.test_studio_editor there with this interpreter (which must
have tools/btga_studio/requirements.txt installed), and expects a failure.
The working tree is never modified.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEOMETRY = "tools/btga_studio/core/geometry.py"
SCENE = "tools/btga_studio/core/scene.py"
DOCUMENT = "tools/btga_studio/core/document.py"
PROJECT = "tools/btga_studio/core/project.py"
THUMBS = "tools/btga_studio/core/thumbnails.py"
INPUT = "tools/btga_studio/gui/input.py"
OVERLAY = "tools/btga_assets/overlay.py"
INSPECTOR = "tools/btga_studio/core/inspector.py"
PACKAGE = "tools/btga_studio/core/package.py"
CONTROLLER = "tools/btga_studio/core/controller.py"
BROWSER = "tools/btga_studio/core/browser.py"
RENDERER = "tools/btga_studio/gui/renderer.py"
COPIED = ("tools/btga_assets", "tools/btga_studio", "tools/benchmark_studio_editor.py", "tools/btga_world.py", "tools/lzari.py",
          "tools/n64_texture.py", "tests/__init__.py", "tests/test_studio_editor.py", "tests/test_n64_assets.py",
          "tests/test_studio_authoring.py", "tests/test_studio_acceptance.py", "tests/test_studio_product.py", "examples", "COPYING",
          "src/code/frontend/800E8380_slot_asset_ranges.c", "tests/test_studio_installer.py", "tests/test_studio_history.py",
          "tests/test_studio_tutorial_fonts.py", "tests/test_studio_setup.py", "tests/test_studio_evidence.py",
          "tools/audit_studio_package.py", "tools/audit_studio_installer.py", "tools/studio_evidence.py",
          "tests/test_studio_imports.py", "tests/test_studio_scenarios.py", "tests/test_studio_drive.py",
          "tools/runtime_feature_matrix.py", "docs/RUNTIME_FEATURES.md", "docs/schemas")
TESTS = ("tests.test_studio_editor",)
ACCEPTANCE_TESTS = ("tests.test_studio_acceptance",)
PRODUCT_TESTS = ("tests.test_studio_product",)

MUTATIONS = (
    (GEOMETRY, "picking: barycentric bound", "hit = valid & (u >= 0) & (v >= 0) & (u + v <= 1) & (t >= 0)",
     "hit = valid & (u >= 0) & (v >= 0) & (u + v <= 2) & (t >= 0)"),
    (GEOMETRY, "picking: farthest triangle", "index = int(np.argmin(candidates))", "index = int(np.argmax(np.where(np.isinf(candidates), -1, candidates)))"),
    (GEOMETRY, "axis swap: screen y", "ndc_y = 1.0 - 2.0 * (y + 0.5) / height", "ndc_y = 2.0 * (y + 0.5) / height - 1.0"),
    (GEOMETRY, "axis swap: x/z in yaw", "return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])",
     "return np.array([[c, 0.0, -s], [0.0, 1.0, 0.0], [s, 0.0, c]])"),
    (GEOMETRY, "yaw direction: units", "return int(round(radians * TURN / (2 * math.pi))) & 0xFFFF", "return int(round(-radians * TURN / (2 * math.pi))) & 0xFFFF"),
    (GEOMETRY, "camera: look direction", "self.yaw -= dx_pixels * self.look_sensitivity", "self.yaw += dx_pixels * self.look_sensitivity"),
    (GEOMETRY, "snap: yaw step", "step = TURN * degrees / 360", "step = TURN * degrees / 180"),
    (SCENE, "picking: tie-break priority", 'PICK_PRIORITY = {"markers": 0, "collision": 1, "instances": 2}', 'PICK_PRIORITY = {"markers": 2, "collision": 1, "instances": 0}'),
    (SCENE, "picking: world ray not localised", "local = g.Ray(rotation @ (ray.origin - translation) / scale, rotation @ ray.direction / scale)",
     "local = g.Ray((ray.origin - translation) / scale, ray.direction / scale)"),
    (DOCUMENT, "undo grouping: per-update commits", "        self._transform = {\"ids\": ids, \"originals\": originals, \"pivot\": pivot, \"delta\": np.zeros(3), \"yaw\": 0, \"scales\": scales,\n                           \"scale\": np.ones(3)}",
     "        self._transform = None"),
    (DOCUMENT, "cancel keeps preview", "        if state is not None:\n            self.scene.refresh(self.overlay, set(state[\"originals\"]))",
     "        if False:\n            self.scene.refresh(self.overlay, set(state[\"originals\"]))"),
    (DOCUMENT, "lost unknown bytes on duplicate", '            for key in ("id", "overlay", "disabled"):\n                copy_.pop(key, None)',
     '            for key in ("id", "overlay", "disabled", "raw_hex"):\n                copy_.pop(key, None)'),
    (DOCUMENT, "missing validation: fields", "        problems = field_errors(name, spec, value)\n        if problems:\n            raise EditError(\"; \".join(problems))",
     "        problems = []"),
    (DOCUMENT, "missing validation: read-only", '    if record.get("confidence") == "unresolved" or record.get("category") in READ_ONLY_CATEGORIES:', "    if False:"),
    (DOCUMENT, "missing validation: bounds", "        if bounds and not (bounds[\"x\"][0] <= translation[0] <= bounds[\"x\"][1] and bounds[\"z\"][0] <= translation[2] <= bounds[\"z\"][1]):\n            raise EditError(f\"({translation[0]:.0f}",
     "        if False:\n            raise EditError(f\"({translation[0]:.0f}"),
    (DOCUMENT, "redo reassigns ids", "        self.overlay.next_user_index = transaction.user_index_after", "        pass"),
    (DOCUMENT, "pivot ignored", "        moved = rotation @ relative + state[\"pivot\"] + state[\"delta\"]", "        moved = rotation @ np.asarray(translation, float) + state[\"delta\"]"),
    (PROJECT, "nondeterministic serialisation", "text = json.dumps(project.as_dict(), sort_keys=True,", "text = json.dumps(project.as_dict() | {\"nonce\": id(project)}, sort_keys=True,"),
    (PROJECT, "source hash not checked", "            if actual != entry.source_fingerprint:", "            if False:"),
    (THUMBS, "stale thumbnail keys", 'text = "\\x1f".join((content_id, renderer, preset, str(int(width)), str(int(height))))',
     'text = "\\x1f".join((content_id, str(int(width)), str(int(height))))'),
    (THUMBS, "corrupt thumbnails accepted", " or hashlib.sha256(payload).digest() != digest:", ":"),
    (THUMBS, "unbounded cache", "                if not self._entries or (self.bytes <= self.max_bytes and len(self._entries) <= self.max_entries):",
     "                if True:"),
    (THUMBS, "cancel ignored", "        kept = [job for job in self._queue if job.generation >= self.generation]", "        kept = list(self._queue)"),
    (OVERLAY, "resource id instability", '        digest = hashlib.sha256(self.scene_id.encode()).hexdigest()[:12]', '        digest = hex(id(self))'),
    (INPUT, "shortcuts while typing", "    if state.text_input:\n        return out", "    if False:\n        return out"),
    (INSPECTOR, "unresolved fields editable", "    reason = record_reason or field_editable(spec)", "    reason = record_reason"),
)

# Defects fixed during the acceptance review; each must be caught by tests.test_studio_acceptance.
ACCEPTANCE_MUTATIONS = (
    (DOCUMENT, "duplicate drops instance scale", "if k in copy_}})", 'if k in copy_ and k != "scale"}})'),
    (CONTROLLER, "cancelled Shift-drag keeps its copy",
     "            if self._gizmo_duplicated:  # a cancelled Shift-drag must not leave the stacked copy behind\n                self._drop_gizmo_duplicate()",
     "            if False:\n                self._drop_gizmo_duplicate()"),
    (CONTROLLER, "empty Shift-drag keeps its copy", "            self._drop_gizmo_duplicate()  # refused or zero-length Shift-drag", "            pass"),
    (PROJECT, "history shape unchecked", '            _check_history(map_data.get("history"), f"map {key!r}")', "            pass"),
    (PROJECT, "map entries unchecked", "            if not isinstance(map_data, dict):", "            if False:"),
    (PACKAGE, "package version accepts booleans and zero", "isinstance(version, bool) or version < 1", "False"),
    (PACKAGE, "missing model dependency accepted", "                    and model not in models:", "                    and False:"),
    (PACKAGE, "missing texture dependency accepted", "            if texture and _stem(str(texture)) not in textures:", "            if False:"),
    (PACKAGE, "zip read errors escape", "except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, ValueError) as error:",
     "except (RuntimeError,) as error:"),
    (BROWSER, "thumbnails requested by instance ID",
     '        return self.model or (self.id if self.kind == "model" and self.source != "user" else None)',
     '        return self.id if self.kind == "model" else None'),
    (THUMBS, "unknown model stops the editor", "            except Exception as error:  # an unknown model ID must not stop the editor",
     "            except KeyboardInterrupt as error:"),
    (RENDERER, "authored navigation not drawn", '             if record.get("category") == "navigation_node" and not record.get("disabled")',
     '             if False'),
)


# Product guarantees (atomic writes, jobs, source registry, hub, install, release, recovery); caught by tests.test_studio_product.
ATOMIC = "tools/btga_studio/core/atomic.py"
JOBS = "tools/btga_studio/core/jobs.py"
SOURCES = "tools/btga_studio/core/sources.py"
APPLOG = "tools/btga_studio/core/applog.py"
HUB = "tools/btga_studio/core/hub.py"
SETTINGS = "tools/btga_studio/core/settings.py"
RELEASE = "tools/btga_studio/packaging/release.py"
APP = "tools/btga_studio/gui/app.py"
PRODUCT_MUTATIONS = (
    (ATOMIC, "partial file left after a failed write", "        try:\n            os.unlink(temporary)\n        except OSError:\n            pass",
     "        pass"),
    (JOBS, "unbounded job queue", "        if len(self.active) >= self.max_pending:", "        if False:"),
    (JOBS, "cancelled job reported as failed", '            except Cancelled:\n                terminal_state = "cancelled"',
     '            except Cancelled:\n                terminal_state = "failed"'),
    (JOBS, "failed job loses its traceback", "                job.detail = traceback.format_exc()", "                job.detail = None"),
    (SOURCES, "damaged registry silently overwritten", "        except ValueError:\n            self._preserve_unreadable()\n            return",
     "        except ValueError:\n            return"),
    (SOURCES, "failed rebuild deletes the working catalog", "        except BaseException:\n            shutil.rmtree(work, ignore_errors=True)\n            raise",
     "        except BaseException:\n            shutil.rmtree(work, ignore_errors=True)\n            shutil.rmtree(target, ignore_errors=True)\n            raise"),
    (APPLOG, "Windows folders leak into logs", '    text = _WINDOWS.sub("…/", text)', "    pass"),
    (HUB, "moved file of another project accepted", "    if expected_id and entry.project_id != expected_id:", "    if False:"),
    (HUB, "rename overwrites another project", "        if target != path and target.exists():", "        if False:"),
    (HUB, "duplicate keeps the original identity", '    project = projects.load_project(Path(path))\n    project.project["id"] = secrets.token_hex(8)',
     '    project = projects.load_project(Path(path))'),
    (SETTINGS, "preferences accept wrong types", "            if isinstance(default, bool) != isinstance(value, bool):\n                continue",
     "            pass"),
    (RELEASE, "zip depends on file times", "            info = zipfile.ZipInfo(f\"{prefix}/{path.relative_to(folder).as_posix()}\", FIXED_TIME)",
     "            info = zipfile.ZipInfo(f\"{prefix}/{path.relative_to(folder).as_posix()}\", __import__('time').localtime(path.stat().st_mtime)[:6])"),
    (APP, "missing project not explained", "        if not path.is_file():\n            self.report_error(\"Project not found\"",
     "        if False:\n            self.report_error(\"Project not found\""),
    (APP, "panel errors escape the frame guard", "        except Exception as error:  # Dear ImGui error recovery repairs the frame's window/ID stacks",
     "        except ZeroDivisionError as error:"),
)


# Installer, Unicode resources, definition history, tour and environment guarantees; caught by the release-gate test modules.
INSTALLER = "tools/btga_studio/core/installer.py"
VERSIONING = "tools/btga_studio/core/versioning.py"
FONTSET = "tools/btga_studio/core/fontset.py"
GUI_FONTS = "tools/btga_studio/gui/fonts.py"
HISTORY = "tools/btga_studio/core/history.py"
TUTORIAL = "tools/btga_studio/core/tutorial.py"
ENVIRONMENT = "tools/btga_studio/core/environment.py"
RELEASE_GATE_TESTS = ("tests.test_studio_installer", "tests.test_studio_history", "tests.test_studio_tutorial_fonts", "tests.test_studio_setup",
                      "tests.test_studio_authoring")
RELEASE_GATE_MUTATIONS = (
    (INSTALLER, "failed install is not rolled back", "    except BaseException:\n        _rollback(journal, system)\n        raise", "    except BaseException:\n        raise"),
    (INSTALLER, "damaged payload accepted", "            if sha256(destination) != info[\"sha256\"]:", "            if False:"),
    (INSTALLER, "unsafe payload path accepted",
     '    if path.is_absolute() or any(part in ("", ".", "..") or ":" in part for part in path.parts):', "    if False:"),
    (INSTALLER, "downgrade without confirmation", "(result.warnings if allow_downgrade else result.blockers)", "result.warnings"),
    (INSTALLER, "upgrade drops files the user added", "                if relative == RECORD or relative in old_files or relative in payload.files:",
     "                if True:"),
    (INSTALLER, "running program not detected", "    if running:\n        result.blockers.append", "    if False:\n        result.blockers.append"),
    (INSTALLER, "uninstall removes user data by default", "    if remove_user_data:\n        for folder in user_data:", "    if True:\n        for folder in user_data:"),
    (INSTALLER, "uninstall without a record deletes user files", "    return relative in PROGRAM_FILES or relative.startswith(PROGRAM_FOLDERS)", "    return True"),
    (INSTALLER, "uninstall command not quoted", 'f"{quote_argument(executable)} --uninstall"', 'f"{executable} --uninstall"'),
    (INSTALLER, "rollback discards a pre-existing shortcut",
     "            previous_bytes = Path(link).read_bytes() if existed else None", "            previous_bytes = None"),
    (VERSIONING, "pre-release sorts after its release", "return (self.numbers, self.pre is None, self.pre or \"\")", "return (self.numbers, self.pre is not None, self.pre or \"\")"),
    (FONTSET, "font with the wrong hash accepted", "path.stat().st_size == entry.size and _sha256(path) == entry.sha256", "path.stat().st_size == entry.size"),
    (GUI_FONTS, "fallback fonts not merged", "        config.merge_mode = number > 0 and bool(primary)", "        config.merge_mode = False"),
    (HISTORY, "failed edit stays applied", "        except BaseException:\n            restore(self.project, before)\n            raise", "        except BaseException:\n            raise"),
    (HISTORY, "typing never stops coalescing", "and now - current.stamp <= COALESCE_SECONDS", "and True"),
    (PROJECT, "undo history counted as unsaved content", "        value = _content(json.loads(dumps(self))) != self._saved_content",
     "        value = dumps(self) != self._saved_text"),
    (PROJECT, "saved sequence numbers are not deterministic", "        data.update(self.unknown)\n        _rank_sequences(data)", "        data.update(self.unknown)"),
    (CONTROLLER, "undo picks the older history", "        if def_seq is not None and (doc_seq is None or def_seq > doc_seq):",
     "        if def_seq is not None and (doc_seq is None or def_seq < doc_seq):"),
    (TUTORIAL, "earlier work completes a tour step", "            if step.key != \"open\":\n                return False", "            pass"),
    (ENVIRONMENT, "installed copy reported as portable", "mode = \"installed\" if record is not None else", "mode = \"portable\" if record is not None else"),
    (RELEASE, "stray files allowed in the release folder", "    if unexpected:\n        raise SystemExit", "    if False:\n        raise SystemExit"),
)


# Importing outside resources, sky panoramas, teleporters and OpenStreetMap cities; caught by tests.test_studio_imports.
IMAGING = "tools/btga_studio/core/imaging.py"
IMPORTS = "tools/btga_studio/core/imports.py"
GEO = "tools/btga_studio/core/geo.py"
OSM = "tools/btga_studio/core/osm.py"
SKY = "tools/btga_studio/core/sky.py"
SKYLINE = "tools/btga_studio/core/skyline.py"
DIAGNOSTICS = "tools/btga_studio/core/diagnostics.py"
IMPORT_TESTS = ("tests.test_studio_imports",)
IMPORT_MUTATIONS = (
    (IMAGING, "PNG palette index unchecked", "        if int(indices.max(initial=0)) >= len(palette):\n            raise ImageError", "        if False:\n            raise ImageError"),
    (IMAGING, "PNG Paeth predictor wrong", "values[i] = (values[i] + (left if pa <= pb and pa <= pc else up[i] if pb <= pc else corner)) & 0xFF",
     "values[i] = (values[i] + (left if pa <= pb else up[i])) & 0xFF"),
    (IMAGING, "JPEG chroma not upsampled per component", "np.repeat(np.repeat(plane, vmax // c[\"v\"], axis=0), hmax // c[\"h\"], axis=1)",
     "np.repeat(np.repeat(plane, 1, axis=0), 1, axis=1)"),
    (IMAGING, "progressive JPEG decoded as baseline", "        if marker in (0xC2, 0xC6, 0xCA, 0xCE):", "        if False:"),
    (IMAGING, "image size limit missing", "    if width > MAX_INPUT_SIDE or height > MAX_INPUT_SIDE or width * height > MAX_INPUT_PIXELS:", "    if False:"),
    (IMPORTS, "OBJ texture rows not flipped", "uvs.append((float(parts[1]), 1.0 - float(parts[2]) if len(parts) > 2 else 1.0))",
     "uvs.append((float(parts[1]), float(parts[2]) if len(parts) > 2 else 0.0))"),
    (IMPORTS, "paths may leave the model folder", "        if resolved.is_file() and folder in resolved.parents:", "        if resolved.is_file():"),
    (IMPORTS, "Z-up models not turned", "        points = np.stack([points[:, 0], points[:, 2], -points[:, 1]], axis=1)", "        pass"),
    (IMPORTS, "removing a model drops textures still in use", "            if key in textures and key not in used:", "            if key in textures:"),
    (IMPORTS, "credit not stored", 'record = {"width": width, "height": height, "rgba": rgba, "wrap": {"s": wrap, "t": wrap}, "alpha": alpha, "format": "rgba8",',
     'options = ImportOptions(group=options.group)\n    record = {"width": width, "height": height, "rgba": rgba, "wrap": {"s": wrap, "t": wrap}, "alpha": alpha, "format": "rgba8",'),
    (PACKAGE, "imported resources not embedded", 'STARTER_PLATFORMS = ("studio", "user")', 'STARTER_PLATFORMS = ("studio",)'),
    (PACKAGE, "sky picture not checked on read", "            if not isinstance(texture, str) or (texture.startswith(", "            if False and (texture.startswith("),
    (PROJECT, "invalid sky accepted", "                environment[\"sky\"] = sky_module.check(sky)", "                environment[\"sky\"] = dict(sky)"),
    (HISTORY, "undo leaves the sky", '            else:\n                entry.base["environment"][0].pop("sky", None)', "            else:\n                pass"),
    (SCENE, "missing import crashes the map", "        except StudioError:  # an imported model missing", "        except ZeroDivisionError:  # an imported model missing"),
    (DIAGNOSTICS, "teleporter without exit accepted", '            if not target:\n                out.append(Diagnostic("error", "TELEPORTER_WITHOUT_EXIT"',
     '            if False:\n                out.append(Diagnostic("error", "TELEPORTER_WITHOUT_EXIT"'),
    (OVERLAY, "generated overlap hides real overlaps",
     'if other["id"] == shape["id"] or (shape.get("allow_overlap") and other.get("allow_overlap")):',
     'if other["id"] == shape["id"] or (shape.get("allow_overlap") or other.get("allow_overlap")):'),
    (GEO, "holes not cut from roofs", "    for hole in hole_rings:\n        ring = _bridge(points, ring, hole)", "    pass"),
    (GEO, "projection ignores latitude", "self.m_per_lon = math.radians(1) * WGS84_A / math.sqrt(w) * math.cos(phi)", "self.m_per_lon = math.radians(1) * WGS84_A / math.sqrt(w)"),
    (OSM, "heights in feet read as metres", "        return round(float(match.group(1)) * 0.3048 + float(match.group(2) or 0) * 0.0254, 3)",
     "        return float(match.group(1))"),
    (OSM, "edge boxes leave the map", "        if not all(self.inside(corner) for corner in corners):\n            return None", "        pass"),
    (OSM, "ballpark filled solid", "                if building.bowl is not None:\n                    field_centre", "                if False:\n                    field_centre"),
    (OSM, "open stadium without a mapped field filled solid", "                    building.bowl = _field_ring(building.outer)", "                    pass"),
    (OSM, "indoor arenas opened up", '                elif building.tags.get("building") == "stadium":', "                else:"),
    (OSM, "gabled roofs become pyramids", "    inset = half_width if hipped else 0.0", "    inset = half_length"),
    (OSM, "sharp ground used everywhere", "                    if math.hypot(origin[0] - centre[0], origin[1] - centre[1]) > radius:", "                    if False:"),
    (OSM, "street lamps left out", '            if kind == "street_lamp":\n                post(', '            if False:\n                post('),
    (OSM, "skyways treated as corridors", "                if all(in_building(p) for p in middles):", "                if True:"),
    (OSM, "ground ignores elevation", "        values = self.terrain.elevation(lat, lon)\n        return np.where(np.isnan(values), self.reference, values)",
     "        return np.full(len(points), self.reference)"),
    (SKY, "sky turned the wrong way", "    return math.sin(angle), -math.cos(angle)", "    return -math.sin(angle), -math.cos(angle)"),
    (SKYLINE, "earth curvature ignored", "    return distance * distance / (2 * EARTH_RADIUS) * (1 - REFRACTION)", "    return distance * 0.0"),
)


SCENARIO = "tools/btga_studio/core/scenario.py"
ACTIONS = "tools/btga_studio/core/actions.py"
ITEMS = "tools/btga_studio/core/items.py"
BOTS = "tools/btga_studio/core/bots.py"
RUNTIME = "tools/btga_studio/core/runtime.py"
AUTHORING = "tools/btga_studio/core/authoring.py"
DRIVE = "tools/btga_studio/core/drive.py"
DRIVE_GUI = "tools/btga_studio/gui/drive_preview.py"
AUTHORING_TESTS = ("tests.test_studio_scenarios", "tests.test_studio_drive")
AUTHORING_MUTATIONS = (
    (SCENARIO, "requirement loops accepted", "    cycle = requirement_cycle({k: v for k, v in objectives.items() if isinstance(v, dict)})\n    if cycle:",
     "    cycle = None\n    if cycle:"),
    (SCENARIO, "trigger loops accepted", "    cycle = trigger_cycle({k: v for k, v in triggers.items() if isinstance(v, dict)})", "    cycle = None"),
    (SCENARIO, "dangling requirement accepted", "            if required not in objectives:\n                err(\"DANGLING_REFERENCE\"",
     "            if False:\n                err(\"DANGLING_REFERENCE\""),
    (SCENARIO, "more targets asked than exist", "            and params[\"count\"] > len(params[\"targets\"]):", "            and params[\"count\"] > 999:"),
    (SCENARIO, "collect ignores the pickups on the map", "        if not unlimited and available < params[\"count\"]:", "        if False:"),
    (SCENARIO, "impossibility not propagated", "            if element_id not in impossible and isinstance(objective, dict) and any(",
     "            if False and isinstance(objective, dict) and any("),
    (SCENARIO, "unreachable areas accepted", "            if a and b and not ctx.navigation.connected(a, b):\n                err(\"UNREACHABLE_OBJECTIVE\"",
     "            if False:\n                err(\"UNREACHABLE_OBJECTIVE\""),
    (SCENARIO, "element IDs not reused deterministically", "    number = 1\n    while f\"{prefix}{number}\" in existing:",
     "    number = len(existing) + 1\n    while f\"{prefix}{number}\" in existing:"),
    (SCENARIO, "scenario without victory accepted", "            err(\"NO_VICTORY\", \"victory\",", "            pass  # err(\"NO_VICTORY\", \"victory\","),
    (SCENARIO, "unknown objective kinds pass", "            err(\"UNSUPPORTED_OBJECTIVE\", f\"{where}.kind\", f\"unknown objective kind {kind!r}\")",
     "            out.append(Problem(\"warning\", \"UNSUPPORTED_OBJECTIVE\", target, f\"{where}.kind\", \"unknown\"))"),
    (PROJECT, "missions not migrated", "        scenarios[key] = scenario_module.from_mission(key, mission)", "        pass"),
    (HISTORY, "scenarios outside undo", "FAMILIES = (\"campaigns\", \"missions\", \"multiplayer\", \"minigames\", \"tanks\", \"gangs\", \"scenarios\", \"items\")",
     "FAMILIES = (\"campaigns\", \"missions\", \"multiplayer\", \"minigames\", \"tanks\", \"gangs\", \"items\")"),
    (ACTIONS, "action contexts ignored", "    if ctx.context not in spec[\"contexts\"]:", "    if False:"),
    (ACTIONS, "action number ranges ignored",
     "            if not isinstance(value, int) or isinstance(value, bool) or not kind_spec[1] <= value <= kind_spec[2]:",
     "            if not isinstance(value, int) or isinstance(value, bool):"),
    (ITEMS, "item grant loops accepted", "    if cycle and len(cycle) > 2:", "    if False:"),
    (ITEMS, "unsupported effects reported as plain field errors",
     "code = \"UNSUPPORTED_EFFECT\" if \"cannot be used\" in message or \"unknown action\" in message else (",
     "code = \"INVALID_FIELD\" if \"cannot be used\" in message or \"unknown action\" in message else ("),
    (ITEMS, "missing item models accepted", "        elif known_models is not None and model not in known_models:", "        elif False:"),
    (BOTS, "duplicate slot claims accepted", "        if len(ids) > 1:\n            for rid in ids:", "        if len(ids) > 2:\n            for rid in ids:"),
    (BOTS, "runtime slot limit ignored", "        if not isinstance(slot, int) or not 0 <= slot < slot_limit:", "        if not isinstance(slot, int) or not 0 <= slot:"),
    (BOTS, "unresolved Queenlords reported as ordinary missing models",
     "            if value(bot, \"kind\") == 1:\n                out.append(Problem(\"error\", \"QUEENLORD_UNRESOLVED\"",
     "            if False:\n                out.append(Problem(\"error\", \"QUEENLORD_UNRESOLVED\""),
    (BOTS, "dead-end patrols accepted", "                if len(set(route)) < 2:", "                if False:"),
    (BOTS, "unreachable bot targets accepted",
     "                if a and b and not graph.connected(a, b):\n                    out.append(Problem(\"error\", \"UNREACHABLE_TARGET\"",
     "                if False:\n                    out.append(Problem(\"error\", \"UNREACHABLE_TARGET\""),
    (CONTROLLER, "placed bots share a slot", "                    values[\"ai_slot\"] = free", "                    values[\"ai_slot\"] = 0"),
    (RUNTIME, "edited project not detected as stale", "        elif (current_content or content_sha256(project)) != export.content_sha256:", "        elif False:"),
    (RUNTIME, "changed package file not detected", "        if package_hash != export.sha256:", "        if False:"),
    (RUNTIME, "wrong cartridge accepted", "        if wanted and actual not in wanted:", "        if False:"),
    (RUNTIME, "missing player spawn accepted",
     '            if spawn is None or spawn.get("category") not in ("player_spawn", "team_start"):', "            if False:"),
    (RUNTIME, "missing bot placement accepted", "        if unavailable:\n            problems.append((\"INVALID_SELECTION\"",
     "        if False:\n            problems.append((\"INVALID_SELECTION\""),
    (RUNTIME, "runtime missing features accepted", "        if report[\"missing\"]:", "        if False:"),
    (RUNTIME, "launch despite problems", "    if not plan.ok:\n        raise RuntimeProblem", "    if False:\n        raise RuntimeProblem"),
    (RUNTIME, "playability assumed without a runtime", "        return {\"status\": \"unverified\", \"runtime\": None", "        return {\"status\": \"supported\", \"runtime\": None"),
    (RUNTIME, "session ID not unique", "\"session_id\": secrets.token_hex(16)", "\"session_id\": \"0\" * 32"),
    (RUNTIME, "incompatible launch versions accepted", "    if LAUNCH_VERSION not in (data.get(\"launch_request_versions\") or []):", "    if False:"),
    (PACKAGE, "malformed dependency report read",
     "        if not isinstance(dependencies, dict) or not isinstance(dependencies.get(\"game_resources\"), list):", "        if False:"),
    (PACKAGE, "scenarios left out of packages", "\"scenarios\": meta[\"scenarios\"], \"items\": meta[\"items\"], \"extra\"",
     "\"scenarios\": {}, \"items\": meta[\"items\"], \"extra\""),
    (AUTHORING, "unresolved game models reported as resolved", "            if item[\"kind\"] == \"model\" and identifier in library._models:",
     "            if item[\"kind\"] == \"model\":"),
    (DRIVE, "Drive Preview drives into water",
     "        if ground is not None and self.heights.last_water and state.on_ground and abs(state.speed) > 1e-6:", "        if False:"),
    (DRIVE, "Drive Preview claims pickups are collected",
     "text = f\"Pickup{' ' + item if item else ''}: not collected; pickups only work in the game\"",
     "text = f\"Picked up{' ' + item if item else ''}\""),
    (DRIVE_GUI, "Drive Preview disclaimer weakened",
     "\"No game physics, AI, combat, destruction, pickups or scripting run here. Use Test in Game for real behaviour.\"",
     "\"Drive around the map.\""),
)


def run(workdir: Path, tests=TESTS) -> bool:
    env = dict(os.environ, PYOPENGL_PLATFORM=os.environ.get("PYOPENGL_PLATFORM", "egl"), BTGA_MUTATION_GATE="1")
    result = subprocess.run([sys.executable, "-m", "unittest", *tests], cwd=workdir, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def main() -> int:
    if importlib.util.find_spec("numpy") is None:
        print("numpy is unavailable; refusing to treat skipped editor tests as a mutation baseline")
        return 2
    plan = ([(m, TESTS) for m in MUTATIONS] + [(m, ACCEPTANCE_TESTS) for m in ACCEPTANCE_MUTATIONS]
            + [(m, PRODUCT_TESTS) for m in PRODUCT_MUTATIONS] + [(m, RELEASE_GATE_TESTS) for m in RELEASE_GATE_MUTATIONS]
            + [(m, IMPORT_TESTS) for m in IMPORT_MUTATIONS] + [(m, AUTHORING_TESTS) for m in AUTHORING_MUTATIONS])
    font_override = os.environ.get("BTGA_STUDIO_FONT_DIR")
    font_source = Path(font_override) if font_override else None
    if font_source is None or not font_source.is_dir():
        optional = "fallback fonts not merged"
        plan = [(mutation, tests) for mutation, tests in plan if mutation[1] != optional]
        print(f"skipped optional mutation: {optional} (set BTGA_STUDIO_FONT_DIR to a verified staged font directory)")
    originals = {path: (ROOT / path).read_text(encoding="utf-8") for path in {m[0] for m, _ in plan}}
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        for relative in COPIED:
            source, target = ROOT / relative, work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(source, target)
        if font_source is not None:
            shutil.copytree(font_source, work / "private-work" / "toolchains" / "fonts")
        (work / "tools" / "__init__.py").touch()
        for tests in (TESTS, ACCEPTANCE_TESTS, PRODUCT_TESTS, RELEASE_GATE_TESTS, IMPORT_TESTS, AUTHORING_TESTS):
            if not run(work, tests):
                print(f"baseline {' '.join(tests)} fails; refusing to judge mutations")
                return 2
        survivors = []
        for (path, name, before, after), tests in plan:
            original = originals[path]
            if original.count(before) != 1:
                print(f"STALE {name}: original text occurs {original.count(before)} times")
                survivors.append(name)
                continue
            (work / path).write_text(original.replace(before, after), encoding="utf-8")
            passed = run(work, tests)
            (work / path).write_text(original, encoding="utf-8")
            print(f"{'SURVIVED' if passed else 'killed  '} {name}")
            if passed:
                survivors.append(name)
    print(f"{len(plan) - len(survivors)}/{len(plan)} mutations killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
