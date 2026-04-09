import json
import logging
import operator
import os
import re
import string
import time
import uuid
from typing import TypedDict, Annotated

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_ollama import ChatOllama

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sentinel-graph")

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:3b")
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

logger.info(f"Using model: {OLLAMA_MODEL} (temp={OLLAMA_TEMPERATURE})")

llm = ChatOllama(model=OLLAMA_MODEL, temperature=OLLAMA_TEMPERATURE)

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}

checkpointer = MemorySaver()


# ──────────────────────────────────────────────
# EXCEPTIONS
# ──────────────────────────────────────────────

class StructuredOutputError(Exception):
    pass


# ──────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────

class CodeReviewState(TypedDict):
    thread_id: str
    session_type: str   # "problem" | "code"
    problem_description: str
    input_code: str
    language: str
    messages: Annotated[list, operator.add]
    findings: Annotated[list, operator.add]
    aggregated_report: list
    approved_suggestions: list
    final_code: str


# ──────────────────────────────────────────────
# PARSING UTILS
# ──────────────────────────────────────────────

def parse_findings(raw: str) -> list:
    text = raw.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise StructuredOutputError("STRUCTURED_OUTPUT_ERROR: No JSON array found in LLM response")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        raise StructuredOutputError("STRUCTURED_OUTPUT_ERROR: Invalid JSON in LLM response")
    if not isinstance(parsed, list):
        raise StructuredOutputError("STRUCTURED_OUTPUT_ERROR: Expected JSON array")
    return parsed


def invoke_with_retry(messages: list, agent_name: str, max_retries: int = None) -> list:
    retries = max_retries if max_retries is not None else MAX_RETRIES
    for attempt in range(retries):
        try:
            response = llm.invoke(messages)
            logger.debug(f"[{agent_name}] Attempt {attempt+1} raw output: {response.content[:300]}")
            findings = parse_findings(response.content)
            logger.info(f"[{agent_name}] Attempt {attempt+1} — parsed {len(findings)} findings")
            return findings
        except StructuredOutputError:
            logger.warning(f"[{agent_name}] Attempt {attempt+1}/{retries} — parse failed")
            if attempt == retries - 1:
                raise
    raise StructuredOutputError("STRUCTURED_OUTPUT_ERROR: All retries exhausted")


# ──────────────────────────────────────────────
# GRAPH NODES
# ──────────────────────────────────────────────

def router(state: CodeReviewState):
    code = state["input_code"]
    python_patterns = [r"\bdef\b", r"\bimport\b", r":\s*$"]
    js_patterns = [r"\bfunction\b", r"\bconst\b", r"\blet\b", r"=>"]
    python_score = sum(1 for p in python_patterns if re.search(p, code, re.MULTILINE))
    js_score = sum(1 for p in js_patterns if re.search(p, code, re.MULTILINE))
    language = "python" if python_score >= js_score else "javascript"
    logger.info(f"[Router] Detected language: {language} (py={python_score}, js={js_score})")
    return {"language": language}


def route_by_language(state: CodeReviewState) -> list[str]:
    return ["BugHunter", "StyleGuard", "PerfArchitect"]


def synthesizer(state: CodeReviewState):
    findings = state.get("findings", [])
    unique_findings = []
    
    def get_words(text):
        if not text:
            return set()
        text = str(text).lower()
        text = text.translate(str.maketrans('', '', string.punctuation))
        return set(text.split())
        
    def is_similar(msg1, msg2):
        words1 = get_words(msg1)
        words2 = get_words(msg2)
        if not words1 or not words2:
            return False
        intersection = words1.intersection(words2)
        union = words1.union(words2)
        if not union:
            return False
        jaccard = len(intersection) / len(union)
        norm1 = " ".join(words1)
        norm2 = " ".join(words2)
        return jaccard >= 0.5 or norm1 in norm2 or norm2 in norm1
        
    for f in findings:
        current_msg = f.get("message", "")
        current_sev = f.get("severity", "low")
        current_type = f.get("type", "bug")
        current_source = f.get("source")
        
        merged = False
        for existing in unique_findings:
            if is_similar(current_msg, existing.get("message", "")):
                merged = True
                
                # Merge sources
                if "sources" not in existing:
                    existing["sources"] = []
                    if existing.get("source"):
                        existing["sources"].append(existing["source"])
                if current_source and current_source not in existing["sources"]:
                    existing["sources"].append(current_source)
                
                # Keep highest severity
                if SEVERITY_RANK.get(current_sev, 0) > SEVERITY_RANK.get(existing.get("severity", "low"), 0):
                    existing["severity"] = current_sev
                    existing["message"] = current_msg
                    existing["type"] = current_type
                    if "suggestion" in f:
                        existing["suggestion"] = f["suggestion"]
                    if "line" in f:
                        existing["line"] = f["line"]
                break
                
        if not merged:
            new_f = dict(f)
            src = new_f.pop("source", None)
            new_f["sources"] = [src] if src else []
            unique_findings.append(new_f)

    sorted_findings = sorted(
        unique_findings, key=lambda f: SEVERITY_RANK.get(f.get("severity", "low"), 0), reverse=True
    )
    logger.info(f"[Synthesizer] {len(findings)} raw → {len(unique_findings)} unique → {len(sorted_findings)} sorted")
    return {"aggregated_report": sorted_findings}


