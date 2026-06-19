# ITC System Scanner

Pure Node.js + React replacement for the old ITC System Scanner workflow. The system has three parts:

1. `backend/` is the Express + MongoDB control plane.
2. `frontend/` is the React web UI used by admins and users.
3. `agent/` is the Node.js client service installed on each LAN machine.

The React app never performs deep machine scans. The backend tells the selected client agent to scan, the agent gathers local OS inventory, generates JSON/PDF reports, uploads them to the backend, and the UI shows status, logs, report history, and downloads.

## Folder Structure

```text
backend/
  src/config/          Environment and MongoDB connection
  src/controllers/     API handlers
  src/middleware/      Auth, upload, rate-limit, error handling
  src/models/          Mongoose models
  src/routes/          REST route modules
  src/services/        Agent calls, auth, report persistence
  src/socket/          Socket.IO status events
  src/utils/           File, logging, sanitization helpers
  src/server.js

frontend/
  src/components/      Layout, status badges, JSON viewer
  src/context/         Authentication context
  src/hooks/           Socket hook
  src/pages/           Dashboard, clients, scan, reports, settings
  src/services/        Axios and Socket.IO clients
  src/styles/

agent/
  src/scanner/         OS inventory collectors
  src/services/        Scan runner, PDF generation, upload, registration
  src/routes/          Local agent HTTP API
  src/utils/
  src/agent.js

docs/
  API.md
  DEPLOYMENT.md
  ENVIRONMENT.md
  INSTALLATION.md
  LINUX_SYSTEMD.md
  TESTING_CHECKLIST.md
  WINDOWS_SERVICE.md
```

## Quick Start

```powershell
cd backend
copy .env.example .env
npm install
npm run dev
```

```powershell
cd frontend
copy .env.example .env
npm install
npm run dev
```

```powershell
cd agent
copy .env.example .env
npm install
npm start
```

Open the React UI at `http://localhost:5173`. The backend listens on `http://localhost:5051` by default, and the agent listens on `http://localhost:7979`.

## Default Flow

1. Start MongoDB.
2. Configure `backend/.env` with `MONGO_URI`, `JWT_SECRET`, `AGENT_SHARED_TOKEN`, and initial admin credentials.
3. Start the backend.
4. Configure `frontend/.env` with the backend URL.
5. Start the frontend and sign in.
6. Configure each `agent/.env` with the backend URL and same shared token.
7. Start the agent on each client PC.
8. Open the UI, select a registered client, and click `Scan This PC`.

See the `docs/` folder for installation, service setup, API, deployment, and testing details.
