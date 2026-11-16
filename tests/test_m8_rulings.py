"""M8 裁决版本：别名修订演练、同名冲突与依赖环、发布、版本钉住与目录导出。"""

from __future__ import annotations

import csv
import io

import pytest

from app import errors, store
from app.modules.m8_rulings import alias, metrics, service


DEFS = [
    {"name": "gmv", "version": "v1", "expression": "pay + refund",
     "priority": 10, "source": "finance", "depends_on": ["pay_amt", "refund_amt"]},
    {"name": "net_total", "version": "v1", "expression": "pay - refund",
     "priority": 5, "source": "sales", "depends_on": ["pay_amt", "refund_amt"]},
    {"name": "net_pay", "version": "v1", "expression": "pay - refund",
     "priority": 15, "source": "finance", "depends_on": ["pay_amt", "refund_amt"]},
]

WRONG = [{"alias": "pay", "target": "pay_amt"}, {"alias": "refund", "target": "refund_amt"},
         {"alias": "net_total", "target": "gmv"}]
FIXED = [{"alias": "pay", "target": "pay_amt"}, {"alias": "refund", "target": "refund_amt"},
         {"alias": "net_total", "target": "net_pay"}]


def _dry(client, entries=FIXED, base=WRONG, definitions=None, revision="rev-1"):
    response = client.post("/api/rulings/dry-run", json={
        "definitions": definitions or DEFS,
        "aliases": entries,
        "base_aliases": base,
        "revision_id": revision,
    })
    assert response.status_code == 200, response.text
    return response.json()


def _publish(client, dry_run_id):
    response = client.post("/api/rulings/publish", json={"dry_run_id": dry_run_id})
    assert response.status_code == 200, response.text
    return response.json()


# ---------- 别名表 ----------

def test_ambiguous_alias_is_pending_not_merged():
    normalized = alias.normalize_entries([
        {"alias": "paid_amount", "target": "pay_amt"},
        {"alias": "paid_amount", "target": "net_pay"},
    ])
    assert normalized["entries"] == []
    assert normalized["pending"][0]["reason"] == "ALIAS_AMBIGUOUS"
    assert normalized["pending"][0]["targets"] == ["net_pay", "pay_amt"]
    assert normalized["ambiguous"] is True


def test_alias_chain_resolves_and_cycle_pends():
    chained = alias.normalize_entries([
        {"alias": "a", "target": "b"}, {"alias": "b", "target": "gmv"},
    ])
    assert chained["map"] == {"a": "gmv", "b": "gmv"}
    assert chained["pending"] == []

    cyclic = alias.normalize_entries([
        {"alias": "x", "target": "y"}, {"alias": "y", "target": "x"},
    ])
    assert cyclic["entries"] == []
    assert {item["reason"] for item in cyclic["pending"]} == {"ALIAS_CYCLE"}


def test_alias_entry_needs_both_sides():
    with pytest.raises(errors.BadInput):
        alias.normalize_entries([{"alias": "a"}])
    with pytest.raises(errors.BadInput):
        alias.normalize_entries([{"alias": "a", "target": "a"}])


def test_alias_account_is_conserved():
    """提交别名 = 生效映射 + 待处理；不能既进有效表又挂起。"""
    raw = [
        {"alias": "a", "target": "pay_amt"},
        {"alias": "b", "target": "pay_amt"},
        {"alias": "c", "target": "refund_amt"},
        {"alias": "c", "target": "pay_amt"},     # 歧义 → 待处理
        {"alias": "x", "target": "y"},           # 环 → 待处理
        {"alias": "y", "target": "x"},
    ]
    normalized = alias.normalize_entries(raw)
    submitted = len({str(item["alias"]).lower() for item in raw})
    effective = {entry["alias"] for entry in normalized["entries"]}
    pending = {item["alias"] for item in normalized["pending"]}
    assert effective.isdisjoint(pending)
    assert len(effective) + len(pending) == submitted


# ---------- 演练 ----------

def test_dry_run_lists_conflicts_cycles_and_affected(client):
    body = _dry(client)
    assert body["affected_count"] >= 1
    names = {item["name"] for item in body["affected"]}
    assert "net_total" in names
    # 误映射把 net_total 折成 gmv，修订后两侧签名一致，冲突消除
    assert any(item["name"] == "gmv" for item in body["conflicts"]["before"])
    assert body["conflicts"]["after"] == []
    assert body["conflicts"]["resolved"]
    assert body["adoption_basis"]