# ──────────────────────────────────────────────
# AGENT PROMPTS
# ──────────────────────────────────────────────

BUG_HUNTER_PROMPT = """You are BugHunter, an expert code reviewer focused ONLY on real runtime errors and logical bugs.

STRICT RULES:
- Report ONLY actual, provable bugs. Do NOT guess or speculate.
- Do NOT report performance issues, style issues, or optimizations.
- Do NOT report the same bug more than once (no duplicates).
- If multiple lines cause the same root issue, report it ONLY once.
- Do NOT invent issues like "infinite loop" unless the loop condition is clearly unbounded.
- Do NOT flag safe or valid code as a bug.
- If no real bugs exist, return an empty JSON array [].

DEFINITION OF BUG:
A bug is something that WILL cause:
- runtime error (e.g., division by zero, null/undefined access)
- crash or exception
- incorrect logic leading to wrong results

OUTPUT FORMAT (STRICT):
Return ONLY a valid JSON array.
Each object MUST have exactly these fields:
- "type": always "bug"
- "severity": "low" | "medium" | "high"
- "message": clear and specific description of the bug
- "line": integer line number (or null if unknown)
- "suggestion": actionable fix (not vague advice)

NO extra text. NO markdown. NO explanation.

EXAMPLES:

Input:
def divide(a, b):
    return a / b

Output:
[{"type":"bug","severity":"high","message":"Division by zero may occur if b is 0","line":2,"suggestion":"Add a check to ensure b is not zero before division"}]

---

Input:
def add(a, b):
    return a + b

Output:
[]
"""
STYLE_GUARD_PROMPT = """You are StyleGuard, an expert code reviewer focused ONLY on meaningful naming, formatting, and best practices.

STRICT RULES:
- Report ONLY real and meaningful style issues. Do NOT nitpick or over-criticize.
- Do NOT report bugs or performance issues.
- Do NOT repeat the same issue more than once (no duplicates).
- If multiple instances share the same problem, report it ONLY once.
- Do NOT flag acceptable or commonly used names (e.g., i, j in loops, add, sum, count).
- Only flag naming issues if the name is unclear, misleading, or too generic (e.g., x, temp, data).
- Do NOT suggest renaming if the current name is already clear and descriptive.
- Do NOT invent issues just to return output.
- If no meaningful style issues exist, return an empty JSON array [].

WHAT TO CHECK:
- Poor or unclear naming (functions, variables, classes)
- Inconsistent naming conventions (camelCase vs snake_case)
- Readability issues (very long functions, deeply nested code)
- Minor best practices (missing edge-case handling comments, unclear structure)

OUTPUT FORMAT (STRICT):
Return ONLY a valid JSON array.
Each object MUST have exactly these fields:
- "type": always "style"
- "severity": "low" | "medium" | "high"
- "message": clear and specific description of the issue
- "line": integer line number (or null if unknown)
- "suggestion": actionable improvement (clear and specific)

NO extra text. NO markdown. NO explanation.

EXAMPLES:

Input:
def x(a, b):
    return a + b

Output:
[{"type":"style","severity":"medium","message":"Function name 'x' is not descriptive","line":1,"suggestion":"Rename to 'addNumbers' or a more meaningful name"}]

---

Input:
def add_numbers(a, b):
    return a + b

Output:
[]
"""
PERF_ARCHITECT_PROMPT = """You are PerfArchitect, an expert code reviewer focused ONLY on performance, time complexity, and inefficiencies.

STRICT RULES:
- Report ONLY real and meaningful performance issues.
- Do NOT report bugs, runtime errors, or style issues.
- Do NOT repeat the same issue more than once (no duplicates).
- If multiple lines share the same inefficiency, report it ONLY once.
- Do NOT flag small or acceptable loops as performance issues.
- Only report an issue if it significantly impacts time or space complexity.
- Do NOT invent issues like "N+1 query" unless there is clear evidence (e.g., database/API call inside loop).
- Do NOT flag nested loops unless they cause unnecessary O(n²) or worse complexity.
- If no meaningful performance issues exist, return an empty JSON array [].

WHAT TO DETECT:
- Inefficient time complexity (e.g., O(n²) when O(n) is possible)
- Nested loops that can be optimized
- Repeated expensive operations inside loops
- N+1 query patterns (DB/API calls inside loops)
- Unnecessary recomputation
- Poor data structure choices (e.g., list lookup instead of set/map)

WHAT TO IGNORE:
- Small loops with trivial cost
- Acceptable nested loops where no better approach exists
- Micro-optimizations with negligible impact

OUTPUT FORMAT (STRICT):
Return ONLY a valid JSON array.
Each object MUST have exactly these fields:
- "type": always "performance"
- "severity": "low" | "medium" | "high"
- "message": clear and specific description of the inefficiency
- "line": integer line number (or null if unknown)
- "suggestion": actionable optimization (clear and specific)

NO extra text. NO markdown. NO explanation.

EXAMPLES:

Input:
for i in range(len(arr)):
    for j in range(len(arr)):
        if arr[i] == arr[j]:
            print(arr[i])

Output:
[{"type":"performance","severity":"high","message":"Nested loops result in O(n^2) time complexity","line":2,"suggestion":"Use a set or hashmap to reduce complexity to O(n)"}]

---

Input:
for i in range(10):
    print(i)

Output:
[]

---

Input:
for user in users:
    posts = db.getPosts(user.id)

Output:
[{"type":"performance","severity":"high","message":"Database query inside loop causes N+1 query problem","line":2,"suggestion":"Fetch all posts in a single batch query using user IDs"}]
"""
REFACTOR_PROMPT = """You are RefactorAI, an expert code refactoring engine.

You will receive:
1. The original code
2. A list of approved suggestions

STRICT RULES:
- Apply ONLY the approved suggestions. Do NOT introduce any additional changes.
- Do NOT modify code that is unrelated to the suggestions.
- Do NOT rename variables, functions, or structure unless explicitly instructed.
- Do NOT change formatting unnecessarily.
- Preserve the original logic and behavior unless a suggestion explicitly requires a fix.
- Ensure the final code is syntactically correct and runnable.
- Do NOT remove existing functionality unless explicitly required.
- If multiple suggestions conflict, apply them in a safe and logical way without breaking the code.

SAFETY RULES:
- Do NOT introduce new bugs.
- Do NOT change working code unnecessarily.
- If a suggestion is unclear or unsafe, IGNORE it.
- If no valid suggestions are provided, return the original code unchanged.

OUTPUT FORMAT (STRICT):
- Return ONLY the final refactored code.
- NO explanations
- NO comments about changes
- NO markdown
- NO extra text

GOAL:
Produce the minimal, correct, and safe version of the code after applying the approved suggestions.
"""

