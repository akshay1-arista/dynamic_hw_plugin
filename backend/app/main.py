from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .config import OUTPUTS_ROOT
from .generator import GenerationError, generate_topology
from .inventory import load_inventory, save_inventory
from .models import GenerateRequest, GenerateResult, InventoryFile
from .reference import list_references


app = FastAPI(title="Dynamic Hardware Topology Generator", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
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
def put_hardware(inventory: InventoryFile):
    return save_inventory(inventory)


@app.post("/api/generate", response_model=GenerateResult)
def post_generate(request: GenerateRequest):
    try:
        return generate_topology(request)
    except (GenerationError, ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/runs/{run_id}/download")
def download_run(run_id: str):
    run_root = (OUTPUTS_ROOT / run_id).resolve()
    outputs_root = OUTPUTS_ROOT.resolve()
    if outputs_root not in run_root.parents and run_root != outputs_root:
        raise HTTPException(status_code=400, detail="Invalid run id")
    zip_files = list(Path(run_root).glob("*.zip"))
    if not zip_files:
        raise HTTPException(status_code=404, detail="Run zip not found")
    return FileResponse(zip_files[0], filename=zip_files[0].name, media_type="application/zip")