def test_dry_run_detects_emergent_cycle(client):
    definitions = [
        {"name": "report", "version": "v1", "expression": "x", "source": "bi",
         "depends_on": ["collector_old"]},
        {"name": "collector", "version": "v1", "expression": "y", "source": "bi",
         "depends_on": ["report_old"]},
    ]
    body = _dry(client, entries=[{"alias": "report_old", "target": "report"},
                                 {"alias": "collector_old", "target": "collector"}],
                base=[], definitions=definitions)
    assert body["cycles"]["before"]["cycles"] == []
    assert body["cycles"]["after"]["cycles"]
    assert body["cycles"]["emerged"][0]["nodes"] == ["collector", "report"]
    assert any(w["code"] == "CYCLE_EMERGED" for w in body["warnings"])


def test_dry_run_is_idempotent(client):
    first = _dry(client)
    second = _dry(client)
    assert first["dry_run_id"] == second["dry_run_id"]
    assert store.count_dry_runs() == 1


def test_dry_run_corrects_signature_before_name_matching(client):
    """别名纠正后同名分组与签名都要按新含义重算。"""
    wrong = alias.normalize_entries(WRONG)
    fixed = alias.normalize_entries(FIXED)
    before = metrics.evaluate(metrics.normalize(DEFS), wrong["map"])
    after = metrics.evaluate(metrics.normalize(DEFS), fixed["map"])
    assert {item["name"] for item in before["conflicts"]} == {"gmv"}
    assert after["conflicts"] == []
    canonical = {row["name"]: row["canonical_name"] for row in after["metrics"]}
    assert canonical["net_total"] == "net_pay"


# ---------- 发布 ----------

def test_publish_creates_version_and_is_idempotent(client):
    dry = _dry(client)
    first = _publish(client, dry["dry_run_id"])
    assert first["reused"] is False
    assert first["ruling_seq"] == 1
    again = _publish(client, dry["dry_run_id"])
    assert again["reused"] is True
    assert again["ruling_seq"] == 1
    assert store.count_rulings() == 1


def test_old_ruling_keeps_its_alias_snapshot(client):
    """确认过的裁决还原当时的别名，后续纠正不改写旧结论。"""
    first = _publish(client, _dry(client, entries=WRONG, base=WRONG,
                                 revision="rev-wrong")["dry_run_id"])
    fixed_dry = _dry(client, entries=FIXED, base=WRONG, revision="rev-fixed",
                     definitions=[dict(item, version="v2") for item in DEFS])
    second = _publish(client, fixed_dry["dry_run_id"])
    assert second["ruling_seq"] == 2

    old = client.get("/api/rulings/1").json()
    assert old["ruling"]["ruling_seq"] == first["ruling_seq"]
    pinned = {item["alias"]: item["target"] for item in old["aliases"]["entries"]}
    assert pinned["net_total"] == "gmv"          # 旧版仍然是那时的误映射
    new = client.get("/api/rulings/2").json()
    pinned_new = {item["alias"]: item["target"] for item in new["aliases"]["entries"]}
    assert pinned_new["net_total"] == "net_pay"  # 新版才纠正


def test_old_dry_run_rejected_after_alias_rule_update(client):
    stale = _dry(client, revision="rev-old")
    _dry(client, entries=FIXED + [{"alias": "pay2", "target": "pay_amt"}],
         revision="rev-new")
    response = client.post("/api/rulings/publish", json={"dry_run_id": stale["dry_run_id"]})
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "ALIAS_REVISION_STALE"
    assert store.count_rulings() == 0


def test_rule_pointer_moves_on_resubmission_even_when_deduped(client):
    """重交同一份演练会命中内容去重，但当前规则指针仍前移，旧演练随之失效。"""
    first = _dry(client, revision="rev-1")
    other = _dry(client, entries=WRONG, base=WRONG, revision="rev-2")
    assert other["dry_run_id"] != first["dry_run_id"]
    again = _dry(client, revision="rev-1")          # 回到第一张表，内容去重
    assert again["dry_run_id"] == first["dry_run_id"]
    assert store.count_dry_runs() == 2

    # 指针指向第一张表，第一版可发布；第二版演练已失效
    assert _publish(client, first["dry_run_id"])["reused"] is False
    blocked = client.post("/api/rulings/publish", json={"dry_run_id": other["dry_run_id"]})
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "ALIAS_REVISION_STALE"


def test_ambiguous_dry_run_records_pending_and_publishes_without_merging(client):
    body = _dry(client, entries=[
        {"alias": "paid_amount", "target": "pay_amt"},
        {"alias": "paid_amount", "target": "net_pay"},
    ], base=[])
    assert body["pending"][0]["reason"] == "ALIAS_AMBIGUOUS"
    published = _publish(client, body["dry_run_id"])
    detail = client.get(f"/api/rulings/{published['ruling_seq']}").json()
    # 歧义映射不进有效表，原样留作待处理
    assert detail["aliases"]["entries"] == []
    assert detail["pending"][0]["alias"] == "paid_amount"
    aliases = list(csv.DictReader(io.StringIO(
        client.get(f"/api/rulings/{published['ruling_seq']}/aliases.csv").text)))
    assert aliases[0]["status"] == "pending"