# ──────────────────────────────────────────────
# AGENT NODES (with crash isolation)
# ──────────────────────────────────────────────

def bug_hunter(state: CodeReviewState):
    logger.info("[BugHunter] Starting analysis...")
    start = time.time()
    findings = []
    
    # 1. Dynamic execution analysis (Python only)
    if state.get("language") == "python":
        logger.info("[BugHunter] Running dynamic execution analysis...")
        try:
            exec(state["input_code"], {})
        except Exception as e:
            line_no = getattr(e, "lineno", None)
            if line_no is None:
                import traceback
                tb = traceback.extract_tb(e.__traceback__)
                for frame in reversed(tb):
                    if frame.filename == "<string>" or frame.filename == "<module>":
                        line_no = frame.lineno
                        break
            msg = f"Runtime Error ({type(e).__name__}): {str(e)}"
            if line_no:
                msg += f" on line {line_no}"
            
            error_finding = {
                "type": "bug",
                "severity": "high",
                "message": msg,
                "suggestion": "Fix the runtime error and ensure inputs are valid.",
                "source": "BugHunter"
            }
            if line_no:
                error_finding["line"] = line_no
            findings.append(error_finding)
            logger.info(f"[BugHunter] Detected dynamic error: {msg}")

    # 2. LLM Static Analysis
    try:
        llm_findings = invoke_with_retry([
            {"role": "system", "content": BUG_HUNTER_PROMPT},
            {"role": "user", "content": state["input_code"]},
        ], agent_name="BugHunter")
        for f in llm_findings:
            f["source"] = "BugHunter"
        findings.extend(llm_findings)
    except Exception as e:
        logger.error(f"[BugHunter] Static analysis failed after {time.time()-start:.1f}s — {e}")
        
    logger.info(f"[BugHunter] Completed in {time.time()-start:.1f}s — {len(findings)} findings")
    logger.info(f"[BugHunter] Output: {json.dumps(findings)}")
    return {"findings": findings}


