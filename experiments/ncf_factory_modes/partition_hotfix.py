"""Deterministic repair for MiniFlash cyclic region interleaves.

MiniFlash commit ``a2227e`` can choose a front region that is not part of the
blocking cycle, then attempt a cut outside that region.  This adapter keeps the
vendored compiler immutable while replacing only its private serialization
helper with a cycle-SCC-aware implementation.  Remove it after an equivalent
upstream fix is pinned and bump every physical provenance schema at that time.
"""

from __future__ import annotations

import hashlib
import importlib
from itertools import pairwise
from pathlib import Path

HOTFIX_SCHEMA = "ncf_miniflash_partition_cyclic_scc_hotfix_v1"


def _strongly_connected_components(graph):
    """Return deterministic SCCs without recursive-depth dependence."""

    nodes = sorted(graph, key=repr)
    visited = set()
    finish_order = []
    for root in nodes:
        if root in visited:
            continue
        visited.add(root)
        stack = [(root, iter(sorted(graph.get(root, ()), key=repr)))]
        while stack:
            node, successors = stack[-1]
            try:
                successor = next(successors)
            except StopIteration:
                stack.pop()
                finish_order.append(node)
                continue
            if successor not in visited:
                visited.add(successor)
                stack.append(
                    (
                        successor,
                        iter(sorted(graph.get(successor, ()), key=repr)),
                    )
                )

    reverse = {node: set() for node in nodes}
    for node, successors in graph.items():
        for successor in successors:
            reverse.setdefault(successor, set()).add(node)

    assigned = set()
    components = []
    for root in reversed(finish_order):
        if root in assigned:
            continue
        assigned.add(root)
        component = []
        stack = [root]
        while stack:
            node = stack.pop()
            component.append(node)
            for predecessor in sorted(reverse.get(node, ()), key=repr):
                if predecessor not in assigned:
                    assigned.add(predecessor)
                    stack.append(predecessor)
        components.append(tuple(sorted(component, key=repr)))
    return tuple(components)


def _fixed_serialize_regions(regions, events):
    from miniflash.partition import _split_region_at
    from miniflash.schedule import _region_anchors

    initial_size = len(regions) + len(events)
    for _ in range(max(64, 16 * (initial_size + 1))):
        sequence = {}
        for order, region in enumerate(regions):
            for qubit, anchor in _region_anchors(region).items():
                sequence.setdefault(qubit, []).append(
                    (anchor, order, ("region", order))
                )
        for order, event in enumerate(events):
            sequence.setdefault(event.qubit, []).append(
                (event.index, len(regions) + order, ("event", order))
            )
        position = {}
        for qubit, timeline in sequence.items():
            timeline.sort(key=lambda item: (item[0], item[1]))
            position[qubit] = 0
        pending = {("region", order) for order in range(len(regions))} | {
            ("event", order) for order in range(len(events))
        }

        def qubits_of(entry):
            if entry[0] == "region":
                return regions[entry[1]].qubits
            return [events[entry[1]].qubit]

        need = {entry: len(qubits_of(entry)) for entry in pending}
        at_front = {}
        for qubit in sequence:
            entry = sequence[qubit][0][2]
            at_front[entry] = at_front.get(entry, 0) + 1
        queue = sorted(
            (entry for entry in pending if at_front.get(entry, 0) == need[entry]),
            key=repr,
            reverse=True,
        )
        while queue:
            entry = queue.pop()
            if entry not in pending:
                continue
            pending.remove(entry)
            for qubit in qubits_of(entry):
                position[qubit] += 1
                if position[qubit] < len(sequence[qubit]):
                    successor = sequence[qubit][position[qubit]][2]
                    at_front[successor] = at_front.get(successor, 0) + 1
                    if at_front[successor] == need[successor]:
                        queue.append(successor)
                        queue.sort(key=repr, reverse=True)
        if not pending:
            return regions

        graph = {entry: set() for entry in pending}
        for qubit, timeline in sequence.items():
            remaining = [
                item[2] for item in timeline[position[qubit] :] if item[2] in pending
            ]
            for left, right in pairwise(remaining):
                graph[left].add(right)
        cyclic = [
            component
            for component in _strongly_connected_components(graph)
            if len(component) > 1
            or any(member in graph[member] for member in component)
        ]
        cyclic.sort(key=lambda component: tuple(map(repr, component)))
        cycle = cyclic[0] if cyclic else tuple(sorted(pending, key=repr))
        region_orders = sorted(
            (entry[1] for entry in cycle if entry[0] == "region"),
            key=lambda order: (
                -(regions[order].gate_indices[-1] - regions[order].gate_indices[0]),
                regions[order].gate_indices[0],
                order,
            ),
        )
        split = None
        diagnostics = []
        for target in region_orders:
            region = regions[target]
            boundaries = set(region.gate_indices[1:])
            for entry in cycle:
                if entry == ("region", target):
                    continue
                if entry[0] == "region":
                    competitor = regions[entry[1]]
                    boundaries.add(competitor.gate_indices[0])
                    boundaries.add(competitor.gate_indices[-1] + 1)
                else:
                    boundaries.add(events[entry[1]].index)
                    boundaries.add(events[entry[1]].index + 1)
            valid_boundaries = sorted(
                boundary
                for boundary in boundaries
                if region.gate_indices[0] < boundary <= region.gate_indices[-1]
            )
            diagnostics.append(
                {
                    "target": target,
                    "indices": tuple(region.gate_indices),
                    "boundaries": tuple(valid_boundaries),
                }
            )
            for boundary in valid_boundaries:
                parts = _split_region_at(region, boundary)
                if parts is not None:
                    split = (target, parts)
                    break
            if split is not None:
                break
        if split is None:
            raise ValueError(
                "partition: cyclic region interleave could not be split; "
                f"cycle={cycle!r}; candidates={diagnostics!r}"
            )
        target, parts = split
        regions[target : target + 1] = parts
        regions.sort(
            key=lambda region: (
                region.first_gate_index,
                tuple(region.gate_indices),
                tuple(region.qubits),
            )
        )
    raise ValueError(
        "partition: region serialization did not converge after deterministic "
        f"SCC splitting; regions={len(regions)}; events={len(events)}"
    )


def install() -> dict:
    """Install the adapter into the already importable MiniFlash package."""

    # ``miniflash.__init__`` exports a function named ``partition``.  The
    # dotted import spelling can therefore bind that function instead of the
    # submodule on supported Python versions; import the module explicitly.
    partition_module = importlib.import_module("miniflash.partition")

    partition_module._serialize_regions = _fixed_serialize_regions
    source = Path(__file__).read_bytes()
    return {
        "schema": HOTFIX_SCHEMA,
        "sha256": hashlib.sha256(source).hexdigest(),
        "installed": True,
    }


__all__ = ["HOTFIX_SCHEMA", "install"]
