# 🎬 Demo Walkthrough

> Step-by-step script for demoing Sentinel-Graph.

## Prerequisites

1. Ollama running with model pulled: `ollama pull qwen2.5-coder:1.5b`
2. Python service running: `cd python_service && uvicorn main:app --reload --port 8000`
3. Node.js server running: `npm run dev`
4. Browser open at: `http://localhost:3000`

---

## Demo Script

### Step 1 — Paste Code

Paste this sample code into the Monaco Editor:

```javascript
function fetchData(url) {
  var data = fetch(url)
  var result = JSON.parse(data)
  for (var i = 0; i < result.length; i++) {
    console.log(result[i])
    var x = result[i].value
    if (x = 10) {
      processItem(result[i])
    }
  }
  return result
}

function processItem(item) {
  var temp = item.name + " " + item.value
  return temp
}
```

> ☝️ This code has intentional bugs (assignment in condition), style issues (var usage, short variable names), and performance issues (no async handling).

---

### Step 2 — Click "Analyze Code"

Press the **⚡ Analyze Code** button (or `Ctrl+Enter`).

**What happens:**
- Status badge changes from "Ready" to "Analyzing"
- Right panel switches to **Logs** tab
- Agent activity animates progressively

---

### Step 3 — Observe Agent Logs

Watch the agent pipeline execute:

```
🔍 Router       — Analyzing code structure...        ✓ Done
🐛 BugHunter    — Scanning for bugs and errors...    ✓ Done
✨ StyleGuard    — Checking code style...             ✓ Done
⚡ PerfArchitect — Analyzing performance patterns...  ✓ Done
📋 Aggregator   — Compiling report...                ✓ Done
```

---

### Step 4 — Review Findings

The UI auto-switches to the **Findings** tab.

Each finding shows:
- **Type badge** (bug / style / performance)
- **Severity badge** (high / medium / low) — color-coded
- **Message** — what the issue is
- **Suggestion** — how to fix it

All findings start **pre-approved** (✓ green).

---

### Step 5 — Approve/Reject Suggestions

- Click **✕** to reject a suggestion you disagree with
- Click **✓** to re-approve it
- Use **"Approve All"** / **"Reject All"** for bulk operations

The approved count updates in the action bar.

---

### Step 6 — Click "Commit Changes"

Press the **✓ Commit Changes** button.

**What happens:**
- Status changes to "Committing"
- The approved suggestions are sent to the Refactor agent
- The LLM applies only the approved changes

---

### Step 7 — View Results

The UI auto-switches to the **Results** tab.

You'll see:
- **Stats bar** — `+N / −N lines changed`
- **Side-by-side diff** — Original (left) vs Refactored (right)
- Added lines highlighted in **green**
- Removed lines highlighted in **red**

---

### Step 8 — Start Over

Click **"New Review"** to reset everything and review another piece of code.

---

## 💡 Demo Talking Points

1. **"Three agents run in parallel"** — Router fans out to BugHunter, StyleGuard, and PerfArchitect simultaneously
2. **"Human stays in control"** — The graph pauses before refactoring, waiting for explicit approval
3. **"Crash isolation"** — If one agent fails, the others still return results
4. **"LangGraph checkpointing"** — Each review gets a unique thread_id, the graph state is preserved between the two API calls
5. **"Hexagonal architecture"** — The Node.js layer has zero knowledge of LangGraph internals
