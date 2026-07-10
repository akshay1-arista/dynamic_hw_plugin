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

## Deploy For Production

Run the production deployment script from the repo root:

```bash
sudo ./deploy_production.sh
```

It will:

- create or refresh `.venv-production`
- install backend dependencies from `backend/requirements.txt`
- run `npm ci` and `npm run build`
- install `systemd` services for the frontend and backend
- enable and restart the frontend on `5400`
- enable and restart the backend on `5401`

By default it builds the UI against `http://127.0.0.1:5401`. Override `PUBLIC_HOST` when the app is accessed through a server IP or DNS name:

```bash
sudo PUBLIC_HOST=your-server-name ./deploy_production.sh
```

The services restart automatically on crash and on reboot. After deployment:

```bash
systemctl status dynamic-hw-topology-backend.service dynamic-hw-topology-frontend.service
journalctl -u dynamic-hw-topology-backend.service -u dynamic-hw-topology-frontend.service -f
```

Optional persistent overrides and secrets can be supplied with `/etc/default/dynamic-hw-topology`. A sample file is available at `deploy/systemd/dynamic-hw-topology.env.example`.

## Data And Outputs

- Hardware inventory: `backend/data/hardware_inventory.json`
- Vendored reference topologies: `backend/reference_topologies/`
- Generated topology folders and zip files: `outputs/<run_id>-<suffix>/`
- Reference topologies are read from `backend/reference_topologies/` by default.
- `REFERENCE_CONFIG_ROOT` remains available if you need to override the source path.

Example `.env`:

```bash
REFERENCE_CONFIG_ROOT=backend/reference_topologies
```

## Verification

```bash
cd backend && python3 -m pytest
cd frontend && npm test && npm run build
```
