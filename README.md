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
- install `sshpass` for switch auto-config support
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

Switch auto-config uses `sshpass` to open non-interactive SSH sessions to access and upstream switches. If you deploy with `AUTO_INSTALL_SYSTEM_DEPS=0`, install the OS package manually before using the "Configure switches" flow.

## Discovery and Refresh

Only one Lab Navigator operation runs at a time across users, including searches,
refresh/import requests, and discovery during generation. Overlapping requests return
HTTP 409 with `code: discovery_busy` and the active request ID; requests are not queued.
Inventory reads and progress remain available. The backend must run with one worker,
as configured in the systemd template; admission, progress, and previews are held in memory.

The UI displays the request ID, current stage, counts, and progress logs. It polls every
two seconds during discovery and every five seconds while idle. API clients can supply a
UUID in `X-Request-ID` and query:

- `GET /api/discovery/active`
- `GET /api/discovery/requests/{request_id}?after_seq=0`

Progress uses increasing log sequence numbers; pass the last sequence to retrieve only
new events. Completed requests remain available for 15 minutes (up to 100 requests,
500 events each). The same request ID appears in backend systemd journal messages.

Each operation reads bulk device metadata once and fetches each shared wiremap once,
using up to four concurrent upstream requests. The operation deadline is 90 seconds;
timeouts return HTTP 504 with the request ID. A disconnected browser does not release
the discovery slot while work is still running.

Refresh preview returns `preview_id`; pass it with `hardware_ids` to refresh Apply to
save the reviewed result without another discovery. Previews expire after five minutes
(up to 32 stored). An expired/consumed preview returns 410; changed inventory returns 409.
Bulk refresh preserves unavailable devices and reports their IDs as partial results while
refreshing the remaining groups. A single-group request for a missing device reports an error.
Reservations and availability are taken from current local state when applying. Clients
omitting `preview_id` retain the discover-and-apply behavior. Restarting the backend clears
preview and progress records. Deploy the backend and rebuilt frontend assets together.

Refresh reconciles conflicting imported edge links only for the selected hardware.
It checks that the selected edge and switch agree on the new cable, and that the old
edge explicitly reports the old port disconnected or connected elsewhere. Failed,
missing, incomplete, or contradictory wiremaps keep the existing link protected.
Manual links on other hardware are also protected. Preview lists removed links first,
includes `remove-connection` changes and `reconciled_connection_count`, and logs each
confirmed move under the request ID. Conflicts elsewhere in a shared switch graph are
preserved without marking the selected hardware's refresh as partial.

## Data And Outputs

- Hardware inventory: `backend/data/hardware_inventory.json`
- Local hardware availability and reservation overlay: `backend/data/hardware_inventory.local.json`
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
- Gerrit private branch commits always use the service git identity. Configure
  `HAPY_GIT_USER_NAME` and `HAPY_GIT_USER_EMAIL` for a fixed committer, or set `user.name` and
  `user.email` in the source Hapy repo as a fallback. `requested_by` is still recorded in run
  metadata and audit events, but it no longer affects the git commit identity.

Example `.env`:

```bash
REFERENCE_CONFIG_ROOT=backend/reference_topologies
HAPY_REPO_ROOT=/Users/akshay1.jain/Documents/automation/hapy_repo_for_tools/velocloud.src
# Optional override if testbed configs live somewhere else inside the repo:
# HAPY_TESTBED_CONFIG_ROOT=/absolute/path/to/velocloud.src/hapy/hapy/testbed/configs
# Optional override for the persisted tool branch list:
# HAPY_PRIVATE_BRANCH_REGISTRY_PATH=backend/data/hapy_private_branches.json
# Recommended fixed service commit identity for Gerrit private branch commits:
# HAPY_GIT_USER_NAME=Dynamic HW Topology
# HAPY_GIT_USER_EMAIL=dynamic-hw-topology@example.com
# Optional override for local-only hardware availability and reservations:
# INVENTORY_STATE_PATH=backend/data/hardware_inventory.local.json
```

## Verification

```bash
cd backend && python3 -m pytest
cd frontend && npm test && npm run build
```
