# RatsNestPro integration and multi-agent assessment

## Assessment

RatsNestPro is a real role-based multi-agent system, not just a collection of
classes named “agent”. Its original implementation has:

- separate Architect, Coder, and Reviewer roles with different prompts,
  contracts, permissions, and responsibilities;
- structured handoffs through Pydantic models rather than unvalidated prose;
- an Architect → generation → deterministic verification → Coder repair →
  re-verification loop;
- an independent Reviewer that cannot alter authoritative gate severities;
- bounded tool authority: the Coder can only apply validated `set_param`
  operations and cannot write arbitrary files or execute a shell;
- a fixed 17-step knowledge-driven PCB pipeline with fail-closed checks.

Before this integration it was a weakly autonomous, fixed-orchestration MAS.
The roles were real, but there was no LangGraph supervisor, graph-level
handoff, service conversation state, or frontend-visible delegation.

## Framework-native form

The registered system ID is `ratsnestpro-multi-agent`. It uses the same
`create_agent` + `create_supervisor` pattern as the toolkit's existing
LangGraph supervisor examples:

| Sub-agent | Framework responsibility | Preserved RatsNestPro capability |
| --- | --- | --- |
| Architect | Requirement research and plan delegation, with Web Search | Family judgment, parameter selection, immutable `DesignPlan` |
| Hardware Engineer | Generation and repair delegation | Pipeline A and the full 17-step pipeline B |
| Reviewer | Independent audit delegation | Arbitrary KiCad project review and severity-preserving narrative |
| Parts Specialist | Grounded catalog delegation | Local JLCPCB SQLite search without invented MPN/LCSC data |

The supervisor can select one role or make sequential handoffs. For example, a
request to generate and then audit a board can be delegated first to the
Hardware Engineer and then to the Reviewer. Existing service streaming exposes
these transfers to the Next.js structured workflow-event UI.

The original deterministic core remains the authority. The outer LangGraph
agents decide which workflow to invoke and explain results; they do not replace
EDA validation, process limits, typed contracts, or gate verdicts.

ATmega328 is the deterministic offline example, not the system's family
boundary. A named non-ATmega MCU is sent to pipeline B in `required` mode through
an adapter to the toolkit model. Before component selection, the pipeline scans
the installed KiCad libraries and gives the model exact matching MCU symbol and
default-footprint IDs. Bottom-line checks then require both the requested MCU
identity and its declared package. Consequently, a request such as RP2040 cannot
fall back to or be relabeled from the ATmega example.

## Service and frontend

No new protocol is required:

- `GET /info` advertises `ratsnestpro-multi-agent`;
- `POST /ratsnestpro-multi-agent/invoke` supports non-streaming calls;
- `POST /ratsnestpro-multi-agent/stream` supports tokens, tools, and handoffs;
- the existing history, thread ownership, feedback, authentication, and AG-UI
  facilities continue to apply;
- the Next.js agent selector discovers the system from `/info`.

Docker Compose mounts `data/ratsnestpro` on the host at
`/data/ratsnestpro` in the service. Generated artifacts are written below
`data/ratsnestpro/runs`; review reports are written below
`data/ratsnestpro/reviews`. Web tools reject review paths outside this
workspace.

## Running

Create the normal toolkit `.env` first. A real framework LLM provider is needed
for supervisor routing; `USE_FAKE_MODEL=true` is suitable only for smoke tests.

```powershell
Copy-Item .env.example .env
docker compose up --build
```

To run the supervisor and its sub-agents through DeepSeek V4, configure:

```dotenv
DEEPSEEK_API_KEY=<your-deepseek-api-key>
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEFAULT_MODEL=deepseek-v4-flash
USE_FAKE_MODEL=false
```

`deepseek-v4-pro` can be selected instead when higher model quality is more
important than latency and cost. Docker Compose loads these values from the
root `.env`; no API key should be added to the image or committed to source.
DeepSeek thinking mode is disabled in the framework adapter because the current
LangChain OpenAI-compatible message conversion does not replay DeepSeek's
provider-specific `reasoning_content` field across tool-call turns. Non-thinking
mode retains function calling and avoids failed multi-agent handoffs.

Open the Next.js frontend at `http://localhost:3000` and select `ratsnestpro-multi-agent`.
Example requests:

- `为 ATmega328 USB-C 5V 16MHz 开发板生成不可变设计计划，run_name 用 demo-plan。`
- `运行完整 17 步 PCB 流程：ATmega328 USB-C 3.3V 8MHz，run_name 用 demo-pcb。`
- `审查 runs/demo-pcb 里的 KiCad 工程并生成 Markdown 报告。`
- `在本地 JLCPCB 缓存中搜索 10k 0603。`

The original CLI is installed in the same service image:

```powershell
docker compose run --rm agent_service ratsnestpro --help
```

## Capability boundaries

- `offline` is valid only for the ATmega328 deterministic example. The outer
  LangGraph supervisor still needs a configured toolkit model.
- A non-ATmega requirement upgrades `offline` or `auto` to `required` and uses
  the toolkit's configured model through the RatsNestPro text-client adapter.
- The supervisor and Architect expose DuckDuckGo-backed `web_search`; research
  output is advisory, while KiCad-library and deterministic gate checks remain
  authoritative.
- The service image includes Debian KiCad 9, Java 25, and checksum-pinned
  Freerouting 2.2.4. Compose enables `RATSNESTPRO_REQUIRE_FREEROUTING`, making
  DSN export, SES import, and zero unconnected items a blocking production gate.
- Set `RATSNESTPRO_REQUIRE_FREEROUTING=false` only for planning/test contexts
  where an unrouted PCB is intentionally acceptable as an intermediate.
- Real symbol and footprint grounding requires container-visible
  `KICAD_SYMBOL_DIR` and `KICAD_FOOTPRINT_DIR`.
- Grounded part search requires `jlcpcb.sqlite` below the configured
  `KICAD_MCP_HOME` (Compose defaults it to
  `/data/ratsnestpro/cache`).