def style_guard(state: CodeReviewState):
    logger.info("[StyleGuard] Starting analysis...")
    start = time.time()
    try:
        findings = invoke_with_retry([
            {"role": "system", "content": STYLE_GUARD_PROMPT},
            {"role": "user", "content": state["input_code"]},
        ], agent_name="StyleGuard")
        for f in findings:
            f["source"] = "StyleGuard"
        logger.info(f"[StyleGuard] Completed in {time.time()-start:.1f}s — {len(findings)} findings")
        logger.info(f"[StyleGuard] Output: {json.dumps(findings)}")
        return {"findings": findings}
    except Exception as e:
        logger.error(f"[StyleGuard] Failed after {time.time()-start:.1f}s — {e}")
        return {"findings": []}


def perf_architect(state: CodeReviewState):
    logger.info("[PerfArchitect] Starting analysis...")
    start = time.time()
    try:
        findings = invoke_with_retry([
            {"role": "system", "content": PERF_ARCHITECT_PROMPT},
            {"role": "user", "content": state["input_code"]},
        ], agent_name="PerfArchitect")
        for f in findings:
            f["source"] = "PerfArchitect"
        logger.info(f"[PerfArchitect] Completed in {time.time()-start:.1f}s — {len(findings)} findings")
        logger.info(f"[PerfArchitect] Output: {json.dumps(findings)}")
        return {"findings": findings}
    except Exception as e:
        logger.error(f"[PerfArchitect] Failed after {time.time()-start:.1f}s — {e}")
        return {"findings": []}


def refactor(state: CodeReviewState):
    logger.info("[Refactor] Starting code transformation...")
    start = time.time()
    approved = state.get("approved_suggestions", [])
    original_code = state["input_code"]

    if not approved:
        logger.info("[Refactor] No approved suggestions — returning original code")
        return {"final_code": original_code}

    suggestions_text = json.dumps(approved, indent=2)
    prompt = f"""Original code:
```
{original_code}
```

Approved suggestions to apply:
{suggestions_text}

Rules:
1. Apply ONLY the approved suggestions listed above.
2. Do NOT introduce any additional changes, improvements, or refactoring.
3. Preserve all existing functionality exactly as-is.
4. Return ONLY the modified code."""

    response = llm.invoke([
        {"role": "system", "content": REFACTOR_PROMPT},
        {"role": "user", "content": prompt},
    ])

    refactored = response.content.strip()
    if refactored.startswith("```"):
        lines = refactored.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        refactored = "\n".join(lines)

    logger.info(f"[Refactor] Completed in {time.time()-start:.1f}s")
    return {"final_code": refactored}


# ──────────────────────────────────────────────
# GRAPH CONSTRUCTION
# ──────────────────────────────────────────────

graph_builder = StateGraph(CodeReviewState)

graph_builder.add_node("Router", router)
graph_builder.add_node("BugHunter", bug_hunter)
graph_builder.add_node("StyleGuard", style_guard)
graph_builder.add_node("PerfArchitect", perf_architect)
graph_builder.add_node("Synthesizer", synthesizer)
graph_builder.add_node("Refactor", refactor)

graph_builder.add_edge(START, "Router")
graph_builder.add_conditional_edges("Router", route_by_language, ["BugHunter", "StyleGuard", "PerfArchitect"])
graph_builder.add_edge("BugHunter", "Synthesizer")
graph_builder.add_edge("StyleGuard", "Synthesizer")
graph_builder.add_edge("PerfArchitect", "Synthesizer")
graph_builder.add_edge("Synthesizer", "Refactor")
graph_builder.add_edge("Refactor", END)

graph = graph_builder.compile(
    checkpointer=checkpointer,
    interrupt_before=["Refactor"],
)

logger.info("Graph compiled successfully")

# ──────────────────────────────────────────────
# FASTAPI
# ──────────────────────────────────────────────

app = FastAPI(title="Sentinel-Graph AI Engine", version="1.0.0")


class ReviewRequest(BaseModel):
    input_code: str

    @field_validator("input_code")
    @classmethod
    def code_must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("input_code must not be empty")
        return v


