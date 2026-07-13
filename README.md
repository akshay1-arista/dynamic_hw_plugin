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

- auto-install missing host packages on supported distros (`apt-get` and `dnf`) unless `AUTO_INSTALL_SYSTEM_DEPS=0`
- create or refresh `.venv-production`
- install backend dependencies from `backend/requirements.txt`
- run `npm ci` and `npm run build` when `npm` is available
- install `systemd` services for the frontend and backend
- enable and restart the frontend on `5400`
- enable and restart the backend on `5401`

If the deployment host does not have `npm`, the script reuses committed or prebuilt assets from `frontend/dist`. That `frontend/dist` directory is intentionally versioned so production deploys can still work on hosts with an older Node runtime. In both modes the frontend is served by a small Python static server that proxies `/api` to the backend, so Node is not required at runtime.

If `npm` exists but the host `node` version is older than `20.19.0`, the script also falls back to `frontend/dist` when it is already present. If `frontend/dist` is missing, deployment stops with a clear upgrade requirement instead of failing later inside `vite build`.

Set `PUBLIC_HOST` when the app is accessed through a server IP or DNS name:

```bash
sudo PUBLIC_HOST=your-server-name ./deploy_production.sh
```

If you need to bake a different backend origin into the UI bundle instead of using the built-in `/api` proxy, set `API_BASE_URL` explicitly:

```bash
sudo API_BASE_URL=https://your-api.example.com PUBLIC_HOST=your-server-name ./deploy_production.sh
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
- Hapy repo publishing reads `HAPY_REPO_ROOT` and copies committed topologies under
  `hapy/hapy/testbed/configs/`.
- For nested references such as `5-site-cluster/spirent`, the committed topology is saved under the
  parent folder path, for example `hapy/hapy/testbed/configs/5-site-cluster/<generated-topology>`.
- Tool-created Gerrit private branches are tracked in `backend/data/hapy_private_branches.json` by default.
- Hapy commits and pushes run inside isolated per-run clone workspaces under `outputs/`, so different
  users and different base branches do not share a mutable checkout.

Example `.env`:

```bash
REFERENCE_CONFIG_ROOT=backend/reference_topologies
HAPY_REPO_ROOT=/Users/akshay1.jain/Documents/automation/hapy_repo_for_tools/velocloud.src
# Optional override if testbed configs live somewhere else inside the repo:
# HAPY_TESTBED_CONFIG_ROOT=/absolute/path/to/velocloud.src/hapy/hapy/testbed/configs
# Optional override for the persisted tool branch list:
# HAPY_PRIVATE_BRANCH_REGISTRY_PATH=backend/data/hapy_private_branches.json
```

## Verification

```bash
cd backend && python3 -m pytest
cd frontend && npm test && npm run build
```
