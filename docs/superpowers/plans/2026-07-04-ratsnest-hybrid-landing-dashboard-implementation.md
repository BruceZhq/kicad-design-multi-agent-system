# RatsNest Hybrid Landing Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a React + Vite + TypeScript + Tailwind single-page hybrid landing page and live dashboard for RatsNest at `/`.

**Architecture:** Add a focused `frontend/` project that owns the React application, typed API helpers, UI components, and tests. Vite builds directly into `backend/src/main/resources/static` so Spring Boot serves the compiled app without backend API changes. Keep the API surface unchanged and preserve the current dashboard behaviors: create design runs, create repair runs, list runs, inspect run details, and show ATDP events.

**Tech Stack:** Vite, React 18, TypeScript, Tailwind CSS 3, framer-motion, lucide-react, Vitest, React Testing Library, Spring Boot static resources.

---

## File Structure

- Create `frontend/package.json`: npm scripts and dependencies.
- Create `frontend/index.html`: app mount point plus Google Fonts.
- Create `frontend/vite.config.ts`: React plugin, test config, dev proxy, and static build output to Spring Boot.
- Create `frontend/tailwind.config.js`: Tailwind content paths, primary color, serif font.
- Create `frontend/postcss.config.js`: Tailwind and Autoprefixer plugins.
- Create `frontend/tsconfig.json`, `frontend/tsconfig.node.json`: TypeScript config.
- Create `frontend/src/main.tsx`: React entry.
- Create `frontend/src/App.tsx`: page composition and live dashboard state.
- Create `frontend/src/index.css`: Tailwind layers, global fonts, noise utilities, base styles.
- Create `frontend/src/lib/api.ts`: typed fetch helpers for the existing REST API.
- Create `frontend/src/lib/runData.ts`: safe formatting and parsing helpers for `DesignRun`, run records, and events.
- Create `frontend/src/lib/runData.test.ts`: RED/GREEN tests for helper behavior.
- Create `frontend/src/test/setup.ts`: test-dom setup.
- Replace generated contents in `backend/src/main/resources/static` through `npm run build`.
- Modify `README.md`: document the new frontend workflow and static build step.

## Task 1: Scaffold the Vite Frontend

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/index.html`
- Create: `frontend/vite.config.ts`
- Create: `frontend/tailwind.config.js`
- Create: `frontend/postcss.config.js`
- Create: `frontend/tsconfig.json`
- Create: `frontend/tsconfig.node.json`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/App.tsx`
- Create: `frontend/src/index.css`

- [ ] **Step 1: Create minimal scaffold files**

Create `frontend/package.json` with scripts:

```json
{
  "name": "ratsnest-frontend",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite --host 0.0.0.0",
    "build": "tsc -b && vite build",
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "dependencies": {
    "@vitejs/plugin-react": "^4.3.4",
    "framer-motion": "^11.18.2",
    "lucide-react": "^0.468.0",
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@testing-library/jest-dom": "^6.6.3",
    "@testing-library/react": "^16.1.0",
    "@types/react": "^18.3.18",
    "@types/react-dom": "^18.3.5",
    "autoprefixer": "^10.4.20",
    "jsdom": "^25.0.1",
    "postcss": "^8.4.49",
    "tailwindcss": "^3.4.17",
    "typescript": "^5.7.3",
    "vite": "^6.0.7",
    "vitest": "^2.1.8"
  }
}
```

- [ ] **Step 2: Add Vite, Tailwind, TypeScript, and empty app files**

Use `index.html` for Google Fonts, `vite.config.ts` with `outDir: '../backend/src/main/resources/static'`, and an initial `App.tsx` that renders:

```tsx
export default function App() {
  return (
    <main className="min-h-screen bg-black text-[#E1E0CC]">
      <h1>RatsNest</h1>
    </main>
  );
}
```

- [ ] **Step 3: Install dependencies**

Run: `npm install`

Expected: `package-lock.json` is created and dependencies install without errors.

- [ ] **Step 4: Build the empty scaffold**

Run: `npm run build`

Expected: TypeScript and Vite complete successfully and create static assets under `backend/src/main/resources/static`.

- [ ] **Step 5: Commit scaffold**

Run:

```powershell
git add frontend backend/src/main/resources/static
git commit -m "feat: scaffold ratsnest react frontend"
```

## Task 2: Add Typed Data Helpers With Tests

**Files:**
- Create: `frontend/src/lib/runData.ts`
- Create: `frontend/src/lib/runData.test.ts`
- Create: `frontend/src/test/setup.ts`
- Modify: `frontend/vite.config.ts`

- [ ] **Step 1: Write failing helper tests**

Create tests for:

- `parseRunRecord` returns `null` for blank or invalid JSON.
- `parseRunRecord` returns parsed iterations for valid `resultJson`.
- `formatScoreDelta` prefixes positive deltas with `+`.
- `summarizeEvent` produces compact MCP tool and regular node descriptions.

Run: `npm test`

Expected: FAIL because helpers do not exist yet.

- [ ] **Step 2: Implement minimal helper module**

Create typed helpers:

```ts
export interface DesignRun {
  id: string;
  status: string;
  kind?: string | null;
  resultJson?: string | null;
}

export function parseRunRecord(resultJson?: string | null): RunRecord | null {
  if (!resultJson) return null;
  try {
    return JSON.parse(resultJson) as RunRecord;
  } catch {
    return null;
  }
}

export function formatScoreDelta(delta?: number | null): string {
  if (delta === null || delta === undefined) return "-";
  return delta > 0 ? `+${delta}` : String(delta);
}

export function summarizeEvent(event: AtdpEvent): string {
  const payload = parseEventPayload(event.payload);
  if (event.node === "mcp_tool") {
    const tool = payload?.action?.tool ?? "mcp_tool";
    const args = JSON.stringify(payload?.action?.arguments ?? {});
    return `${tool} ${args}`.slice(0, 140);
  }
  return JSON.stringify(payload?.outcome ?? payload ?? {}).slice(0, 140);
}
```