def test_insert_ruling_failure_leaves_no_row_and_no_seq_gap(client, monkeypatch):
    """事务失败不落行、不占号：下一版仍从 1 开始。"""
    first = _publish(client, _dry(client)["dry_run_id"])
    second_dry = _dry(client, revision="rev-2",
                      definitions=[dict(item, version="v2") for item in DEFS])
    real_dumps = store._dumps

    def flaky(value):
        if isinstance(value, dict) and value.get("ruling_id"):
            raise RuntimeError("序列化失败")
        return real_dumps(value)

    monkeypatch.setattr(store, "_dumps", flaky)
    response = client.post("/api/rulings/publish", json={"dry_run_id": second_dry["dry_run_id"]})
    assert response.status_code == 500
    monkeypatch.setattr(store, "_dumps", real_dumps)

    assert store.count_rulings() == 1
    retried = _publish(client, second_dry["dry_run_id"])
    assert retried["ruling_seq"] == first["ruling_seq"] + 1
    assert store.count_rulings() == 2


def test_publish_failure_rolls_back_and_keeps_previous(client, monkeypatch):
    first = _publish(client, _dry(client)["dry_run_id"])
    second_dry = _dry(client, revision="rev-2",
                      definitions=[dict(item, version="v2") for item in DEFS])
    real_insert = store.insert_ruling

    def boom(_ruling):
        raise RuntimeError("磁盘炸了")

    monkeypatch.setattr(store, "insert_ruling", boom)
    response = client.post("/api/rulings/publish", json={"dry_run_id": second_dry["dry_run_id"]})
    assert response.status_code == 500
    assert response.json()["error"] == "PUBLISH_FAILED"
    monkeypatch.setattr(store, "insert_ruling", real_insert)

    # 目录仍可读上一版
    latest = client.get("/api/rulings/latest")
    assert latest.status_code == 200
    assert latest.json()["ruling"]["ruling_seq"] == first["ruling_seq"]
    assert store.count_rulings() == 1

    # 重试成功，且只新增一版
    retried = _publish(client, second_dry["dry_run_id"])
    assert retried["ruling_seq"] == 2
    assert store.count_rulings() == 2


def test_publish_requires_known_dry_run(client):
    response = client.post("/api/rulings/publish", json={"dry_run_id": "dry-nope"})
    assert response.status_code == 404


# ---------- 目录导出 ----------

def test_catalog_csv_reads_selected_version(client):
    _publish(client, _dry(client, entries=FIXED, base=WRONG,
                          revision="rev-1")["dry_run_id"])
    # 第二版沿用修订前的误映射，验证目录按所选版本各读各的
    _publish(client, _dry(client, entries=WRONG, base=FIXED,
                          definitions=[dict(item, version="v2") for item in DEFS],
                          revision="rev-2")["dry_run_id"])

    first = list(csv.DictReader(io.StringIO(
        client.get("/api/rulings/1/catalog.csv").text)))
    second = list(csv.DictReader(io.StringIO(
        client.get("/api/rulings/2/catalog.csv").text)))
    assert {row["version"] for row in first} == {"v1"}
    assert {row["version"] for row in second} == {"v2"}
    first_row = [row for row in first if row["metric"] == "net_total"][0]
    assert first_row["canonical_name"] == "net_pay"
    second_row = [row for row in second if row["metric"] == "net_total"][0]
    assert second_row["canonical_name"] == "gmv"


def test_published_view_shows_conflicts_resolved(client):
    published = _publish(client, _dry(client)["dry_run_id"])
    detail = client.get(f"/api/rulings/{published['ruling_seq']}").json()
    assert detail["diff"]["conflicts"]["before"]
    assert detail["diff"]["conflicts"]["after"] == []
    assert detail["view"]["conflicts"] == []


def test_rulings_page_renders_and_compares(client):
    _publish(client, _dry(client)["dry_run_id"])
    page = client.get("/rulings")
    assert page.status_code == 200
    assert "裁决版本" in page.text
    assert "修订前" in page.text and "修订后" in page.text
    assert "指标目录" in page.text


def test_rulings_page_empty_state(empty_client):
    page = empty_client.get("/rulings")
    assert page.status_code == 200
    assert "还没有已发布的裁决版本" in page.text
