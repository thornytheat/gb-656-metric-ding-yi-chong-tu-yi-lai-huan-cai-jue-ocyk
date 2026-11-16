"""指标视图：按别名表纠正后的含义算同名冲突、依赖环与采用依据。

这里的「当前含义」完全由传入的别名表决定：同一批定义换一张别名表，
得到的同名分组、签名与依赖边都会变。演练就是拿前后两张表各算一遍再对比。
"""

from __future__ import annotations

from app import errors
from app.modules.m3_compat.metric_rules import signature

MAX_DEFINITIONS = 500
MAX_DEPS = 50


def _norm(value) -> str:
    return str(value or "").strip().lower()


def canonical_name(name: str, aliases: dict[str, str]) -> str:
    key = _norm(name)
    return aliases.get(key, key)


def canonical_deps(depends_on: list[str], aliases: dict[str, str]) -> list[str]:
    return sorted({canonical_name(dep, aliases) for dep in depends_on})


def normalize(definitions) -> list[dict]:
    if not isinstance(definitions, list):
        raise errors.BadInput("definitions 必须是列表", detail={"field": "definitions"})
    if len(definitions) > MAX_DEFINITIONS:
        raise errors.BadInput("定义最多 500 条", detail={"count": len(definitions)})

    out: list[dict] = []
    for index, item in enumerate(definitions):
        if not isinstance(item, dict):
            raise errors.BadInput("定义项必须是对象",
                                  detail={"index": index, "type": type(item).__name__})
        name = str(item.get("name") or "").strip()
        version = str(item.get("version") or "").strip()
        if not name or not version:
            raise errors.BadInput("定义缺少 name 或 version",
                                  detail={"index": index})
        raw_priority = item.get("priority", 0)
        try:
            priority = int(raw_priority or 0)
        except (TypeError, ValueError):
            raise errors.BadInput("priority 必须是整数",
                                  detail={"index": index, "value": repr(raw_priority)})
        deps_raw = item.get("depends_on") or []
        if not isinstance(deps_raw, list):
            raise errors.BadInput("depends_on 必须是列表", detail={"index": index})
        if len(deps_raw) > MAX_DEPS:
            raise errors.BadInput("单个指标依赖最多 50 条",
                                  detail={"index": index, "count": len(deps_raw)})
        depends_on = sorted({str(dep).strip() for dep in deps_raw if str(dep).strip()})
        out.append({
            "index": index,
            "name": name,
            "version": version,
            "expression": str(item.get("expression") or ""),
            "priority": priority,
            "source": str(item.get("source") or ""),
            "depends_on": depends_on,
        })
    return out


def _rank_key(item: dict) -> tuple:
    return (-item["priority"], item["source"], item["name"].lower())