- [ ] **Step 3: Run tests**

Run: `npm test`

Expected: PASS.

- [ ] **Step 4: Commit helpers**

Run:

```powershell
git add frontend/src/lib frontend/src/test frontend/vite.config.ts
git commit -m "feat: add typed run data helpers"
```

## Task 3: Add REST API Client

**Files:**
- Create: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/runData.ts`

- [ ] **Step 1: Add API client**

Create functions:

```ts
export async function getHealth(): Promise<HealthResponse>;
export async function listRuns(): Promise<DesignRun[]>;
export async function getRun(id: string): Promise<DesignRun>;
export async function getRunEvents(id: string): Promise<AtdpEvent[]>;
export async function createDesignRun(requirement: string): Promise<CreateRunResponse>;
export async function createRepairRun(projectDir: string): Promise<CreateRunResponse>;
```

Each function uses `fetch`, throws an `Error` with response text on non-OK responses, and returns JSON on success.

- [ ] **Step 2: Type-check**

Run: `npm run build`

Expected: TypeScript passes.

- [ ] **Step 3: Commit API client**

Run:

```powershell
git add frontend/src/lib/api.ts frontend/src/lib/runData.ts
git commit -m "feat: add frontend api client"
```

## Task 4: Build the Cinematic Landing Page

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/index.css`

- [ ] **Step 1: Add animation components**

Add `WordsPullUp`, `WordsPullUpMultiStyle`, and `AnimatedLetter` components inside `App.tsx`.

- [ ] **Step 2: Add Hero section**

Implement full-height inset hero with:

- Pull-up `RatsNest*` heading.
- Pill nav.
- Abstract animated background.
- Noise and gradient overlays.
- CTA that scrolls to `#console`.
- Health status indicator from `/api/health`.

- [ ] **Step 3: Add System section**

Implement centered dark card with multi-style heading and scroll-linked character reveal.

- [ ] **Step 4: Add Features section**

Implement four staggered cards with lucide icons and checklist rows.

- [ ] **Step 5: Build**

Run: `npm run build`

Expected: PASS.

- [ ] **Step 6: Commit landing sections**

Run:

```powershell
git add frontend/src/App.tsx frontend/src/index.css backend/src/main/resources/static
git commit -m "feat: add cinematic ratsnest landing sections"
```

## Task 5: Build the Live Console Dashboard

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/index.css`

- [ ] **Step 1: Add console state**

In `App.tsx`, keep state for:

```ts
const [runs, setRuns] = useState<DesignRun[]>([]);
const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
const [selectedRun, setSelectedRun] = useState<DesignRun | null>(null);
const [events, setEvents] = useState<AtdpEvent[]>([]);
const [error, setError] = useState<string | null>(null);
const [isSubmittingDesign, setIsSubmittingDesign] = useState(false);
const [isSubmittingRepair, setIsSubmittingRepair] = useState(false);
```

- [ ] **Step 2: Add polling**

Load runs immediately and every 4 seconds. Preserve selected run and refresh detail/events when selected.

- [ ] **Step 3: Add forms**

Wire `POST /api/designs` and `POST /api/runs`, disable buttons during submit, ignore empty submissions, and select the returned run id.

- [ ] **Step 4: Add runs and detail views**

Render run table/cards, scorecard, strategy metadata, iteration table, repair rationale, and ATDP timeline.

- [ ] **Step 5: Build**

Run: `npm run build`

Expected: PASS.

- [ ] **Step 6: Commit console**

Run:

```powershell
git add frontend/src/App.tsx frontend/src/index.css backend/src/main/resources/static
git commit -m "feat: add live ratsnest control console"
```

## Task 6: Update Documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document frontend workflow**

Add commands:

```powershell
cd RatsNest/frontend
npm install
npm run dev
npm run build
```

Document that `npm run build` emits static files into `backend/src/main/resources/static`.

- [ ] **Step 2: Commit docs**

Run:

```powershell
git add README.md
git commit -m "docs: document frontend dashboard workflow"
```

## Task 7: Verify Integration

**Files:**
- No intended source changes.

- [ ] **Step 1: Run frontend tests**

Run: `npm test`

Expected: PASS.

- [ ] **Step 2: Run frontend build**

Run: `npm run build`

Expected: PASS and static files appear in `backend/src/main/resources/static`.

- [ ] **Step 3: Run backend tests with JDK 17**

Run:

```powershell
$env:JAVA_HOME='C:\Program Files\Eclipse Adoptium\jdk-17.0.2.8-hotspot'
$env:Path="$env:JAVA_HOME\bin;$env:Path"
mvn -q test
```

from `backend/`.

Expected: PASS.

- [ ] **Step 4: Run local visual smoke check**

Run:

```powershell
npm run dev -- --port 5173
```

Open `http://localhost:5173/` and confirm the page is nonblank, responsive, and dashboard controls render. Stop the dev server after the check.

- [ ] **Step 5: Commit any generated static drift**

Run:

```powershell
git status --short
```

If only intended static files changed after the final build, commit them with:

```powershell
git add backend/src/main/resources/static frontend
git commit -m "build: refresh frontend static assets"
```

If no changes remain, do not create an empty commit.
