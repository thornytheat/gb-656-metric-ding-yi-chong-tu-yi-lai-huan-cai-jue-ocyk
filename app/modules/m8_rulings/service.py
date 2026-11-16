"""裁决版本编排：别名修订演练 → 受影响指标 → 发布新裁决版本。

核心约定：
- 演练与发布都以「这一版别名表」为准，算出的视图随版本钉住；
- 已发布的裁决读出来时还原它当时的别名快照，后续纠正不改写旧结论；
- 发布键由「别名规则摘要 + 定义摘要」决定，重试不会新增裁决；
- 写入是一个事务，失败整笔回滚，目录退回上一版仍可用；
- 有歧义的映射只挂起、不合并，等人工处理。
"""

from __future__ import annotations

import hashlib
import json

from app import config, errors, store
from app.modules.m8_rulings import alias as alias_mod
from app.modules.m8_rulings import metrics

DRY_RUN_LIMIT = 40
SAMPLE_FILE = "metric_aliases_v2026_11.json"
SAMPLE_BASE_FILE = "metric_aliases_v2026_10.json"
RULE_POINTER_KEY = "alias_rule_current"


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _definitions_digest(definitions: list[dict]) -> str:
    payload = [
        {key: row[key] for key in
         ("name", "version", "expression", "priority", "source", "depends_on")}
        for row in definitions
    ]
    return _digest(payload)


def rule_digest(alias_snapshot: dict) -> str:
    """别名规则的版本摘要：换表或换规则集都会得到新值。"""
    return _digest({"ruleset": config.RULESET_VERSION, "aliases": alias_snapshot["sha256"]})