def _cycle_scan(nodes: list[str], edges: dict[str, set[str]]) -> dict:
    """三色 DFS 找环；自引用单独一类。"""
    cycles: dict[tuple[str, ...], tuple[str, ...]] = {}
    color: dict[str, int] = {node: 0 for node in nodes}
    stack: list[str] = []

    def canonical_ring(ring: list[str]) -> tuple[str, ...]:
        pivot = min(range(len(ring)), key=lambda i: ring[i])
        return tuple(ring[pivot:] + ring[:pivot])

    def visit(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            if nxt not in color:
                continue
            if color[nxt] == 0:
                visit(nxt)
            elif color[nxt] == 1 and nxt in stack:
                ring = canonical_ring(stack[stack.index(nxt):])
                cycles[ring] = ring
        stack.pop()
        color[node] = 2

    for node in nodes:
        if color[node] == 0:
            visit(node)
    self_loops = sorted(node for node in nodes if node in edges.get(node, set()))
    return {
        "cycles": [
            {"nodes": list(key), "length": len(key), "reason": "METRIC_DEP_CYCLE",
             "basis": " -> ".join(list(key) + [key[0]])}
            for key in sorted(cycles)
        ],
        "self_loops": [
            {"node": node, "reason": "METRIC_DEP_SELF_LOOP", "basis": f"{node} -> {node}"}
            for node in self_loops
        ],
        "cycle_count": len(cycles),
        "self_loop_count": len(self_loops),
    }


def evaluate(definitions: list[dict], aliases: dict[str, str]) -> dict:
    """按一张别名表给出一份完整视图：分组、签名、采用与环。"""
    enriched: list[dict] = []
    for item in definitions:
        row = dict(item)
        row["canonical_name"] = canonical_name(item["name"], aliases)
        row["signature"] = signature(item["expression"], aliases)
        row["canonical_deps"] = canonical_deps(item["depends_on"], aliases)
        enriched.append(row)

    groups: dict[tuple[str, str], list[dict]] = {}
    for row in enriched:
        groups.setdefault((row["canonical_name"], row["version"]), []).append(row)

    chosen: list[dict] = []
    conflicts: list[dict] = []
    basis: list[dict] = []
    for (name, version), rows in sorted(groups.items()):
        ranked = sorted(rows, key=_rank_key)
        signatures = {row["signature"] for row in rows}
        candidates = [
            {"source": row["source"], "priority": row["priority"],
             "signature": row["signature"], "name": row["name"],
             "expression": row["expression"]}
            for row in ranked
        ]
        if any(not row["expression"] for row in rows):
            conflicts.append({
                "name": name, "version": version, "reason": "DEFINITION_EMPTY",
                "sources": [row["source"] for row in ranked], "candidates": candidates,
                "basis": "存在空定义，无法签名",
            })
            basis.append({"name": name, "version": version, "status": "conflict",
                          "reason": "DEFINITION_EMPTY", "candidates": candidates})
        elif len(signatures) > 1:
            conflicts.append({
                "name": name, "version": version, "reason": "DEFINITION_CONFLICT",
                "sources": [row["source"] for row in ranked], "candidates": candidates,
                "basis": "签名不一致：" + "，".join(sorted(signatures)),
            })
            basis.append({"name": name, "version": version, "status": "conflict",
                          "reason": "DEFINITION_CONFLICT", "candidates": candidates})
        else:
            winner = ranked[0]
            chosen.append({
                "name": name, "version": version, "source": winner["source"],
                "signature": next(iter(signatures)),
                "expression": winner["expression"],
                "canonical_name": name,
                "display_name": winner["name"],
                "depends_on": winner["canonical_deps"],
            })
            basis.append({
                "name": name, "version": version, "status": "chosen",
                "source": winner["source"], "signature": next(iter(signatures)),
                "rule": "priority 降序，同优先级按 source 字典序",
                "candidates": candidates,
            })

    node_names = sorted({row["canonical_name"] for row in enriched})
    edges: dict[str, set[str]] = {node: set() for node in node_names}
    for row in enriched:
        edges.setdefault(row["canonical_name"], set()).update(row["canonical_deps"])
    graph = _cycle_scan(node_names, edges)

    per_metric = [
        {"index": row["index"], "name": row["name"], "canonical_name": row["canonical_name"],
         "version": row["version"], "source": row["source"],
         "signature": row["signature"], "depends_on": row["canonical_deps"]}
        for row in enriched
    ]
    return {
        "metrics": per_metric,
        "chosen": chosen,
        "conflicts": conflicts,
        "basis": basis,
        "graph": {
            "nodes": node_names,
            "edges": {node: sorted(targets) for node, targets in sorted(edges.items())},
        },
        "cycles": graph["cycles"],
        "self_loops": graph["self_loops"],
        "cycle_count": graph["cycle_count"],
        "self_loop_count": graph["self_loop_count"],
        "conflict_count": len(conflicts),
        "chosen_count": len(chosen),
    }


def _cycle_keys(view: dict) -> dict[str, dict]:
    """环与自引用统一成「排序节点串 -> 条目」，便于做前后差集。"""
    out: dict[str, dict] = {}
    for item in view["cycles"]:
        key = " -> ".join(item["nodes"])
        out[key] = {"kind": "cycle", "nodes": item["nodes"], "key": key,
                    "reason": item["reason"], "length": item["length"]}
    for item in view["self_loops"]:
        key = f"{item['node']} -> {item['node']}"
        out[key] = {"kind": "self_loop", "node": item["node"], "key": key,
                    "reason": item["reason"], "length": 1}
    return out


def compare(before: dict, after: dict) -> dict:
    """逐个定义对看修订前后，列出受影响项与变化依据。"""
    before_by_index = {row["index"]: row for row in before["metrics"]}
    after_by_index = {row["index"]: row for row in after["metrics"]}
    chosen_before = {(item["name"], item["version"]): item for item in before["chosen"]}
    chosen_after = {(item["name"], item["version"]): item for item in after["chosen"]}
    basis_before = {(item["name"], item["version"]): item for item in before["basis"]}
    basis_after = {(item["name"], item["version"]): item for item in after["basis"]}
    conflict_before = {(item["name"], item["version"]): item for item in before["conflicts"]}
    conflict_after = {(item["name"], item["version"]): item for item in after["conflicts"]}
    cycles_before = _cycle_keys(before)
    cycles_after = _cycle_keys(after)
    in_cycle_before = {node for item in before["cycles"] for node in item["nodes"]}
    in_cycle_before.update(item["node"] for item in before["self_loops"])
    in_cycle_after = {node for item in after["cycles"] for node in item["nodes"]}
    in_cycle_after.update(item["node"] for item in after["self_loops"])

    affected: list[dict] = []
    for index in sorted(after_by_index):
        old = before_by_index.get(index) or {}
        new = after_by_index[index]
        changes: list[str] = []
        if old.get("canonical_name") != new["canonical_name"]:
            changes.append("canonical_name")
        if old.get("signature") != new["signature"]:
            changes.append("signature")
        if old.get("depends_on") != new["depends_on"]:
            changes.append("dependencies")
        if ((old.get("canonical_name") in in_cycle_before)
                != (new["canonical_name"] in in_cycle_after)):
            changes.append("cycle")
        group_before = chosen_before.get((old.get("canonical_name"), old.get("version")))
        group_after = chosen_after.get((new["canonical_name"], new["version"]))
        basis_before_item = basis_before.get((old.get("canonical_name"), old.get("version")))
        basis_after_item = basis_after.get((new["canonical_name"], new["version"]))
        if (group_before or {}).get("source") != (group_after or {}).get("source"):
            changes.append("adoption")
        if changes:
            affected.append({
                "index": index,
                "name": new["name"],
                "version": new["version"],
                "source": new["source"],
                "changes": changes,
                "before": {key: old.get(key) for key in
                           ("canonical_name", "signature", "depends_on")},
                "after": {key: new[key] for key in
                          ("canonical_name", "signature", "depends_on")},
                "basis": _basis_for(new, group_before, group_after,
                                    basis_before_item, basis_after_item),
            })

    emerged = [conflict_after[key] for key in sorted(conflict_after)
               if key not in conflict_before]
    resolved = [conflict_before[key] for key in sorted(conflict_before)
                if key not in conflict_after]
    cycle_emerged = [cycles_after[key] for key in sorted(cycles_after)
                     if key not in cycles_before]
    cycle_resolved = [cycles_before[key] for key in sorted(cycles_before)
                      if key not in cycles_after]

    return {
        "affected": affected,
        "affected_count": len(affected),
        "conflicts": {
            "before": before["conflicts"],
            "after": after["conflicts"],
            "emerged": emerged,
            "resolved": resolved,
        },
        "cycles": {
            "before": {"cycles": before["cycles"], "self_loops": before["self_loops"]},
            "after": {"cycles": after["cycles"], "self_loops": after["self_loops"]},
            "emerged": cycle_emerged,
            "resolved": cycle_resolved,
        },
    }


def _basis_for(metric: dict, group_before: dict | None, group_after: dict | None,
               basis_before: dict | None = None,
               basis_after: dict | None = None) -> list[str]:
    notes: list[str] = []
    if basis_before and basis_before.get("status") == "chosen":
        notes.append(f"修订前按 {basis_before.get('rule') or '优先级'} 采用 "
                     f"{group_before.get('source') if group_before else '—'}")
    elif basis_before and basis_before.get("status") == "conflict":
        notes.append(f"修订前为冲突（{basis_before.get('reason')}），不自动采用")

    if basis_after and basis_after.get("status") == "chosen":
        candidates = basis_after.get("candidates") or []
        names = "、".join(item["source"] or "—" for item in candidates)
        notes.append(f"修订后按 {basis_after.get('rule') or '优先级'} 采用 "
                     f"{group_after.get('source') if group_after else '—'}"
                     f"（候选：{names or '—'}）")
    elif basis_after and basis_after.get("status") == "conflict":
        notes.append(f"修订后进入冲突（{basis_after.get('reason')}），不再自动采用")

    if group_after is None and group_before is not None:
        notes.append("该指标修订后不再自动采用")
    if group_after is not None and group_before is None:
        notes.append("该指标修订后恢复自动采用")
    return notes
