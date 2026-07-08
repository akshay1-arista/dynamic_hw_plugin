# Dynamic Hardware Topology Generator

Phase1 tool for generating Hapy hardware topology folders from allowlisted virtual reference topologies.

## Run Locally

Backend:

```bash
cd backend
PYTHONPATH=. uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173`.

## Data And Outputs

- Hardware inventory: `backend/data/hardware_inventory.json`
- Generated topology folders and zip files: `outputs/<run_id>/`
- Reference topologies are read from `REFERENCE_CONFIG_ROOT` in `.env`.

Example `.env`:

```bash
REFERENCE_CONFIG_ROOT=/Users/akshay1.jain/Documents/automation/arista/velocloud.src/hapy/hapy/testbed/configs
```

## Verification

```bash
cd backend && python3 -m pytest
cd frontend && npm test && npm run build
```
