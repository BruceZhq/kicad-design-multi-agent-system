# 🧰 AI Agent Service Toolkit

[![build status](https://github.com/JoshuaC215/agent-service-toolkit/actions/workflows/test.yml/badge.svg)](https://github.com/JoshuaC215/agent-service-toolkit/actions/workflows/test.yml) [![codecov](https://codecov.io/github/JoshuaC215/agent-service-toolkit/graph/badge.svg?token=5MTJSYWD05)](https://codecov.io/github/JoshuaC215/agent-service-toolkit) [![Python Version](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2FJoshuaC215%2Fagent-service-toolkit%2Frefs%2Fheads%2Fmain%2Fpyproject.toml)](https://github.com/JoshuaC215/agent-service-toolkit/blob/main/pyproject.toml)
[![GitHub License](https://img.shields.io/github/license/JoshuaC215/agent-service-toolkit)](https://github.com/JoshuaC215/agent-service-toolkit/blob/main/LICENSE)

A distributed multi-agent engineering system built with LangGraph, FastAPI, Temporal,
PostgreSQL, Redis, Kafka, Next.js, React, and TypeScript.

The browser communicates through a same-origin Next.js BFF. Agent output is streamed
with `fetch()` and `ReadableStream`; live cross-instance run state and replay live in
Redis, LangGraph checkpoints live in PostgreSQL, long Hardware Engineer execution lives
in Temporal, and versioned audit metadata is relayed to Kafka.

This project offers a template for you to easily build and run your own agents using the LangGraph framework. It demonstrates a complete setup from agent definition to user interface, making it easier to get started with LangGraph-based projects by providing a full, robust toolkit.

**[🎥 Watch a video walkthrough of the repo and app](https://www.youtube.com/watch?v=pdYVHw_YCNY)**

## Overview

### Quickstart

Run directly in python

```sh
# At least one LLM API key is required
echo 'OPENAI_API_KEY=your_openai_api_key' >> .env

# uv is the recommended way to install agent-service-toolkit, but "pip install ." also works
# For uv installation options, see: https://docs.astral.sh/uv/getting-started/installation/
curl -LsSf https://astral.sh/uv/0.11.29/install.sh | sh

# Install dependencies. "uv sync" creates .venv automatically
uv sync --frozen
source .venv/bin/activate
python src/run_service.py

# In another shell, start the TypeScript web frontend
cd frontend
npm ci
npm run dev
```

Run with docker

```sh
echo 'OPENAI_API_KEY=your_openai_api_key' >> .env
docker compose watch
```

### Architecture Diagram

<img src="media/agent_architecture.png" width="600" alt="Agent architecture diagram">

### Key Features

1. **LangGraph Agent and latest features**: A customizable agent built using the LangGraph framework. Implements the latest LangGraph v1.0 features including human in the loop with `interrupt()`, flow control with `Command`, long-term memory with `Store`, and `langgraph-supervisor`.
1. **FastAPI Service**: Serves the agent with both streaming and non-streaming endpoints.
1. **Advanced Streaming**: A novel approach to support both token-based and message-based streaming.
1. **AG-UI Protocol Support**: Every agent is also served over the [AG-UI protocol](https://docs.ag-ui.com) for connecting AG-UI compatible frontends like CopilotKit - see [docs](docs/AGUI.md).
1. **Next.js/React Interface**: Uses a server-side BFF and incremental `fetch` + `ReadableStream` SSE parsing without exposing the FastAPI credential.
1. **Distributed Run Control**: Redis provides cross-replica idempotency, leases, cancellation, bounded SSE replay, and the Kafka audit outbox.
1. **Durable Hardware Workflow**: Temporal owns long KiCad/Freerouting activities, retries, timeouts, Event History, and recovery boundaries.
1. **Audit Backbone**: A consumer-group relay publishes versioned metadata to Kafka with at-least-once delivery and stable deduplication IDs.
1. **Immutable Deliveries**: Content-addressed S3-compatible artifacts, short-lived authorized downloads, and human-feedback Run Revisions preserve every engineering version.
1. **Multiple Agent Support**: Run multiple agents in the service and call by URL path. Available agents and models are described in `/info`
1. **Asynchronous Design**: Utilizes async/await for efficient handling of concurrent requests.
1. **Content Moderation**: Implements Safeguard for content moderation (requires Groq API key).
1. **RAG Agent**: A basic RAG agent implementation using ChromaDB - see [docs](docs/RAG_Assistant.md).
1. **Feedback Mechanism**: Includes a star-based feedback system integrated with LangSmith.
1. **Docker Support**: Includes Dockerfiles and a docker compose file for easy development and deployment.
1. **Testing**: Includes robust unit and integration tests for the full repo.

### Key Files

The repository is structured as follows:

- `src/agents/`: Defines several agents with different capabilities
- `src/schema/`: Defines the protocol schema
- `src/core/`: Core modules including LLM definition and settings
- `src/service/service.py`: FastAPI service to serve the agents
- `src/client/client.py`: Client to interact with the agent service
- `frontend/`: Next.js/React/TypeScript web application and server-only BFF
- `src/service/redis_run_registry.py`: distributed run lifecycle and SSE replay
- `src/service/kafka_relay.py`: Redis outbox to Kafka audit relay
- `tests/`: Unit and integration tests

## Setup and Usage

1. Clone the repository:

   ```sh
   git clone https://github.com/JoshuaC215/agent-service-toolkit.git
   cd agent-service-toolkit
   ```

2. Set up environment variables:
   Create a `.env` file in the root directory. At least one LLM API key or configuration is required. See the [`.env.example` file](./.env.example) for a full list of available environment variables, including a variety of model provider API keys, header-based authentication, LangSmith tracing, testing and development modes, and OpenWeatherMap API key.

3. Run the complete development stack with Docker, or run FastAPI and the Next.js frontend separately for local development.

### Additional setup for specific AI providers

- [Setting up Ollama](docs/Ollama.md)
- [Setting up VertexAI](docs/VertexAI.md)
- [Setting up RAG with ChromaDB](docs/RAG_Assistant.md)

### Building or customizing your own agent

To customize the agent for your own use case:

1. Add your new agent to the `src/agents` directory. You can copy `research_assistant.py` or `chatbot.py` and modify it to change the agent's behavior and tools.
1. Import and add your new agent to the `agents` dictionary in `src/agents/agents.py`. Your agent can be called by `/<your_agent_name>/invoke` or `/<your_agent_name>/stream`.
1. Extend the typed event rendering in `frontend/components/chat-console.tsx` when an agent introduces a new client-visible event contract.

### RatsNestPro multi-agent PCB system

The embedded `src/RatsNestPro-main/RatsNestPro-main` project is registered as
`ratsnestpro-multi-agent`. It keeps the original RatsNestPro CLI and deterministic
EDA core, while exposing its capabilities through a LangGraph supervisor with
Architect, Hardware Engineer, Reviewer, and grounded Parts Specialist sub-agents.
The existing `/info`, `/{agent_id}/invoke`, `/{agent_id}/stream`, history, and
Next.js agent discovery and selection work through the existing `/info` endpoint.

With Docker Compose, generated KiCad, BOM, CPL, Gerber, plan, gate, and review
artifacts are persisted under `data/ratsnestpro` on the host. Paths passed to the
review agent must remain inside that workspace. ATmega328 is an offline example
template, not a supported-family limit. Other named MCUs use the adaptive
17-step pipeline and the toolkit's configured model (including DeepSeek), plus
the installed KiCad symbol/footprint libraries. The supervisor and Architect
also have a real `web_search` tool for manufacturer datasheets and reference
designs.

Non-ATmega requests cannot silently enter the ATmega fallback: their effective
LLM mode is forced to `required`, the requested MCU must occur in the selected
BOM, and its library-defined package must match. A failed identity or package
check blocks generation instead of relabeling an ATmega board.

The service image includes KiCad 9, Java 25, and pinned Freerouting 2.2.4.
Compose sets `RATSNESTPRO_REQUIRE_FREEROUTING=true`, so a full PCB request
cannot report success unless DSN export, Freerouting, SES import, and the
zero-unconnected check all complete. Set the flag to `false` only when an
unrouted board is intentionally acceptable as an intermediate artifact.

The production intent router, task-local AHE convergence loop, cross-task EHE
memory, checkpoint/concurrency model, and SSE event flow are documented in
[RatsNestPro Intent + AHE + EHE Architecture](docs/RATSNESTPRO_INTENT_AHE_EHE_ARCHITECTURE.md).
The intake accepts ordinary Chinese or English instead of requiring a long
template. Ambiguous hardware goals are resolved by a compact LLM classifier;
greetings and unrelated questions receive a lightweight conversational answer
without entering the PCB pipeline.
The production run lifecycle, PostgreSQL state ownership, resumable SSE protocol,
admission control, cancellation, health checks, and deployment settings are documented
in [RatsNestPro Production Runtime](docs/RATSNESTPRO_PRODUCTION_RUNTIME.md).
Increment 7 implementation and bounded verification evidence are recorded in
[Increment 7 Acceptance](docs/increment-7-acceptance.md).

### Handling Private Credential files

If your agents or chosen LLM require file-based credential files or certificates, the `privatecredentials/` has been provided for your development convenience. All contents, excluding the `.gitkeep` files, are ignored by git and docker's build process. See [Working with File-based Credentials](docs/File_Based_Credentials.md) for suggested use.

### Docker Setup

The Compose development stack includes PostgreSQL, Redis, a single-node Kafka broker,
Temporal, FastAPI, a Temporal Hardware Engineer worker, the Kafka audit relay, and the
Next.js frontend. Single-node Compose is not a production HA topology; see
[Distributed Runtime](docs/DISTRIBUTED_RUNTIME.md) for production boundaries.

For local development, we recommend using [docker compose watch](https://docs.docker.com/compose/file-watch/). This feature allows for a smoother development experience by automatically updating your containers when changes are detected in your source code.

1. Make sure you have Docker and Docker Compose (>= [v2.23.0](https://docs.docker.com/compose/release-notes/#2230)) installed on your system.

2. Create a `.env` file from the `.env.example`. At minimum, you need to provide an LLM API key (e.g., OPENAI_API_KEY).

   ```sh
   cp .env.example .env
   # Edit .env to add your API keys
   ```

3. Build and launch the services in watch mode:

   ```sh
   docker compose watch
   ```

   This will automatically:
   - Start a PostgreSQL database service that the agent service connects to
   - Start the agent service with FastAPI
   - Start Redis-backed distributed run coordination and SSE replay
   - Start the Kafka audit relay and Temporal Hardware Engineer worker
   - Start the Next.js web interface

4. The services will now automatically update when you make changes to your code:
   - Changes in the relevant python files and directories will trigger updates for the relevant services.
   - NOTE: If you make changes to the `pyproject.toml` or `uv.lock` files, you will need to rebuild the services by running `docker compose up --build`.

5. Access the web application at `http://localhost:3000`.

6. The agent service API will be available at `http://0.0.0.0:8080`. You can also use the OpenAPI docs at `http://0.0.0.0:8080/redoc`.

7. Use `docker compose down` to stop the services.

This setup allows you to develop and test your changes in real-time without manually restarting the services.

### Building other apps on the AgentClient

The repo includes a generic `src/client/client.AgentClient` that can be used to interact with the agent service. This client is designed to be flexible and can be used to build other apps on top of the agent. It supports both synchronous and asynchronous invocations, and streaming and non-streaming requests.

See the `src/run_client.py` file for full examples of how to use the `AgentClient`. A quick example:

```python
from client import AgentClient
client = AgentClient()

response = client.invoke("Tell me a brief joke?")
response.pretty_print()
# ================================== Ai Message ==================================
#
# A man walked into a library and asked the librarian, "Do you have any books on Pavlov's dogs and Schrödinger's cat?"
# The librarian replied, "It rings a bell, but I'm not sure if it's here or not."

```

### Development with LangGraph Studio

The agent supports [LangGraph Studio](https://langchain-ai.github.io/langgraph/concepts/langgraph_studio/), the IDE for developing agents in LangGraph.

`langgraph-cli[inmem]` is installed with `uv sync`. You can simply add your `.env` file to the root directory as described above, and then launch LangGraph Studio with `langgraph dev`. Customize `langgraph.json` as needed. See the [local quickstart](https://langchain-ai.github.io/langgraph/cloud/how-tos/studio/quick_start/#local-development-server) to learn more.

### Local development without Docker

You can also run FastAPI and the TypeScript frontend locally without Docker.

1. Create a virtual environment and install dependencies:

   ```sh
   uv sync --frozen
   source .venv/bin/activate
   ```

2. Run the FastAPI server:

   ```sh
   python src/run_service.py
   ```

3. In a separate terminal, run the Next.js frontend:

   ```sh
   cd frontend
   npm ci
   npm run dev
   ```

4. Open `http://localhost:3000`.

## Projects built with or inspired by agent-service-toolkit

The following are a few of the public projects that drew code or inspiration from this repo.

- **[PolyRAG](https://github.com/QuentinFuxa/PolyRAG)** - Extends agent-service-toolkit with RAG capabilities over both PostgreSQL databases and PDF documents.
- **[alexrisch/agent-web-kit](https://github.com/alexrisch/agent-web-kit)** - A Next.JS frontend for agent-service-toolkit
- **[raushan-in/dapa](https://github.com/raushan-in/dapa)** - Digital Arrest Protection App (DAPA) enables users to report financial scams and frauds efficiently via a user-friendly platform.

**Please create a pull request editing the README or open a discussion with any new ones to be added!** Would love to include more projects.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

**A note on how this repo is maintained:** this is a solo-maintainer project, and issues, PRs, and discussions are triaged on a roughly biweekly cycle with help from an AI maintenance agent. Thanks for your patience if responses take a week or two — I will do my best to respond to truly urgent issues (vulnerability reports, etc.) or in-progress PRs within a few days. The full automation playbooks are versioned in [`docs/maintenance/`](docs/maintenance/) if you're curious how it works.

Currently the tests need to be run using the local development without Docker setup. To run the tests for the agent service:

1. Ensure you're in the project root directory and have activated your virtual environment.

2. Install the development dependencies and pre-commit hooks:

   ```sh
   uv sync --frozen
   pre-commit install
   ```

3. Run the tests using pytest:

   ```sh
   pytest
   ```

### Smoke testing optional dependencies

Some integrations aren't exercised by the unit suite or the default CI run because they
need real infrastructure: the Postgres and MongoDB checkpointers, the AG-UI endpoint, and
LangFuse tracing. `scripts/smoke_test.sh` spins up each dependency in Docker, runs the
service against it, verifies the integration end-to-end (including a check that the
intended backend was actually used, not a silent SQLite fallback), and tears it down.

```sh
./scripts/smoke_test.sh                 # default: postgres, mongo, agui
./scripts/smoke_test.sh mongo           # a single target
./scripts/smoke_test.sh langfuse        # heavy: starts LangFuse's full self-host stack
./scripts/smoke_test.sh all             # everything, including langfuse
```

These are opt-in confidence checks for a maintainer or agent — not part of CI. Run the
target that matches what you changed rather than the whole set. The optional add-on
compose files live in `docker/` (e.g. `docker/compose.mongo.yaml`), layered on top of the
default `compose.yaml` so the default stack stays lightweight.

## License

This project is licensed under the MIT License - see the LICENSE file for details.