class ApproveRequest(BaseModel):
    thread_id: str
    approved_suggestions: list

    @field_validator("thread_id")
    @classmethod
    def thread_id_must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("thread_id must not be empty")
        return v


class SessionReviewRequest(BaseModel):
    thread_id: str
    session_type: str = "code"
    input_code: str = ""
    messages: list = []

    @field_validator("thread_id")
    @classmethod
    def thread_id_must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("thread_id must not be empty")
        return v


@app.get("/health")
def health():
    return {"status": "ok", "model": OLLAMA_MODEL}


@app.post("/review")
def review(request: ReviewRequest):
    request_id = str(uuid.uuid4())[:8]
    logger.info(f"[{request_id}] POST /review — {len(request.input_code)} chars")
    logger.info(f"[{request_id}] Input Code:\n{request.input_code}")
    start = time.time()

    try:
        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}
        state = {
            "input_code": request.input_code,
            "language": "",
            "findings": [],
            "aggregated_report": [],
            "approved_suggestions": [],
            "final_code": "",
        }
        graph.invoke(state, config=config)
        snapshot = graph.get_state(config)
        current_state = snapshot.values

        findings = current_state.get("findings", [])
        report = current_state.get("aggregated_report", [])

        logger.info(
            f"[{request_id}] Review complete in {time.time()-start:.1f}s — "
            f"{len(findings)} findings, {len(report)} in report"
        )
        logger.info(f"[{request_id}] Final Report: {json.dumps(report)}")

        return {
            "status": "PENDING_APPROVAL",
            "thread_id": thread_id,
            "input_code": request.input_code,
            "findings": findings,
            "aggregated_report": report,
        }
    except StructuredOutputError as e:
        logger.error(f"[{request_id}] Structured output error: {e}")
        return JSONResponse(status_code=502, content={"error": str(e)})
    except Exception as e:
        logger.error(f"[{request_id}] Unexpected error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/review/approve")
def approve(request: ApproveRequest):
    request_id = str(uuid.uuid4())[:8]
    logger.info(
        f"[{request_id}] POST /review/approve — thread={request.thread_id[:8]}... "
        f"{len(request.approved_suggestions)} suggestions"
    )
    start = time.time()

    try:
        config = {"configurable": {"thread_id": request.thread_id}}
        snapshot = graph.get_state(config)

        if not snapshot or not snapshot.values:
            logger.warning(f"[{request_id}] No pending review found")
            return JSONResponse(
                status_code=404,
                content={"error": "No pending review found for this thread_id"},
            )

        graph.update_state(
            config,
            {"approved_suggestions": request.approved_suggestions},
        )

        result = graph.invoke(None, config=config)

        logger.info(f"[{request_id}] Approval complete in {time.time()-start:.1f}s")

        return {
            "status": "COMPLETED",
            "input_code": result.get("input_code", ""),
            "final_code": result.get("final_code", ""),
        }
    except Exception as e:
        logger.error(f"[{request_id}] Unexpected error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/review/session")
def review_session(request: SessionReviewRequest):
    request_id = str(uuid.uuid4())[:8]
    session_id = request.thread_id # Map frontend thread_id conceptually to session_id 
    logger.info(f"[{request_id}] POST /review/session — session={session_id[:8]}")
    start = time.time()
    
    try:
        config = {"configurable": {"thread_id": session_id}}
        
        # Load previous state safely
        prior_state = graph.get_state(config)
        
        new_message = {
            "type": "input", 
            "content": request.input_code, 
            "session_type": request.session_type
        }
        
        state_input = {
            "thread_id": session_id,
            "session_type": request.session_type,
            "input_code": request.input_code,
            "messages": [new_message] # Reducer safely appends this
        }
        
        graph.invoke(state_input, config=config)
        current_state = graph.get_state(config).values

        findings = current_state.get("findings", [])
        report = current_state.get("aggregated_report", [])

        logger.info(
            f"[{request_id}] Session Review complete in {time.time()-start:.1f}s — "
            f"{len(findings)} findings, {len(report)} in report"
        )
        return {
            "status": "COMPLETED",
            "session_id": session_id,
            "session_type": current_state.get("session_type", request.session_type),
            "input_code": current_state.get("input_code", request.input_code),
            "findings": findings,
            "aggregated_report": report,
            "messages": current_state.get("messages", [])
        }
    except StructuredOutputError as e:
        logger.error(f"[{request_id}] Parsing failed: {str(e)}")
        return JSONResponse(status_code=502, content={"error": "LLM output parsing failed after retries."})
    except Exception as e:
        logger.error(f"[{request_id}] Crash: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})
