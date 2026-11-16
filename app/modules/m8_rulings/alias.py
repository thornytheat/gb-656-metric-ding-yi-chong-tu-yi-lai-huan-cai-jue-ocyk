"""指标别名表：归一、纠正误映射、歧义留待处理。

别名表是「同义词 → 正名」的映射。一次修订就是一张完整的表，所以纠正一条
误映射只需在新修订里换掉目标，而不是在原表上就地改写——历史裁决各自钉住
自己当时的整张表，旧结论不会被后来的纠正改写。

整治原则：
- 同一个别名在同一修订里指向两个不同目标 → 歧义，不进有效映射，留待处理。
- 别名只指向自己、别名或目标为空 → 无效条目，直接拒绝提交。
- 链式别名可解析，但成环或多目标途中截断同样算歧义，不猜测合并。
"""

from __future__ import annotations

import hashlib
import json

MAX_HOPS = 12


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def normalize_entries(raw_entries) -> dict:
    """把提交上来的别名条目归一成有效表与待处理清单。

    无效条目（空、自指、非对象）直接抛给调用方当输入错误；
    歧义条目（同别名多目标、链式成环）留在 pending 里，不进有效映射。
    """
    from app import errors

    if not isinstance(raw_entries, list):
        raise errors.BadInput("entries 必须是列表", detail={"field": "entries"})
    if len(raw_entries) > 500:
        raise errors.BadInput("别名条目最多 500 条", detail={"count": len(raw_entries)})

    targets: dict[str, set[str]] = {}
    notes: dict[str, str] = {}

    for index, item in enumerate(raw_entries, 1):
        if not isinstance(item, dict):
            raise errors.BadInput("别名条目必须是对象",
                                  detail={"index": index, "type": type(item).__name__})
        alias = _norm(item.get("alias"))
        target = _norm(item.get("target"))
        if not alias or not target:
            raise errors.BadInput("别名条目缺少 alias 或 target",
                                  detail={"index": index})
        if alias == target:
            raise errors.BadInput("别名与目标相同", detail={"index": index, "alias": alias})
        targets.setdefault(alias, set()).add(target)
        note = str(item.get("note") or "").strip()
        if note:
            notes[alias] = note

    entries: list[dict] = []
    pending: list[dict] = []
    for alias in sorted(targets):
        found = targets[alias]
        if len(found) > 1:
            pending.append({
                "alias": alias,
                "targets": sorted(found),
                "target": None,
                "reason": "ALIAS_AMBIGUOUS",
                "detail": "同一别名指向多个目标，按规则不猜测合并",
                "note": notes.get(alias, ""),
            })
            continue
        entries.append({"alias": alias, "target": next(iter(found)),
                        "note": notes.get(alias, "")})

    # 链式解析：成环、途中多目标或断链的别名一律挂起；移除后可能牵动别的链，
    # 迭代到稳定，保证留下的每条链都能唯一解析到一个正名。
    while True:
        mapping = {entry["alias"]: entry["target"] for entry in entries}
        results = {alias: resolve(mapping, alias) for alias in mapping}
        drop = {alias for alias, result in results.items()
                if result["cycle"] or result["ambiguous"]}
        if not drop:
            break
        kept: list[dict] = []
        for entry in entries:
            if entry["alias"] not in drop:
                kept.append(entry)
                continue
            reason = "ALIAS_CYCLE" if results[entry["alias"]]["cycle"] else "ALIAS_AMBIGUOUS"
            pending.append({
                "alias": entry["alias"],
                "targets": [entry["target"]],
                "target": None,
                "reason": reason,
                "detail": ("别名链成环，按规则不猜测合并" if reason == "ALIAS_CYCLE"
                           else "别名链途中出现多目标，按规则不猜测合并"),
                "note": entry.get("note", ""),
            })
        entries = kept

    mapping = {entry["alias"]: entry["target"] for entry in entries}
    paths = {entry["alias"]: resolve(mapping, entry["alias"])["path"]
             for entry in entries}
    effective = {entry["alias"]: paths[entry["alias"]][-1] for entry in entries}
    pending.sort(key=lambda item: (item["alias"], item["reason"]))
    return {
        "entries": entries,
        "pending": pending,
        "map": effective,
        "paths": paths,
        "notes": notes,
        "sha256": digest_entries(entries, pending),
        "ambiguous": any(item["reason"] == "ALIAS_AMBIGUOUS" for item in pending),
    }


def resolve(mapping: dict[str, str], alias: str) -> dict:
    """顺着别名链走到正名；多目标、成环、断链都明确标出来。"""
    key = _norm(alias)
    path = [key]
    seen = {key}
    hops = 0
    while hops < MAX_HOPS:
        target = mapping.get(key)
        if target is None:
            return {"canonical": path[-1], "path": path, "cycle": False,
                    "ambiguous": False, "missing": False, "hops": hops}
        path.append(target)
        if target in seen:
            return {"canonical": path[-1], "path": path, "cycle": True,
                    "ambiguous": False, "missing": False, "hops": hops + 1}
        seen.add(target)
        key = target
        hops += 1
    return {"canonical": path[-1], "path": path, "cycle": False,
            "ambiguous": True, "missing": False, "hops": hops}


def expand(entries: list[dict]) -> dict[str, str]:
    """把条目展开成 alias → 最终正名；供表达式替换用。"""
    mapping = {entry["alias"]: entry["target"] for entry in entries}
    out: dict[str, str] = {}
    for entry in entries:
        result = resolve(mapping, entry["alias"])
        out[entry["alias"]] = result["canonical"]
    return out


def digest_entries(entries: list[dict], pending: list[dict] | None = None) -> str:
    """只摘要语义（别名与目标、待处理项），说明文字不影响演练与发布的口径。"""
    semantic_entries = [{"alias": entry["alias"], "target": entry["target"]}
                        for entry in entries]
    semantic_pending = [
        {key: item.get(key) for key in ("alias", "targets", "reason")}
        for item in (pending or [])
    ]
    return _digest({"entries": semantic_entries, "pending": semantic_pending})


def snapshot(entries: list[dict], pending: list[dict] | None = None) -> dict:
    """裁决落盘时钉住的别名快照：以后读旧裁决只认这一份。"""
    return {
        "entries": [dict(entry) for entry in entries],
        "pending": [dict(item) for item in (pending or [])],
        "sha256": digest_entries(entries, pending),
        "map": expand(entries),
    }
