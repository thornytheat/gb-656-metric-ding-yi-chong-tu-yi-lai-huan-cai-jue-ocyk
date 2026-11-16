"""M8 入口：别名修订演练、裁决版本页与按版本导出的指标目录。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app import errors, store, web
from app.modules.m8_rulings import csv_out, service

router = APIRouter()


def _csv(body: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/rulings", response_class=HTMLResponse)
def rulings_page(request: Request, version: str | None = None):
    rulings = [service.revision_summary(item) for item in service.list_rulings()]
    dry_runs = service.list_dry_runs()
    selected = None
    page_error = None
    if service.list_rulings():
        try:
            selected = service.get_ruling(version)
        except errors.FieldmapError as exc:
            page_error = exc.message
    context = {
        "rulings": rulings,
        "dry_runs": dry_runs,
        "selected": selected,
        "selected_version": version or "latest",
        "catalog_rows": service.catalog_rows(selected) if selected else [],
        "alias_rows": service.alias_rows(selected) if selected else [],
        "page_error": page_error,
    }
    return web.page_or_404(request, "rulings.html", "rulings", selected, **context)


@router.get("/api/rulings")
def rulings_api():
    return {
        "current_version_seq": store.current_ruling_seq(),
        "rulings": service.list_rulings(),
        "dry_runs": [{"dry_run_id": item["dry_run_id"],
                      "rule_sha256": item["rule_sha256"],
                      "definitions_sha256": item["definitions_sha256"],
                      "revision_id": item.get("revision_id"),
                      "created_at": item["created_at"],
                      "affected": item["diff"]["affected_count"],
                      "pending": len(item.get("pending") or [])}
                     for item in service.list_dry_runs()],
    }


@router.get("/api/rulings/{version}")
def ruling_detail(version: str):
    ruling = service.get_ruling(version)
    return {
        "ruling": {
            key: ruling.get(key) for key in
            ("ruling_seq", "ruling_id", "publish_key", "revision_id", "ruleset",
             "rule_sha256", "definitions_sha256", "aliases_sha256", "created_at")
        },
        "aliases": ruling.get("aliases") or {},
        "pending": ruling.get("pending") or [],
        "view": ruling.get("view") or {},
        "diff": ruling.get("diff") or {},
        "catalog": service.catalog_rows(ruling),
        "warnings": ruling.get("warnings") or [],
    }


@router.post("/api/rulings/dry-run")
def create_dry_run(payload: dict | None = None):
    body = payload or {}
    dry = service.dry_run_from_body(body)
    return {
        "dry_run_id": dry["dry_run_id"],
        "revision_id": dry.get("revision_id"),
        "rule_sha256": dry["rule_sha256"],
        "definitions_sha256": dry["definitions_sha256"],
        "aliases_sha256": dry["aliases_sha256"],
        "definitions_source": dry.get("definitions_source"),
        "pending": dry["pending"],
        "affected": dry["diff"]["affected"],
        "affected_count": dry["diff"]["affected_count"],
        "conflicts": dry["diff"]["conflicts"],
        "cycles": dry["diff"]["cycles"],
        "adoption_basis": dry["after"]["basis"],
        "before": {"conflicts": dry["before"]["conflicts"],
                   "cycles": dry["before"]["cycles"],
                   "chosen": dry["before"]["chosen"]},
        "after": {"conflicts": dry["after"]["conflicts"],
                  "cycles": dry["after"]["cycles"],
                  "chosen": dry["after"]["chosen"]},
        "warnings": dry["warnings"],
    }


@router.post("/api/rulings/publish")
def publish_ruling(payload: dict | None = None):
    body = payload or {}
    ruling = service.publish_from_body(body)
    return {
        "ruling_seq": ruling["ruling_seq"],
        "ruling_id": ruling["ruling_id"],
        "reused": bool(ruling.get("reused")),
        "revision_id": ruling.get("revision_id"),
        "rule_sha256": ruling["rule_sha256"],
        "conflicts": len((ruling.get("view") or {}).get("conflicts") or []),
        "cycles": int((ruling.get("view") or {}).get("cycle_count") or 0),
        "pending": len(ruling.get("pending") or []),
        "catalog_url": f"/api/rulings/{ruling['ruling_seq']}/catalog.csv",
    }


@router.get("/api/rulings/{version}/catalog.csv")
def ruling_catalog_csv(version: str):
    ruling = service.get_ruling(version)
    return _csv(csv_out.catalog_csv(service.catalog_rows(ruling)),
                f"metricgraph-ruling-{ruling['ruling_seq']}-catalog.csv")


@router.get("/api/rulings/{version}/aliases.csv")
def ruling_aliases_csv(version: str):
    ruling = service.get_ruling(version)
    return _csv(csv_out.aliases_csv(service.alias_rows(ruling)),
                f"metricgraph-ruling-{ruling['ruling_seq']}-aliases.csv")
