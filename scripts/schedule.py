#!/usr/bin/env python3
"""Schedule all animated ink on one physical tool track.

Scene element timestamps are local; ``toolTrack`` timestamps are global.  The
element arrays are deliberately never sorted: their order controls occlusion.
``pencil: false`` only changes the appearance of a tool and does *not* exempt an
animated element from scheduling.  Mark a recalled drawing ``instant: true``
instead.  ``drawGroup`` keeps the actions of one diagram together; ``afterId``
can identify an outline which a passive color fill must follow.
"""
from __future__ import annotations

import argparse
import copy
import heapq
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ScheduleError(ValueError):
    """The requested actions cannot be placed safely inside their scenes."""


def _number(value: Any, label: str, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScheduleError(f"{label} must be a finite number")
    if not math.isfinite(value) or value < minimum:
        raise ScheduleError(f"{label} must be finite and >= {minimum:g}")
    return float(value)


def _time(value: float) -> int | float:
    value = round(value, 3)
    return int(value) if value.is_integer() else value


def _deps(element: dict) -> list[str]:
    deps = element.get("afterId", [])
    if deps is None:
        return []
    if isinstance(deps, str):
        return [deps]
    if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
        raise ScheduleError(f"{element.get('id')}.afterId must be an id or list of ids")
    return deps


def _is_ink(element: dict) -> bool:
    if element.get("instant") or element.get("durationMs", 0) == 0:
        return False
    kind = element.get("type")
    if kind in {"stroke", "text", "hatch", "crosshatch", "paint", "erase", "image_ink"}:
        return True
    if kind == "image":
        animation = element.get("animation")
        if isinstance(animation, dict):
            animation = animation.get("type", animation.get("mode"))
        # The runtime defaults image elements to outline drawing.  An explicit
        # fade is passive and does not use the physical tool.
        return animation != "fade" and element.get("mode") != "fade"
    return False


@dataclass(eq=False)
class Action:
    element: dict
    scene_id: str
    offset: float
    scope_start: float
    scope_end: float
    index: int
    original_start: float
    original_duration: float
    minimum: float
    group: tuple[str, str]
    start: float = 0
    end: float = 0
    scheduled: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return self.scene_id, self.element["id"]

    @property
    def locked(self) -> bool:
        return bool(self.element.get("locked", False))

    @property
    def priority(self) -> tuple:
        # Initial persistent headers are completed before scene 1's diagrams.
        return (self.scene_id != "global", self.original_start, self.element.get("sequence", self.index), self.index)


def _topological(items: list, edges: dict, priority, label: str) -> list:
    incoming = {item: 0 for item in items}
    outgoing = {item: set() for item in items}
    for item, dependencies in edges.items():
        for dependency in dependencies:
            if dependency != item and item not in outgoing[dependency]:
                outgoing[dependency].add(item)
                incoming[item] += 1
    ready = [(priority(item), i, item) for i, item in enumerate(items) if incoming[item] == 0]
    heapq.heapify(ready)
    order = []
    unique = {item: i for i, item in enumerate(items)}
    while ready:
        _, _, item = heapq.heappop(ready)
        order.append(item)
        for child in outgoing[item]:
            incoming[child] -= 1
            if not incoming[child]:
                heapq.heappush(ready, (priority(child), unique[child], child))
    if len(order) != len(items):
        raise ScheduleError(f"{label}: cyclic afterId dependencies")
    return order


def _order_actions(actions: list[Action], resolve) -> list[Action]:
    groups: dict[tuple, list[Action]] = {}
    members = set(actions)
    for action in actions:
        groups.setdefault(action.group, []).append(action)
    group_edges = {group: set() for group in groups}
    action_edges = {action: set() for action in actions}
    for action in actions:
        for dep_id in _deps(action.element):
            dependency, _ = resolve(action.scene_id, dep_id)
            if dependency not in members:
                continue
            if dependency == action:
                raise ScheduleError(f"{action.key}: an action cannot depend on itself")
            if dependency.group == action.group:
                action_edges[action].add(dependency)
            else:
                group_edges[action.group].add(dependency.group)
    ordered_groups = _topological(list(groups), group_edges,
                                  lambda group: min(action.priority for action in groups[group]), "drawGroup")
    result = []
    for group in ordered_groups:
        result.extend(_topological(groups[group], {a: action_edges[a] for a in groups[group]},
                                   lambda action: action.priority, str(group)))
    # A scene cannot be drawn before a persistent header and subsequently have
    # a header overlaid partway through it because of an inverted dependency.
    first_local = next((i for i, action in enumerate(result) if action.scene_id != "global"), len(result))
    if any(action.scene_id == "global" for action in result[first_local:]):
        raise ScheduleError("Global header dependencies must not require a scene action first")
    return result


def _hard_start(action: Action, resolve) -> float:
    start = max(action.scope_start, action.offset + _number(action.element.get("notBeforeMs", 0), "notBeforeMs"))
    for dep_id in _deps(action.element):
        dependency, passive = resolve(action.scene_id, dep_id)
        if dependency is not None:
            if dependency.scheduled:
                start = max(start, dependency.end)
        else:
            element, offset = passive
            start = max(start, offset + element["startMs"] + element["durationMs"])
    return start


def _allocate_block(actions: list[Action], scope_start: float, scope_end: float,
                    previous_end: float | None, next_locked: Action | None,
                    min_gap: float, target_gap: float, resolve) -> tuple[list[tuple], float, bool]:
    """Fit a block, first retaining soft cue starts, then compacting if needed."""
    if not actions:
        return [], target_gap, False

    def place(scale: float, keep_cue_starts: bool) -> tuple[list[tuple], float]:
        gap = min_gap + (target_gap - min_gap) * scale
        end_limit = scope_end if next_locked is None else min(scope_end, next_locked.original_start - gap)
        cursor = previous_end
        result = []
        temporary: dict[Action, float] = {}
        for action in actions:
            duration = action.minimum + (max(action.minimum, action.original_duration) - action.minimum) * scale
            start = max(scope_start, _hard_start(action, resolve))
            if cursor is not None:
                start = max(start, cursor + gap)
            if keep_cue_starts:
                start = max(start, action.original_start)
            # A dependency earlier in this block is not committed yet.  Its
            # end is covered by the cursor, including group-local dependencies.
            for dep_id in _deps(action.element):
                dependency, _ = resolve(action.scene_id, dep_id)
                if dependency in temporary:
                    start = max(start, temporary[dependency])
            end = start + duration
            result.append((action, start, end))
            temporary[action] = end
            cursor = end
        return result, end_limit

    for keep_cue_starts in (True, False):
        full, limit = place(1, keep_cue_starts)
        if full[-1][2] <= limit + 1e-7:
            return full, target_gap, not keep_cue_starts
        minimal, limit = place(0, keep_cue_starts)
        if minimal[-1][2] > limit + 1e-7:
            continue
        lo, hi = 0.0, 1.0
        for _ in range(55):
            middle = (lo + hi) / 2
            trial, limit = place(middle, keep_cue_starts)
            if trial[-1][2] <= limit:
                lo = middle
            else:
                hi = middle
        result, _ = place(lo, keep_cue_starts)
        return result, min_gap + (target_gap - min_gap) * lo, not keep_cue_starts
    needed = place(0, False)[0][-1][2]
    deadline = scope_end if next_locked is None else next_locked.original_start - min_gap
    raise ScheduleError(
        f"{actions[0].scene_id}: {len(actions)} drawing actions do not fit; "
        f"minimum end {needed:.1f} ms exceeds deadline {deadline:.1f} ms. "
        "Combine connected strokes, recall existing drawings with instant:true, "
        "or extend the scene; hiding a pencil does not shorten drawing time."
    )


def _commit(action: Action, start: float, end: float) -> None:
    element = action.element
    follows_animation_end = "endMs" in element and abs(element["endMs"] - (element["startMs"] + element["durationMs"])) < .01
    action.start, action.end, action.scheduled = start, end, True
    element["startMs"] = _time(start - action.offset)
    element["durationMs"] = _time(end - start)
    if follows_animation_end:
        element["endMs"] = _time(end - action.offset)


def _passive_fills(owners: list[tuple], actions: list[Action], resolve) -> int:
    count = 0
    for scene_id, offset, start_bound, end_bound, elements in owners:
        local_actions = [action for action in actions if action.scene_id == scene_id]
        for element in elements:
            if element.get("type") != "fill" or element.get("instant") or element["durationMs"] == 0:
                continue
            count += 1
            desired = offset + element["startMs"]
            dependencies = [resolve(scene_id, dep) for dep in _deps(element)]
            matches = [action for action in local_actions if element.get("drawGroup") is not None
                       and action.element.get("drawGroup") == element["drawGroup"]]
            explicit = [action for action, _ in dependencies if action is not None]
            if explicit:
                associated = max(explicit, key=lambda action: action.end)
            else:
                candidates = matches or [action for action in local_actions if action.element.get("d") == element.get("d") and element.get("d")] or local_actions
                associated = min(candidates, key=lambda action: (abs(action.original_start - desired), action.original_start > desired, action.index)) if candidates else None
            required = start_bound
            for action, passive in dependencies:
                if action is not None:
                    required = max(required, action.end)
                else:
                    dep, dep_offset = passive
                    required = max(required, dep_offset + dep["startMs"] + dep["durationMs"])
            if associated is None:
                # With no drawing group a fill is a static paper decoration.
                if max(start_bound, required, desired) > end_bound + .001:
                    raise ScheduleError(f"{scene_id}: passive fill {element['id']!r} cannot follow its dependency inside the scene")
                element["durationMs"] = 0
                element["instant"] = True
                element["startMs"] = _time(max(start_bound, required, desired) - offset)
                continue
            # A scene fill can depend on a persistent global outline.  Also
            # bound a header's final fill by the first local drawing stage.
            association_pool = actions if associated.scene_id == "global" else local_actions
            group = [action for action in association_pool if action.group == associated.group]
            group_start = min(action.start for action in group)
            group_end = max(action.end for action in group)
            next_start = min((action.start for action in association_pool if action.start >= group_end - .001 and action.group != associated.group), default=end_bound)
            deadline = min(end_bound, next_start)
            if required > deadline + .001:
                raise ScheduleError(f"{scene_id}: passive fill {element['id']!r} cannot follow its dependency inside its drawing group")
            fill_start = max(group_start, required, associated.start + max(0, desired - associated.original_start))
            fill_start = min(fill_start, deadline)
            duration = max(0, min(element["durationMs"], deadline - fill_start))
            element["startMs"] = _time(fill_start - offset)
            element["durationMs"] = _time(duration)
            element["passive"] = True
            element["associatedElementId"] = associated.element["id"]
            if "endMs" in element:
                element["endMs"] = _time(fill_start + duration - offset)
    return count


def schedule_document(document: dict, *, min_gap_ms: float = 70,
                      travel_ms: float = 160, min_action_ms: float = 80) -> dict:
    """Return a deep copy with a serial track, or raise ``ScheduleError``.

    ``locked:true`` preserves an action's exact authored interval.  Unlocked
    actions preserve cue starts when possible; crowded scenes proportionally
    compress drawing durations and travel down to their useful minimums.  A
    scene that still cannot fit is rejected instead of silently overlapping.
    Initial global headings share scene 1's available window and are drawn
    first.  Later global drawing actions should be placed in their scene.
    """
    if not isinstance(document, dict):
        raise ScheduleError("The timeline must be a JSON object")
    doc = copy.deepcopy(document)
    doc["toolMode"] = "serial"
    duration = _number(doc.get("durationMs"), "durationMs", 1)
    min_gap = _number(min_gap_ms, "min_gap_ms")
    travel = _number(travel_ms, "travel_ms")
    minimum = _number(min_action_ms, "min_action_ms", 1)
    target_gap = max(min_gap, travel)
    scenes = doc.get("scenes", [])
    if not isinstance(scenes, list):
        raise ScheduleError("scenes must be an array")
    sorted_scenes = sorted(scenes, key=lambda scene: scene.get("startMs", 0))
    previous_scene_end = 0.0
    scene_ids = set()
    owners = [("global", 0.0, 0.0, duration, doc.setdefault("elements", []))]
    for i, scene in enumerate(sorted_scenes):
        scene.setdefault("id", f"scene-{i + 1}")
        scene_id = scene["id"]
        if not isinstance(scene_id, str) or not scene_id or scene_id == "global" or scene_id in scene_ids:
            raise ScheduleError(f"Invalid or duplicate scene id {scene_id!r}")
        scene_ids.add(scene_id)
        start = _number(scene.get("startMs"), f"{scene_id}.startMs")
        end = _number(scene.get("endMs"), f"{scene_id}.endMs")
        if end <= start or end > duration or start < previous_scene_end:
            raise ScheduleError(f"{scene_id}: scenes must have positive, nonoverlapping intervals inside durationMs")
        previous_scene_end = end
        owners.append((scene_id, start, start, end, scene.setdefault("elements", [])))
    references: dict[tuple, tuple] = {}
    actions: list[Action] = []
    for scene_id, offset, scope_start, scope_end, elements in owners:
        if not isinstance(elements, list):
            raise ScheduleError(f"{scene_id}.elements must be an array")
        for index, element in enumerate(elements):
            if not isinstance(element, dict):
                raise ScheduleError(f"{scene_id}.elements[{index}] must be an object")
            if scene_id == 'global' and element.get('type') == 'erase':
                raise ScheduleError('Global erase is unsupported; use a scene target')
            element.setdefault("id", f"element-{index + 1:03d}")
            element.setdefault("startMs", element.get("reveal", {}).get("startMs", 0))
            element.setdefault("durationMs", element.get("reveal", {}).get("durationMs", 0))
            key = (scene_id, element["id"])
            if not isinstance(key[1], str) or not key[1] or key in references:
                raise ScheduleError(f"Invalid or duplicate element id in {scene_id}: {key[1]!r}")
            start = offset + _number(element["startMs"], f"{key}.startMs")
            action_duration = _number(element["durationMs"], f"{key}.durationMs")
            if element.get("instant"):
                if "endMs" in element and abs(element["endMs"] - (element["startMs"] + action_duration)) < .01:
                    element["endMs"] = element["startMs"]
                element["durationMs"] = action_duration = 0
            if start < scope_start or start + action_duration > scope_end + .001:
                raise ScheduleError(f"{key}: authored interval extends outside its scene")
            if _is_ink(element):
                min_duration = max(minimum, _number(element.get("minDurationMs", minimum), f"{key}.minDurationMs", 1))
                group = (scene_id, str(element.get("drawGroup", f"__element_{index}")))
                action = Action(element, scene_id, offset, scope_start, scope_end, index, start, action_duration, min_duration, group)
                actions.append(action)
                references[key] = (action, None)
            else:
                references[key] = (None, (element, offset))

    def resolve(scene_id: str, element_id: str) -> tuple:
        result = references.get((scene_id, element_id), references.get(("global", element_id)))
        if result is None:
            raise ScheduleError(f"{scene_id}: afterId references missing element {element_id!r}")
        return result

    # Validate dependencies even for static and passive elements.
    for scene_id, _, _, _, elements in owners:
        for element in elements:
            for dep_id in _deps(element):
                resolve(scene_id, dep_id)
    scopes = []
    global_actions = [action for action in actions if action.scene_id == "global"]
    if sorted_scenes:
        first = sorted_scenes[0]
        # Persistent headings are an initial drawing stage.  Bind their latest
        # deadline to the first scene rather than consuming later scene time.
        for action in global_actions:
            action.scope_end = first["endMs"]
        scopes.append(("initial", 0.0, first["endMs"], global_actions + [a for a in actions if a.scene_id == first["id"]]))
        scopes.extend((scene["id"], scene["startMs"], scene["endMs"], [a for a in actions if a.scene_id == scene["id"]]) for scene in sorted_scenes[1:])
    else:
        scopes.append(("global", 0.0, duration, global_actions))
    previous_end = None
    ordered_actions = []
    scope_diagnostics = []
    for scope_id, scope_start, scope_end, scope_actions in scopes:
        ordered = _order_actions(scope_actions, resolve)
        block: list[Action] = []
        moved_earlier = False
        gaps = []
        for action in ordered + [None]:
            if action is not None and not action.locked:
                block.append(action)
                continue
            placements, gap, compacted = _allocate_block(block, scope_start, scope_end, previous_end, action, min_gap, target_gap, resolve)
            gaps.append(gap)
            moved_earlier |= compacted
            for unlocked, start, end in placements:
                _commit(unlocked, start, end)
                ordered_actions.append(unlocked)
                previous_end = end
            block = []
            if action is None:
                continue
            start, end = action.original_start, action.original_start + action.original_duration
            hard_start = _hard_start(action, resolve)
            if start < scope_start or end > scope_end or start < hard_start - .001:
                raise ScheduleError(f"{action.key}: locked action violates scene bounds or afterId")
            if previous_end is not None and start < previous_end + min_gap - .001:
                raise ScheduleError(f"{action.key}: locked action overlaps the tool track or leaves less than {min_gap:g} ms travel")
            _commit(action, start, end)
            ordered_actions.append(action)
            previous_end = end
        scope_diagnostics.append({"scopeId": scope_id, "actionCount": len(ordered), "compactedCueStarts": moved_earlier,
                                  "minimumActualGapMs": _time(min(gaps, default=target_gap))})
    passive_count = _passive_fills(owners, ordered_actions, resolve)
    tool_track = []
    previous_end = None
    for action in ordered_actions:
        element = action.element
        travel_start = max(0, action.start - travel, previous_end if previous_end is not None else 0)
        item = {"sceneId": action.scene_id, "elementId": element["id"],
                "startMs": _time(action.start), "endMs": _time(action.end),
                "travelStartMs": _time(travel_start),
                "tool": element.get("tool", "eraser" if element.get("type") == "erase" else doc.get("defaultTool", "pencil"))}
        if "drawGroup" in element:
            item["drawGroup"] = element["drawGroup"]
        if "color" in element:
            item["color"] = element["color"]
        tool_track.append(item)
        previous_end = action.end
    overlaps = sum(second["startMs"] < first["endMs"] - .001 for first, second in zip(tool_track, tool_track[1:]))
    if overlaps:
        raise ScheduleError(f"Internal scheduling failure: {overlaps} overlapping tool actions")
    doc["toolTrack"] = tool_track
    doc["serialSchedule"] = {"version": 1, "actionCount": len(tool_track), "overlapCount": overlaps,
                             "maxSimultaneousInk": 1 if tool_track else 0, "passiveFillCount": passive_count,
                             "minGapMs": _time(min_gap), "travelMs": _time(travel), "minActionMs": _time(minimum),
                             "compressedActionCount": sum(a.end - a.start < a.original_duration - .001 for a in actions),
                             "shiftedActionCount": sum(abs(a.start - a.original_start) > .001 for a in actions),
                             "scopes": scope_diagnostics}
    return doc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-gap-ms", type=float, default=70)
    parser.add_argument("--travel-ms", type=float, default=160)
    parser.add_argument("--min-action-ms", type=float, default=80)
    args = parser.parse_args()
    try:
        document = json.loads(args.input.read_text(encoding="utf-8-sig"))
        scheduled = schedule_document(document, min_gap_ms=args.min_gap_ms, travel_ms=args.travel_ms,
                                      min_action_ms=args.min_action_ms)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(scheduled, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ScheduleError, OSError, json.JSONDecodeError) as error:
        parser.exit(2, f"Serial scheduling failed: {error}\n")
    print(json.dumps(scheduled["serialSchedule"], ensure_ascii=False))


if __name__ == "__main__":
    main()
