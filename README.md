# 🛡️ Sentinel-Graph

> Multi-agent AI code review system powered by LangGraph with human-in-the-loop approval workflow.

Sentinel-Graph uses **three specialized AI agents** running in parallel to analyze your code for bugs, style issues, and performance problems — then lets you selectively approve findings before generating the refactored code.

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                         BROWSER (React + Tailwind)                   │
│  ┌──────────────┐  ┌──────────────────────────────────────────────┐  │
│  │ Monaco Editor │  │  Tabs: Logs │ Findings │ Diff Results       │  │
│  └──────────────┘  └──────────────────────────────────────────────┘  │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ REST API
┌──────────────────────────────▼───────────────────────────────────────┐
│                    NODE.JS (Express + Hexagonal Arch)                 │
│  ┌────────┐   ┌────────────────────┐   ┌─────────────────────────┐  │
│  │ Routes │──▶│ CodeReviewService  │──▶│ PythonGraphAdapter      │  │
│  │ (Zod)  │   │ + DiffService      │   │ (Port → Python Service) │  │
│  └────────┘   └────────────────────┘   └───────────┬─────────────┘  │
└────────────────────────────────────────────────────┼────────────────┘
                                                     │ HTTP
┌────────────────────────────────────────────────────▼────────────────┐
│                    PYTHON (FastAPI + LangGraph)                      │
│                                                                      │
│  START ──▶ Router ──┬──▶ BugHunter ────┐                            │
│                     ├──▶ StyleGuard ───┤──▶ Synthesizer             │
│                     └──▶ PerfArchitect ┘        │                   │
│                                          [HITL Interrupt]           │
│                                                 │                   │
│                                           Refactor ──▶ END         │
│                                                                      │
│  Checkpointing: MemorySaver (thread_id per review)                  │
│  LLM: Ollama (local, configurable model)                            │
└─────────────────────────────────────────────────────────────────────┘
```

The system uses a **three-tier hexagonal architecture**:

| Layer | Technology | Responsibility |
|-------|-----------|----------------|
| **Frontend** | React 18, Monaco Editor, Tailwind CSS | IDE-style code review UI |
| **API** | Node.js, Express, Zod, EJS | Request validation, diff computation, serving |
| **AI Engine** | Python, FastAPI, LangGraph, Ollama | Multi-agent graph execution with HITL |

---

## ✨ Features

- **🤖 Multi-Agent Analysis** — Three specialized agents run in parallel:
  - **BugHunter** — Finds logical bugs and edge cases
  - **StyleGuard** — Reviews naming, readability, and conventions
  - **PerfArchitect** — Identifies performance bottlenecks
- **👤 Human-in-the-Loop** — Approve or reject each finding individually
- **📝 Monaco Editor** — Full IDE experience with syntax highlighting
- **📊 Side-by-Side Diff** — GitHub-style diff view of changes
- **🔄 Agent Activity Logs** — Real-time pipeline visualization
- **🛡️ Crash Isolation** — Agent failures don't break the review
- **✅ Input Validation** — Zod (Node) + Pydantic (Python) validation
- **📋 Structured Logging** — Request tracking across both services

---

## 🧰 Tech Stack

| Component | Technology |
|-----------|-----------|
| AI Framework | LangGraph (Python) |
| LLM | Ollama (local, any model) |
| Backend API | Node.js + Express |
| Validation | Zod (Node) + Pydantic (Python) |
| Architecture | Hexagonal (Ports & Adapters) |
| Frontend | React 18 + Tailwind CSS |
| Code Editor | Monaco Editor |
| Diff Engine | `diff` (Node.js) |
| Template | EJS (server-rendered shell) |

---

## 🚀 Setup

### Prerequisites

- [Node.js](https://nodejs.org/) v18+
- [Python](https://python.org/) 3.10+
- [Ollama](https://ollama.ai/) running locally

### 1. Clone & Install

```bash
git clone https://github.com/your-username/sentinel-graph.git
cd sentinel-graph

# Node.js dependencies
npm install

# Python dependencies
cd python_service
pip install -r requirements.txt
cd ..
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env to customize PORT, model, etc.
```

### 3. Pull the Ollama Model

```bash
ollama pull qwen2.5-coder:1.5b
```

### 4. Start Both Services

**Terminal 1 — Python AI Engine:**
```bash
cd python_service
uvicorn main:app --reload --port 8000
```

**Terminal 2 — Node.js API:**
```bash
npm run dev
```

### 5. Open the UI

Navigate to [http://localhost:3000](http://localhost:3000)

---

## 🔄 How It Works

```
User Flow:

1. Paste code in Monaco Editor
2. Click "Analyze Code" (or Ctrl+Enter)
3. Watch agent logs animate in real-time
4. Review findings — each with type, severity, and suggestion
5. Approve ✓ or Reject ✕ individual suggestions
6. Click "Commit Changes"
7. View side-by-side diff of original vs refactored code
```

### API Flow

```
POST /review { input_code }
  → Node.js validates → forwards to Python
  → Router → BugHunter + StyleGuard + PerfArchitect (parallel)
  → Synthesizer (deduplicate + rank)
  → HITL Interrupt (graph pauses)
  ← Returns { status: "PENDING_APPROVAL", thread_id, aggregated_report }

POST /review/approve { thread_id, approved_suggestions }
  → Node.js validates → forwards to Python
  → Graph resumes → Refactor node applies suggestions
  → Node.js computes diff
  ← Returns { status: "COMPLETED", final_code, diffData }
```

---

## 📸 Screenshots

> Add screenshots here after running the application.

| View | Description |
|------|-------------|
| Editor View | IDE-style code input with Monaco Editor |
| Agent Logs | Real-time agent pipeline visualization |
| Findings Panel | Approve/reject individual suggestions |
| Diff View | Side-by-side code comparison |

---

## 🔮 Future Improvements

- [ ] WebSocket for true real-time agent streaming
- [ ] Persistent review history (database)
- [ ] Support for multiple files / projects
- [ ] Custom agent configuration
- [ ] Export reports (PDF / Markdown)
- [ ] CI/CD integration (GitHub Actions)
- [ ] Authentication and team collaboration

---

## 📌 Resume Bullet Points

- Designed and built a **multi-agent AI code review system** using **LangGraph** with three specialized agents (BugHunter, StyleGuard, PerfArchitect) executing in parallel
- Implemented **human-in-the-loop (HITL)** workflow using LangGraph's `interrupt_before` checkpoint mechanism for selective suggestion approval
- Built a **hexagonal architecture** Node.js API layer with **Zod validation**, **port-adapter pattern**, and server-side diff computation
- Created an **IDE-style React frontend** with **Monaco Editor**, tabbed panels, agent activity logs, and GitHub-style side-by-side diff visualization
- Engineered **crash isolation** per agent — individual agent failures return empty results without breaking the pipeline
- Implemented **structured output parsing** with retry logic for reliable LLM-to-JSON extraction

---

## 📄 License

MIT
