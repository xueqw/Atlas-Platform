# Atlas Agent Platform

Atlas is an enterprise agent platform built with React, TypeScript, FastAPI, and SQLAlchemy. It provides a unified workspace for multi-turn task sessions, server-sent event (SSE) streaming, semantic knowledge-base retrieval, multi-model routing, and an extensible connector and tool system.

Current version: **v1.1.0** · [Changelog](CHANGELOG.md) · [Release process](docs/RELEASING.md)

## Features

### Agent Workspace

- Persistent multi-turn task sessions with automatic saving and recovery.
- SSE streaming responses with references to relevant knowledge-base sources.
- Configurable selection of agents, knowledge bases, models, and connectors for each session.

### Knowledge Base and RAG Retrieval

- Upload and automatically parse and chunk PDF, DOCX, TXT, Markdown, CSV, and JSON files.
- Generate `bge-m3` embeddings through SiliconFlow and perform cosine-similarity search with keyword fallback.
- Use dependency-free BM25 ranking for resilient Chinese and English keyword retrieval when embeddings are unavailable or incompatible.
- Support cross-lingual retrieval, including Chinese queries against English-language documents.
- Display citation cards containing the document name, page number, and relevant source excerpt.

![Retrieval quality improvements in version 1.1.0](docs/retrieval-improvement.svg)

### Model Gateway

- Route requests automatically to multiple providers based on the selected model name, including Zhipu GLM and Alibaba Qwen.
- Test provider connectivity and manage credentials through `api/.env`.

### Connectors and Tools

- Support an agent-driven tool execution loop: the agent selects a tool, executes it, receives the result, and continues the response.
- Require explicit user confirmation before write operations such as sending messages or creating issues.
- Integrate with Feishu through OAuth 2.0 for sending messages to the current user, an email address, or a mobile number.
- Connect to GitHub through its official remote MCP server and dynamically expose more than 40 tools.
- Allow users to enable or disable individual connectors from the conversation composer.

## Getting Started

### Prerequisites

- Node.js and npm
- Python 3.10 or later
- Docker and Docker Compose

### Start Supporting Services

Start PostgreSQL with the `pgvector` extension and Redis from the project root:

```bash
docker compose up -d postgres redis
```

### Start the Development Environment

Run the project development script from the project root:

```powershell
./start-dev.ps1
```

Once the services are running, the main interfaces are available at:

- React workspace: <http://localhost:5173>
- FastAPI documentation: <http://localhost:8000/docs>

By default, the application uses `api/data/atlas.db` and does not require a separate database installation for local development.

## Verification

Run the API regression suite from the project root:

```bash
cd api
python -m unittest discover -s tests -v
```

Build the web application with the locked dependency versions:

```bash
cd web
npm ci
npm run build
```

The same checks run automatically for every pull request and every push to `main`.

## Technology Stack

- React
- TypeScript
- FastAPI
- SQLAlchemy
- Model Context Protocol (MCP)
- `bge-m3`
