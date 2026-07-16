from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .audit import AuditTrailError, append_audit_event, list_audit_events
from .discovery import DiscoveryError, apply_inventory_refresh, preview_inventory_refresh
from .config import OUTPUTS_ROOT
from .generator import GenerationError, generate_topology, resolve_run_root
from .hapy_repo import HapyRepoError, delete_private_branches, list_private_branches, publish_run_private_branch
from .inventory import load_inventory, save_inventory, update_hardware_availability
from .models import (
    AuditTrailResult,
    GenerateRequest,
    GenerateResult,
    HapyPrivateBranchDeleteRequest,
    HapyPrivateBranchDeleteResult,
    HapyCommitRequest,
    HapyCommitResult,
    HapyPrivateBranchListResult,
    HardwareAvailabilityUpdateRequest,
    InventoryFile,
    InventoryRefreshRequest,
    InventoryRefreshResult,
    InventoryUpdateRequest,
    SavedRunListResult,
    SavedRunLoadResult,
    SwitchConfigureRequest,
    SwitchConfigureResult,
)
from .reference import list_references
from .run_history import RunHistoryError, list_saved_runs, load_saved_run
from .switch_config import SwitchConfigError, configure_switches_for_run


def _cors_allowed_origins() -> list[str]:
    configured = os.environ.get("CORS_ALLOWED_ORIGINS", "")
    if configured.strip():
        return [origin.strip() for origin in configured.split(",") if origin.strip()]
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5400",
        "http://127.0.0.1:5400",
    ]


app = FastAPI(title="Dynamic Hardware Topology Generator", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/reference-topologies")
def get_reference_topologies():
    return list_references()


@app.get("/api/hardware", response_model=InventoryFile)
def get_hardware():
    return load_inventory()


@app.put("/api/hardware", response_model=InventoryFile)
def put_hardware(request: InventoryUpdateRequest):
    saved = save_inventory(request.inventory)
    if request.requested_by is not None:
        append_audit_event(
            action="inventory_saved",
            actor=request.requested_by,
            target_type="inventory",
            target_id="hardware_inventory",
            summary="Saved inventory updates.",
            details={"hardware_count": len(saved.hardware)},
        )
    return saved


@app.post("/api/hardware/{hardware_id}/availability", response_model=InventoryFile)
def post_hardware_availability(hardware_id: str, request: HardwareAvailabilityUpdateRequest):
    try:
        inventory, events = update_hardware_availability(
            hardware_id,
            request.available,
            request.requested_by,
        )
        for event in events:
            append_audit_event(
                action=event.action,
                actor=event.actor,
                target_type=event.target_type,
                target_id=event.target_id,
                summary=event.summary,
                details=event.details,
            )
        return inventory
    except (ValueError, AuditTrailError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/hardware/refresh-preview", response_model=InventoryRefreshResult)
def post_hardware_refresh_preview(request: InventoryRefreshRequest):
    try:
        return preview_inventory_refresh(request)
    except (DiscoveryError, GenerationError, ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/hardware/refresh-apply", response_model=InventoryRefreshResult)
def post_hardware_refresh_apply(request: InventoryRefreshRequest):
    try:
        return apply_inventory_refresh(request)
    except (DiscoveryError, GenerationError, ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/generate", response_model=GenerateResult)
def post_generate(request: GenerateRequest):
    try:
        return generate_topology(request)
    except (GenerationError, ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/runs", response_model=SavedRunListResult)
def get_saved_runs():
    try:
        return list_saved_runs()
    except (RunHistoryError, GenerationError, ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/runs/{run_id}", response_model=SavedRunLoadResult)
def get_saved_run(run_id: str):
    try:
        return load_saved_run(run_id)
    except (RunHistoryError, GenerationError, ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/hapy/private-branches", response_model=HapyPrivateBranchListResult)
def get_hapy_private_branches():
    try:
        return list_private_branches()
    except (HapyRepoError, GenerationError, ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/hapy/private-branches/delete", response_model=HapyPrivateBranchDeleteResult)
def post_delete_hapy_private_branches(request: HapyPrivateBranchDeleteRequest):
    try:
        return delete_private_branches(request)
    except (HapyRepoError, GenerationError, ValueError, FileNotFoundError, AuditTrailError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/runs/{run_id}/publish-private-branch", response_model=HapyCommitResult)
def post_publish_private_branch(run_id: str, request: HapyCommitRequest):
    try:
        return publish_run_private_branch(run_id, request)
    except (HapyRepoError, GenerationError, ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/runs/{run_id}/configure-switches", response_model=SwitchConfigureResult)
def post_configure_switches(run_id: str, request: SwitchConfigureRequest):
    try:
        return configure_switches_for_run(run_id, request)
    except (SwitchConfigError, GenerationError, ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/runs/{run_id}/download")
def download_run(run_id: str):
    try:
        run_root = resolve_run_root(run_id, OUTPUTS_ROOT)
    except GenerationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    zip_files = list(Path(run_root).glob("*.zip"))
    if not zip_files:
        raise HTTPException(status_code=404, detail="Run zip not found")
    return FileResponse(zip_files[0], filename=zip_files[0].name, media_type="application/zip")


@app.get("/api/audit-trail", response_model=AuditTrailResult)
def get_audit_trail():
    try:
        return list_audit_events()
    except AuditTrailError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
