# RatsNest Hybrid Landing Dashboard Design

Date: 2026-07-04

## Goal

Replace the current zero-build static dashboard with a React + Vite + TypeScript + Tailwind front end that combines a cinematic product landing page with a live control plane dashboard.

The page should adapt the supplied Prisma visual direction to RatsNest: dark, cinematic, warm cream typography, textured surfaces, motion-rich section reveals, and a more technical "autonomous PCB design lab" tone. The page must remain useful as an operations surface, not only a marketing page.

## Selected Approach

Use a single-page hybrid experience at `/`.

The top of the page introduces RatsNest and the AHE multi-agent system with a strong cinematic hero. The same page then moves into system narrative, capability cards, and a live console connected to the existing Spring Boot REST API.

This avoids extra routing and keeps `http://localhost:8080/` useful immediately after the backend starts.

## Product Positioning

Working title: `RatsNest`

Positioning line:

`Auto-evolving multi-agent control plane for KiCad design review, repair, and strategy evolution.`

Tone:

- Systemic and experimental, not consumer SaaS.
- Cinematic and atmospheric, but grounded in real run telemetry.
- "Lab / control plane / trajectory / evolution" language instead of creative-studio language.

## Page Structure

### 1. Hero

Full viewport, inset rounded container, black global background, warm cream text.

Content:

- Top pill navigation: `System`, `Agents`, `Evolution`, `Console`, `Runs`.
- Giant pull-up heading: `RatsNest*`.
- Short description explaining the evaluate -> repair -> re-evaluate -> evolve loop.
- Primary CTA: `Launch a design run`, scrolling to the live console.
- Secondary status line from `/api/health`, displayed as a compact live indicator when available.

Visual adaptation:

- Use an abstract animated lab background instead of Prisma's creative-studio video if external media is unreliable.
- Keep noise overlay, gradient overlay, large type, rounded hero container, and warm cream palette.

### 2. System Section

About-style centered card.

Content should explain:

- The Spring backend is governance.
- The Python runtime is intelligence.
- ATDP captures the trajectory.
- AHE evolves strategy candidates against benchmark gates.

Heading style:

`This is not a PCB generator.` normal text
`It is a strategy evolution loop.` italic serif accent
`Every design run becomes training signal.` normal text

Body text uses scroll-linked character opacity reveal.

### 3. Features Section

Four responsive cards:

1. `Design Generation`
   - Natural language requirement to KiCad project.
   - Template and MCP backend paths.
   - Verified output record.

2. `Repair Loop`
   - Evaluate findings.
   - Generate patch plan.
   - Re-score and converge or escalate.

3. `ATDP Trajectory`
   - Orchestrator node events.
   - MCP tool calls.
   - Reward and outcome traces.

4. `Heuristic Evolution`
   - Candidate strategy experiments.
   - Promotion gates.
   - Rollback-safe incumbent strategy.

Cards use dark panels, lucide icons, staggered entrance, and checklist rows.

### 4. Live Console Section

This is the functional dashboard section.

Required controls:

- Requirement textarea for `POST /api/designs`.
- Project directory input for `POST /api/runs`.
- Run list from `GET /api/runs`.
- Selected run detail from `GET /api/runs/{id}`.
- ATDP timeline from `GET /api/runs/{id}/events`.

Required display:

- Run kind, status, created time.
- Initial and final score when present.
- Strategy version id.
- Python run id.
- Iteration table when `resultJson.iterations` is present.
- Repair rationale when patch-plan rationale exists.
- Timeline events with node, iteration, reward, and compact outcome/tool detail.

Polling:

- Refresh runs every 4 seconds, matching the existing dashboard behavior.
- Preserve selected run across refreshes.

Error handling:

- Failed API calls should show inline messages in the console section.
- Submit buttons should disable while requests are in flight.
- Empty form submissions should be ignored with local validation.

## Technical Architecture

Create a new front-end project under:

`RatsNest/frontend`

Stack:

- Vite
- React 18
- TypeScript
- Tailwind CSS 3
- framer-motion
- lucide-react

Build output:

- Vite builds to `RatsNest/backend/src/main/resources/static`.
- Spring Boot serves the built `index.html` at `/`.
- Existing `/api/...` endpoints remain unchanged.

Development:

- Vite dev server can proxy `/api` to `http://localhost:8080`.
- Production static assets are served by the Spring Boot jar.

Docker:

- The backend Dockerfile currently copies `RatsNest/backend/src`.
- The simplest first implementation builds the front end before rebuilding the backend image.
- A later Dockerfile improvement can add a Node build stage if fully containerized front-end builds are required.

## Styling Rules

Fonts:

- `Almarai` as global font.
- `Instrument Serif` italic for accent text.

Palette:

- Global background: `#000000`.
- Primary display text: `#E1E0CC`.
- Tailwind primary: `#DEDBC8`.
- Card surfaces: `#101010` and `#212121`.
- Muted copy: gray utility colors.

Motion:

- Pull-up word animation for hero and section headings.
- Staggered card entrance animations.
- Scroll-linked character opacity reveal in the system section.
- Keep all animations subtle enough that dashboard usability remains clear.

Custom CSS:

- `.noise-overlay` for hero.
- `.bg-noise` for feature and console surfaces.

Responsive behavior:

- Hero type scales by viewport width.
- Feature cards: 1 column mobile, 2 columns tablet, 4 columns desktop.
- Console: stacked on mobile, split layout on desktop.
- Text must not overlap cards, controls, or run tables at mobile widths.

## Testing and Verification

Front-end verification:

- `npm run build` must pass.
- TypeScript must compile without errors.
- Rendered page must load at Vite dev URL.

Backend integration verification:

- Build assets into `backend/src/main/resources/static`.
- Rebuild backend jar or Docker image.
- Confirm `http://localhost:8080/` returns the new React page.
- Confirm `http://localhost:8080/api/health` still returns status JSON.
- Confirm `/api/runs` still renders in the console.

Functional smoke checks:

- Empty run list renders cleanly.
- Health status renders when backend is reachable.
- Existing run details render without crashing when `resultJson` is absent.
- ATDP timeline renders empty state when no events exist.

## Out of Scope for First Pass

- Authentication.
- Multi-route application shell.
- WebSocket live streaming.
- Kafka cluster control UI.
- Editing strategy candidates from the browser.
- Fully containerized Node build stage.

## Open Implementation Notes

- Use a generated abstract background or CSS/canvas-style visual rather than relying on the Prisma video URLs, because the project domain is technical and offline reliability matters.
- Avoid animal-themed copy or mascot emphasis in the UI; use the existing project name as a system name.
- Keep the dashboard controls visible and operational, not buried after too much marketing copy.