def _load_json(rel_name: str) -> dict:
    path = config.resolve_sample(rel_name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise errors.BadInput("样例 JSON 无法解析",
                              detail={"file": path.name, "reason": exc.msg})
    if not isinstance(payload, dict):
        raise errors.BadInput("样例顶层必须是对象", detail={"file": path.name})
    return payload


# ---------- 输入读取 ----------

def load_definitions(rel_name: str) -> list[dict]:
    payload = _load_json(rel_name)
    raw = payload.get("definitions")
    if raw is None:
        raise errors.BadInput("定义文件缺少 definitions 列表", detail={"file": rel_name})
    return metrics.normalize(raw)


def load_alias_entries(rel_name: str) -> list[dict]:
    payload = _load_json(rel_name)
    raw = payload.get("aliases")
    if raw is None:
        raise errors.BadInput("别名文件缺少 aliases 列表", detail={"file": rel_name})
    if isinstance(raw, dict):
        raw = [{"alias": key, "target": value} for key, value in raw.items()]
    return raw


def _entries_from_body(body: dict, key: str) -> list[dict] | None:
    """从请求体取别名条目：直接给列表，或给文件名。"""
    file_key = f"{key}_file"
    if body.get(file_key):
        return load_alias_entries(str(body[file_key]))
    raw = body.get(key)
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = [{"alias": item, "target": target} for item, target in raw.items()]
    return raw


def _definitions_from_body(body: dict) -> tuple[list[dict], str]:
    if body.get("definitions_file"):
        name = str(body["definitions_file"])
        return load_definitions(name), f"file:{name}"
    if isinstance(body.get("definitions"), list):
        return metrics.normalize(body["definitions"]), "inline"
    raise errors.BadInput("需要 definitions 列表或 definitions_file",
                          detail={"field": "definitions"})


def _snapshot_from_entries(entries: list[dict]) -> dict:
    normalized = alias_mod.normalize_entries(entries)
    return alias_mod.snapshot(normalized["entries"], normalized["pending"])


def _base_snapshot() -> dict:
    """修订前的别名表：以最近一版已发布裁决钉住的快照为准。"""
    ruling = store.latest_ruling()
    if ruling is None:
        return alias_mod.snapshot([], [])
    pinned = ruling.get("aliases") or {}
    return {
        "entries": [dict(item) for item in pinned.get("entries") or []],
        "pending": [dict(item) for item in pinned.get("pending") or []],
        "sha256": pinned.get("sha256") or alias_mod.snapshot(
            pinned.get("entries") or [], pinned.get("pending") or [])["sha256"],
        "map": dict(pinned.get("map") or {}),
    }


# ---------- 演练 ----------

def create_dry_run(definitions: list[dict], alias_entries: list[dict],
                   base_entries: list[dict] | None = None,
                   revision_id: str | None = None) -> dict:
    """拿一版别名表给整批定义做一次演练，不算发布。"""
    revision = _snapshot_from_entries(alias_entries)
    base = _snapshot_from_entries(base_entries) if base_entries is not None \
        else _base_snapshot()

    defs_sha = _definitions_digest(definitions)
    rules_sha = rule_digest(revision)
    before = metrics.evaluate(definitions, base["map"])
    after = metrics.evaluate(definitions, revision["map"])
    diff = metrics.compare(before, after)
    dry_run_id = "dry-" + _digest({
        "rule_sha256": rules_sha,
        "definitions_sha256": defs_sha,
        "base_sha256": base["sha256"],
    })[:12]

    payload = {
        "dry_run_id": dry_run_id,
        "created_at": config.now_utc().isoformat(),
        "ruleset": config.RULESET_VERSION,
        "revision_id": (str(revision_id).strip() if revision_id else None),
        "rule_sha256": rules_sha,
        "definitions_sha256": defs_sha,
        "aliases_sha256": revision["sha256"],
        "definitions": definitions,
        "aliases": revision,
        "base_aliases": base,
        "pending": revision["pending"],
        "before": before,
        "after": after,
        "diff": diff,
    }
    payload["warnings"] = _dry_run_warnings(payload)
    return store.save_dry_run(payload)


def _dry_run_warnings(payload: dict) -> list[dict]:
    warnings: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(code: str, detail: str, **extra) -> None:
        key = (code, detail)
        if key in seen:
            return
        seen.add(key)
        warnings.append(dict({"code": code, "detail": detail}, **extra))

    for item in payload["pending"]:
        add(item["reason"], f"{item['alias']}：{item['detail']}", alias=item["alias"])
    diff = payload["diff"]
    for item in diff["conflicts"]["emerged"]:
        add("CONFLICT_EMERGED", f"{item['name']}@{item['version']} 修订后签名不一致",
            name=item["name"])
    for item in diff["conflicts"]["resolved"]:
        add("CONFLICT_RESOLVED", f"{item['name']}@{item['version']} 修订后签名一致",
            name=item["name"])
    for item in diff["cycles"]["emerged"]:
        add("CYCLE_EMERGED", _cycle_text(item), nodes=_cycle_list(item))
    for item in diff["cycles"]["resolved"]:
        add("CYCLE_RESOLVED", _cycle_text(item), nodes=_cycle_list(item))
    for item in diff["affected"]:
        if "adoption" in item["changes"]:
            add("BASIS_CHANGED", f"{item['name']}@{item['version']} 采用来源变化",
                name=item["name"])
    return warnings


def _cycle_text(item: dict) -> str:
    nodes = _cycle_list(item)
    if item.get("kind") == "self_loop":
        return f"{nodes[0]} → {nodes[0]}"
    return " → ".join(nodes + [nodes[0]])


def _cycle_list(item: dict) -> list[str]:
    return list(item.get("nodes") or [item.get("node")])


def current_rule_digest() -> str | None:
    """当前生效的别名规则摘要：以最近一次提交的规则为准。

    演练按内容去重，重交同一份不会新增行，所以当前规则另用一个指针记录，
    提交时无论是否命中已有演练都要前移。发布只认指针指向的那一条。
    """
    pointer = store.get_kv(RULE_POINTER_KEY)
    if isinstance(pointer, dict) and pointer.get("rule_sha256"):
        return pointer["rule_sha256"]
    latest = store.latest_dry_run()
    if latest is not None:
        return latest["rule_sha256"]
    ruling = store.latest_ruling()
    return ruling["rule_sha256"] if ruling else None


def _mark_rule_current(dry: dict) -> None:
    store.set_kv(RULE_POINTER_KEY, {"rule_sha256": dry["rule_sha256"],
                                    "dry_run_id": dry["dry_run_id"],
                                    "updated_at": config.now_utc().isoformat()})


def get_dry_run(dry_run_id: str) -> dict:
    dry = store.get_dry_run(dry_run_id)
    if dry is None:
        raise errors.NotFound("没有这次演练", detail={"dry_run_id": dry_run_id})
    return dry


def list_dry_runs() -> list[dict]:
    return store.list_dry_runs(DRY_RUN_LIMIT)


def dry_run_from_body(body: dict) -> dict:
    if body.get("sample"):
        definitions = load_definitions(SAMPLE_FILE)
        entries = load_alias_entries(SAMPLE_FILE)
        source = f"file:{SAMPLE_FILE}"
        base_entries = load_alias_entries(SAMPLE_BASE_FILE)
    else:
        definitions, source = _definitions_from_body(body)
        entries = _entries_from_body(body, "aliases")
        if entries is None:
            raise errors.BadInput("需要 aliases 列表或 aliases_file",
                                  detail={"field": "aliases"})
        base_entries = _entries_from_body(body, "base_aliases")
    dry = create_dry_run(definitions, entries, base_entries=base_entries,
                         revision_id=body.get("revision_id"))
    _mark_rule_current(dry)
    return dict(dry, definitions_source=source)


# ---------- 发布 ----------

def publish(dry_run_id: str) -> dict:
    """把一次演练发布成新的裁决版本；同一内容重试不新增版本。"""
    if not dry_run_id:
        raise errors.BadInput("需要 dry_run_id", detail={"field": "dry_run_id"})
    dry = get_dry_run(str(dry_run_id))

    publish_key = _digest({"rule_sha256": dry["rule_sha256"],
                           "definitions_sha256": dry["definitions_sha256"]})
    existing = store.find_ruling_by_key(publish_key)
    if existing is not None:
        return dict(existing, reused=True)

    current_rule = current_rule_digest()
    if current_rule != dry["rule_sha256"]:
        raise errors.AliasRevisionStale(
            "别名规则已更新，这次演练已失效，请重新演练",
            detail={"reason": "ALIAS_REVISION_STALE",
                    "dry_run_id": dry["dry_run_id"],
                    "dry_run_rule_sha256": dry["rule_sha256"],
                    "current_rule_sha256": current_rule,
                    "latest_dry_run_id": (store.latest_dry_run() or {}).get("dry_run_id")},
        )

    created_at = config.now_utc().isoformat()
    ruling = {
        "ruling_id": "rv-" + publish_key[:12],
        "publish_key": publish_key,
        "dry_run_id": dry["dry_run_id"],
        "revision_id": dry.get("revision_id"),
        "ruleset": dry["ruleset"],
        "rule_sha256": dry["rule_sha256"],
        "definitions_sha256": dry["definitions_sha256"],
        "aliases_sha256": dry["aliases_sha256"],
        "created_at": created_at,
        "status": "published",
        "definitions": dry["definitions"],
        # 钉住当时的整张别名表：以后读旧裁决只认这一份
        "aliases": dry["aliases"],
        "base_aliases": dry["base_aliases"],
        "view": dry["after"],
        "base_view": dry["before"],
        "diff": dry["diff"],
        "pending": dry["aliases"].get("pending") or [],
        "warnings": dry["warnings"],
    }
    try:
        stored = store.insert_ruling(ruling)
    except Exception as exc:
        # 并发下另一个请求可能刚好写下同一发布键：UE 冲突按幂等处理
        concurrent = store.find_ruling_by_key(publish_key)
        if concurrent is not None:
            return dict(concurrent, reused=True)
        # 其余写入失败：事务已回滚，不落行、不占号，上一版裁决仍可用
        raise errors.PublishFailed(
            "发布失败，目录已恢复上一版",
            detail={"reason": "PUBLISH_FAILED", "dry_run_id": dry["dry_run_id"],
                    "error": str(exc)},
        )
    return dict(stored, reused=False)


def publish_from_body(body: dict) -> dict:
    return publish(str(body.get("dry_run_id") or ""))


# ---------- 读取 ----------

def get_ruling(version) -> dict:
    if version in (None, "", "latest"):
        ruling = store.latest_ruling()
        if ruling is None:
            raise errors.NotFound("还没有已发布的裁决版本")
        return ruling
    try:
        seq = int(version)
    except (TypeError, ValueError):
        raise errors.BadInput("版本号必须是整数", detail={"version": version})
    ruling = store.ruling_by_seq(seq)
    if ruling is None:
        raise errors.NotFound("没有这个裁决版本", detail={"version": seq})
    return ruling


def list_rulings() -> list[dict]:
    return store.list_rulings()


def catalog_rows(ruling: dict) -> list[dict]:
    """指标目录行：只按这一版裁决钉住的别名含义展开。"""
    view = ruling.get("view") or {}
    basis = {(item["name"], item["version"]): item
             for item in view.get("basis") or []}
    rows: list[dict] = []
    for metric in view.get("metrics") or []:
        group = basis.get((metric["canonical_name"], metric["version"])) or {}
        status = group.get("status") or "conflict"
        adopted = status == "chosen" and group.get("source") == metric["source"]
        if status == "conflict":
            reason = group.get("reason") or "DEFINITION_CONFLICT"
        elif adopted:
            reason = "ADOPTED"
        else:
            reason = "SAME_NAME_ADOPTED_OTHER"
        rows.append({
            "ruling_seq": ruling.get("ruling_seq"),
            "metric": metric["name"],
            "canonical_name": metric["canonical_name"],
            "version": metric["version"],
            "source": metric["source"],
            "signature": metric["signature"],
            "depends_on": "，".join(metric["depends_on"]),
            "group_status": status,
            "adopted": "Y" if adopted else "N",
            "reason": reason,
        })
    rows.sort(key=lambda row: (row["canonical_name"], row["version"],
                               row["metric"], row["source"]))
    return rows


def alias_rows(ruling: dict) -> list[dict]:
    pinned = ruling.get("aliases") or {}
    effective = pinned.get("map") or {}
    rows = []
    for entry in pinned.get("entries") or []:
        rows.append({"alias": entry["alias"], "target": entry["target"],
                     "canonical": effective.get(entry["alias"], entry["target"]),
                     "status": "effective", "detail": entry.get("note") or ""})
    for item in pinned.get("pending") or []:
        rows.append({"alias": item["alias"], "target": "",
                     "canonical": "", "status": "pending",
                     "detail": item.get("detail") or "待处理"})
    rows.sort(key=lambda row: (row["status"], row["alias"]))
    return rows


def revision_summary(ruling: dict) -> dict:
    view = ruling.get("view") or {}
    diff = ruling.get("diff") or {}
    return {
        "ruling_seq": ruling["ruling_seq"],
        "ruling_id": ruling["ruling_id"],
        "publish_key": ruling["publish_key"],
        "revision_id": ruling.get("revision_id"),
        "ruleset": ruling["ruleset"],
        "rule_sha256": ruling["rule_sha256"],
        "definitions_sha256": ruling["definitions_sha256"],
        "aliases_sha256": ruling["aliases_sha256"],
        "created_at": ruling["created_at"],
        "conflicts": len(view.get("conflicts") or []),
        "cycles": int(view.get("cycle_count") or 0),
        "self_loops": int(view.get("self_loop_count") or 0),
        "chosen": int(view.get("chosen_count") or 0),
        "metrics": len(view.get("metrics") or []),
        "pending": len(ruling.get("pending") or []),
        "affected": int(diff.get("affected_count") or 0),
    }
