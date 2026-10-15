"""Navigation graph of a map, for reachability checks (bots, objectives, Drive Preview overlays).

Two sources, whichever the map has:

* Studio navigation nodes: markers of category ``navigation_node`` whose
  ``link_*`` fields name other nodes (blank and city maps);
* decoded source navigation: ``scene["navigation"]`` with ``points`` and
  ``links`` (``{"index", "targets"}``), as the original BattleTanx adapter
  writes.  Global Assault catalogs carry none (``navigation: None``).

Links are stored as directed, as the data says; reachability treats a link
as passable both ways (a road can be driven back), while ``patrol_route``
follows the stored direction.  Disabled nodes are left out.
"""

from __future__ import annotations

import math
from collections import deque


class NavGraph:
    def __init__(self):
        self.nodes: dict[str, tuple[float, float]] = {}
        self.links: dict[str, list[str]] = {}
        self.source = "none"

    @classmethod
    def from_scene(cls, scene) -> "NavGraph":
        """Build from an ``EditorScene`` (or anything with ``records`` and ``navigation``)."""

        graph = cls()
        markers = [r for _, r in scene.records("markers") if r.get("category") == "navigation_node" and not r.get("disabled")]
        if markers:
            graph.source = "studio"
            for record in markers:
                graph.nodes[record["id"]] = (float(record["translation"][0]), float(record["translation"][2]))
            for record in markers:
                targets = []
                for name in sorted(record.get("fields", {})):
                    value = record["fields"][name].get("value") if name.startswith("link_") else None
                    if isinstance(value, str) and value in graph.nodes and value != record["id"]:
                        targets.append(value)
                graph.links[record["id"]] = targets
            return graph
        navigation = getattr(scene, "navigation", None)
        if navigation is None and hasattr(scene, "base"):
            navigation = scene.base.get("navigation")
        if navigation and navigation.get("points"):
            graph.source = "decoded"
            for number, point in enumerate(navigation["points"]):
                graph.nodes[f"nav:{number}"] = (float(point["x"]), float(point["z"]))
            for link in navigation.get("links", []):
                key = f"nav:{link.get('index')}"
                if key in graph.nodes:
                    graph.links[key] = [f"nav:{t}" for t in link.get("targets", []) if isinstance(t, int) and f"nav:{t}" in graph.nodes]
        return graph

    def __bool__(self) -> bool:
        return bool(self.nodes)

    def neighbours(self, node: str) -> set[str]:
        out = set(self.links.get(node, []))
        out |= {other for other, targets in self.links.items() if node in targets}
        return out

    def nearest(self, x: float, z: float, limit: float | None = None) -> str | None:
        best, best_distance = None, math.inf
        for node, (nx, nz) in sorted(self.nodes.items()):
            distance = math.hypot(nx - x, nz - z)
            if distance < best_distance:
                best, best_distance = node, distance
        if limit is not None and best_distance > limit:
            return None
        return best

    def reachable(self, start: str) -> set[str]:
        seen = {start}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            for other in sorted(self.neighbours(node)):
                if other not in seen:
                    seen.add(other)
                    queue.append(other)
        return seen

    def connected(self, a: str, b: str) -> bool:
        return a in self.nodes and b in self.nodes and b in self.reachable(a)

    def patrol_route(self, start: str, limit: int = 64) -> list[str]:
        """Nodes visited following the first stored link from ``start`` until a node repeats or the route ends."""

        route = [start]
        node = start
        while len(route) < limit:
            targets = self.links.get(node, [])
            if not targets:
                break
            node = targets[0]
            if node in route:
                route.append(node)
                break
            route.append(node)
        return route

    def components(self) -> list[set[str]]:
        left = set(self.nodes)
        out = []
        while left:
            start = min(left)
            part = self.reachable(start)
            out.append(part)
            left -= part
        return out
