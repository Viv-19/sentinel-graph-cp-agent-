import json
import logging
import operator
import os
import re
import string
import subprocess
import sys
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
FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("FALLBACK_MODELS", "qwen2.5-coder:1.5b,gemma4:e2b").split(",")
    if m.strip()
]

logger.info(f"Using model: {OLLAMA_MODEL} (temp={OLLAMA_TEMPERATURE})")
logger.info(f"Fallback models: {FALLBACK_MODELS}")

# ── Model pool with lazy instantiation ──
_llm_instances: dict[str, ChatOllama] = {}


def get_llm(model_name: str | None = None) -> ChatOllama:
    """Get or create a ChatOllama instance for the given model."""
    name = model_name or OLLAMA_MODEL
    if name not in _llm_instances:
        _llm_instances[name] = ChatOllama(
            model=name,
            temperature=OLLAMA_TEMPERATURE,
            num_predict=2048,
            timeout=120,
        )
        logger.info(f"Created LLM instance for model: {name}")
    return _llm_instances[name]


def _is_connection_error(error: Exception) -> bool:
    """Check if error is a connection/server issue (vs a parse error)."""
    err_str = f"{type(error).__name__}: {error}".lower()
    return any(kw in err_str for kw in [
        "connecterror", "connectionerror", "refused", "disconnected",
        "remoteprotocol", "timeout", "winerror", "server disconnected",
        "connection reset", "broken pipe", "10061",
    ])


def _get_model_chain() -> list[str]:
    """Return the ordered list of models to try (primary + fallbacks)."""
    return [OLLAMA_MODEL] + FALLBACK_MODELS


# Keep a default reference for backward compat
llm = get_llm(OLLAMA_MODEL)

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}

review_checkpointer = MemorySaver()
problem_checkpointer = MemorySaver()


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
    problem_analysis: dict
    constraint_insights: dict
    expected_time_complexity: str
    expected_space_complexity: str
    complexity_reasoning: str
    detected_pattern: str
    pattern_confidence: float
    strategy_plan: dict
    test_case_validation: dict
    user_decision: str
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
    models = _get_model_chain()
    last_error = None

    for model_idx, model_name in enumerate(models):
        current_llm = get_llm(model_name)
        connection_failed = False

        for attempt in range(retries):
            try:
                response = current_llm.invoke(messages)
                logger.debug(f"[{agent_name}] Attempt {attempt+1} raw output: {response.content[:300]}")
                findings = parse_findings(response.content)
                if model_idx > 0:
                    logger.info(f"[{agent_name}] ✓ Succeeded with fallback model: {model_name}")
                logger.info(f"[{agent_name}] Attempt {attempt+1} — parsed {len(findings)} findings")
                return findings
            except StructuredOutputError as e:
                last_error = e
                logger.warning(f"[{agent_name}] Attempt {attempt+1}/{retries} — parse failed")
            except Exception as e:
                last_error = e
                logger.error(f"[{agent_name}] Attempt {attempt+1}/{retries} — LLM error ({model_name}): {type(e).__name__}: {e}")
                if _is_connection_error(e):
                    connection_failed = True
                    break  # Don't retry same model if connection is dead

        # If connection failed, try next fallback model
        if connection_failed and model_idx < len(models) - 1:
            logger.warning(f"[{agent_name}] ↻ Switching from {model_name} → {models[model_idx + 1]}")
            continue
        # Parse errors don't warrant a model switch
        if not connection_failed:
            break

    raise StructuredOutputError(
        f"STRUCTURED_OUTPUT_ERROR: All retries exhausted across {len(models)} model(s). Last error: {last_error}"
    )

def parse_dict(raw: str) -> dict:
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise StructuredOutputError("STRUCTURED_OUTPUT_ERROR: No JSON object found")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        raise StructuredOutputError("STRUCTURED_OUTPUT_ERROR: Invalid JSON")
    if not isinstance(parsed, dict):
        raise StructuredOutputError("STRUCTURED_OUTPUT_ERROR: Expected JSON object")
    return parsed

def invoke_dict_with_retry(messages: list, agent_name: str, max_retries: int = None) -> dict:
    retries = max_retries if max_retries is not None else MAX_RETRIES
    models = _get_model_chain()
    last_error = None

    for model_idx, model_name in enumerate(models):
        current_llm = get_llm(model_name)
        connection_failed = False

        for attempt in range(retries):
            try:
                response = current_llm.invoke(messages)
                logger.debug(f"[{agent_name}] Attempt {attempt+1} raw output: {response.content[:300]}")
                result = parse_dict(response.content)
                if model_idx > 0:
                    logger.info(f"[{agent_name}] ✓ Succeeded with fallback model: {model_name}")
                logger.info(f"[{agent_name}] Attempt {attempt+1} — parsed dict successfully")
                return result
            except StructuredOutputError as e:
                last_error = e
                logger.warning(f"[{agent_name}] Attempt {attempt+1}/{retries} — parse failed")
            except Exception as e:
                last_error = e
                logger.error(f"[{agent_name}] Attempt {attempt+1}/{retries} — LLM error ({model_name}): {type(e).__name__}: {e}")
                if _is_connection_error(e):
                    connection_failed = True
                    break

        if connection_failed and model_idx < len(models) - 1:
            logger.warning(f"[{agent_name}] ↻ Switching from {model_name} → {models[model_idx + 1]}")
            continue
        if not connection_failed:
            break

    raise StructuredOutputError(
        f"STRUCTURED_OUTPUT_ERROR: All retries exhausted across {len(models)} model(s). Last error: {last_error}"
    )

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

def problem_analyzer(state: CodeReviewState):
    logger.info("[ProblemAnalyzer] Starting analysis...")
    start = time.time()
    problem = state.get("problem_description", "").strip()
    
    default_output = {
        "constraints": {},
        "input_format": "",
        "output_format": "",
        "problem_type": "unknown"
    }
    
    if not problem:
        logger.warning("[ProblemAnalyzer] Empty problem description")
        return {"problem_analysis": default_output}

    prompt = f"""You are an Expert Competitive Programming Assistant.
Analyze the following problem description. Extract structured information. 
DO NOT solve the problem. DO NOT write code.

Return ONLY a valid JSON object matching this exact structure:
{{
  "constraints": {{"n": "<= 10^5", "time_limit": "1.0s"}},
  "input_format": "Number of test cases, followed by...",
  "output_format": "Single integer per test case...",
  "problem_type": "array" // Choose ONE from: array, string, graph, dp, greedy, math, binary search, implementation, mixed
}}

Instructions:
- constraints: extract explicit/hidden limits. If missing constraints, infer standard limits.
- input_format: indicate single or multi-testcase setups.
- output_format: clearly state what to output per case.
- problem_type: map to best fitting category.

Problem Description:
{problem}
"""
    try:
        analysis = invoke_dict_with_retry([
            {"role": "system", "content": prompt}
        ], agent_name="ProblemAnalyzer")
        
        result = {
            "constraints": analysis.get("constraints", {}),
            "input_format": str(analysis.get("input_format", "")),
            "output_format": str(analysis.get("output_format", "")),
            "problem_type": str(analysis.get("problem_type", "unknown"))
        }
        logger.info(f"[ProblemAnalyzer] Extraction completed in {time.time()-start:.1f}s")
        return {"problem_analysis": result}
    except Exception as e:
        logger.error(f"[ProblemAnalyzer] Parsing failed: {str(e)}")
        return {"problem_analysis": default_output}

def constraint_analyzer(state: CodeReviewState):
    logger.info("[ConstraintAnalyzer] Starting...")
    start = time.time()
    problem_analysis = state.get("problem_analysis", {})
    constraints = problem_analysis.get("constraints", {})
    problem_desc = state.get("problem_description", "").strip()

    default_output = {
        "scale": "unknown",
        "test_case_count": "unknown",
        "tight_constraints": [],
        "infeasible_complexities": [],
        "special_notes": []
    }
    
    if not constraints and not problem_desc:
        return {"constraint_insights": default_output}
        
    prompt = f"""You are a Competitive Programming Expert specializing in constraints.
Analyze the following constraints and problem description. Extract structural insights.
DO NOT suggest an algorithm. DO NOT write code.

Return ONLY a valid JSON object matching this exact structure:
{{
  "scale": "small",
  "test_case_count": "single",
  "tight_constraints": [""],
  "infeasible_complexities": [""],
  "special_notes": [""]
}}

Constraints: {json.dumps(constraints)}
Problem Description: {problem_desc[:500]}
"""
    try:
        res = invoke_dict_with_retry([{"role": "system", "content": prompt}], agent_name="ConstraintAnalyzer")
        logger.info(f"[ConstraintAnalyzer] Completed in {time.time()-start:.1f}s")
        return {"constraint_insights": res}
    except Exception as e:
        logger.error(f"[ConstraintAnalyzer] Failed: {e}")
        return {"constraint_insights": default_output}


def complexity_estimator(state: CodeReviewState):
    logger.info("[ComplexityEstimator] Starting...")
    start = time.time()
    constraint_insights = state.get("constraint_insights", {})
    problem_desc = state.get("problem_description", "").strip()
    
    default_output = {
        "expected_time_complexity": "unknown",
        "expected_space_complexity": "unknown",
        "complexity_reasoning": "Failed to estimate complexity"
    }
    if not constraint_insights and not problem_desc:
        return default_output

    prompt = f"""You are a Competitive Programming Expert specializing in Time and Space Complexity.
Infer the REQUIRED complexity based on constraints.
DO NOT suggest a specific strategy. DO NOT write code.

Return ONLY a valid JSON object matching this exact structure:
{{
  "expected_time_complexity": "O(N log N)",
  "expected_space_complexity": "O(N)",
  "complexity_reasoning": "Explanation here..."
}}

Constraints Insights: {json.dumps(constraint_insights)}
"""
    try:
        res = invoke_dict_with_retry([{"role": "system", "content": prompt}], agent_name="ComplexityEstimator")
        logger.info(f"[ComplexityEstimator] Completed in {time.time()-start:.1f}s")
        return {
            "expected_time_complexity": str(res.get("expected_time_complexity", "unknown")),
            "expected_space_complexity": str(res.get("expected_space_complexity", "unknown")),
            "complexity_reasoning": str(res.get("complexity_reasoning", "unknown"))
        }
    except Exception as e:
        logger.error(f"[ComplexityEstimator] Failed: {e}")
        return default_output


def pattern_recognizer(state: CodeReviewState):
    logger.info("[PatternRecognizer] Starting...")
    start = time.time()
    problem_desc = state.get("problem_description", "").strip()
    problem_type = state.get("problem_analysis", {}).get("problem_type", "unknown")
    
    default_output = {
        "detected_pattern": "unknown",
        "pattern_confidence": 0.0
    }
    if not problem_desc:
        return default_output

    prompt = f"""You are a Competitive Programming Expert specializing in pattern recognition.
Detect the underlying algorithm pattern from the problem description.
DO NOT generate a full solution. DO NOT write code.

Return ONLY a valid JSON object matching this exact structure:
{{
  "detected_pattern": "sliding window",
  "pattern_confidence": 0.85
}}

Problem Type: {problem_type}
Problem Description: {problem_desc[:1000]}
"""
    try:
        res = invoke_dict_with_retry([{"role": "system", "content": prompt}], agent_name="PatternRecognizer")
        logger.info(f"[PatternRecognizer] Completed in {time.time()-start:.1f}s")
        return {
            "detected_pattern": str(res.get("detected_pattern", "unknown")),
            "pattern_confidence": float(res.get("pattern_confidence", 0.0))
        }
    except Exception as e:
        logger.error(f"[PatternRecognizer] Failed: {e}")
        return default_output


def strategy_planner(state: CodeReviewState):
    logger.info("[StrategyPlanner] Starting...")
    start = time.time()
    
    constraint_insights = state.get("constraint_insights", {})
    expected_tc = state.get("expected_time_complexity", "unknown")
    expected_sc = state.get("expected_space_complexity", "unknown")
    pattern = state.get("detected_pattern", "unknown")
    
    default_output = {
        "strategy": "Failed to build strategy",
        "confidence": 0.0,
        "justification": "Dependency nodes failed.",
        "approach_steps": [],
        "alternative_strategies": [],
        "edge_case_plan": []
    }

    prompt = f"""You are a Lead Competitive Programmer constructing an overarching strategy.
Combine outputs from parallel reasoning nodes into a cohesive, correct, constraints-aware strategy without writing code.

REQUIREMENTS:
1. Respect constraints: NEVER suggest infeasible complexity.
2. Align with expected complexity: Must hit Time/Space targets.
3. Use pattern as hint: Prefer detected pattern.
4. Validate internally: Ensure steps logically solve the problem.

Return ONLY a valid JSON object matching this exact structure:
{{
  "strategy": "Use a hash map prefix sum...",
  "confidence": 0.95,
  "justification": "O(N) fits the 1.0s limit, scaling easily across 10^5 inputs.",
  "approach_steps": ["1. Initialize prefix map", "2. Iterate array", "3. Return count"],
  "alternative_strategies": ["Binary search on answer O(N log(MAX))"],
  "edge_case_plan": ["All elements zero", "Single element array"]
}}

Constraint Insights: {json.dumps(constraint_insights)}
Expected TC: {expected_tc}
Expected SC: {expected_sc}
Detected Pattern: {pattern}
"""
    try:
        res = invoke_dict_with_retry([{"role": "system", "content": prompt}], agent_name="StrategyPlanner")
        confidence = float(res.get("confidence", 0.0))
        if confidence < 0.5:
             logger.warning(f"[StrategyPlanner] Low confidence strategy generated: {confidence}")
        logger.info(f"[StrategyPlanner] Merge completed in {time.time()-start:.1f}s")
        return {
            "strategy_plan": {
                 "strategy": str(res.get("strategy", "unknown")),
                 "confidence": float(confidence),
                 "justification": str(res.get("justification", "unknown")),
                 "approach_steps": res.get("approach_steps", []),
                 "alternative_strategies": res.get("alternative_strategies", []),
                 "edge_case_plan": res.get("edge_case_plan", [])
            }
        }
    except Exception as e:
        logger.error(f"[StrategyPlanner] Failed: {e}")
        return {"strategy_plan": default_output}

def test_case_validator(state: CodeReviewState):
    logger.info("[TestCaseValidator] Starting simulation...")
    start = time.time()
    
    problem_desc = state.get("problem_description", "").strip()
    strategy_plan = state.get("strategy_plan", {})
    expected_tc = state.get("expected_time_complexity", "unknown")
    constraints = state.get("problem_analysis", {}).get("constraints", {})

    default_output = {
        "passes_sample_tests": False,
        "validation_mode": "uncertain",
        "predicted_output": "",
        "expected_output": "",
        "failed_cases": [],
        "edge_case_analysis": [],
        "confidence_score": 0.0
    }

    if not problem_desc or not strategy_plan:
        return {"test_case_validation": default_output}

    prompt = f"""You are a Competitive Programming Judge and Validator.
Simulate the planned strategy over sample test cases extracted from the problem.
DO NOT execute code. DO NOT assume the strategy is correct blindly. DO NOT generate code.

REQUIREMENTS:
1. Extract Sample Input and Output from Problem.
2. Simulate the strategy step-by-step logically.
3. Compare simulated predicted output vs expected sample output.
4. If multiple valid outputs exist and order is not fixed, set "validation_mode" to "skipped_non_deterministic" and "passes_sample_tests" to true.
5. Generate an edge case analysis list (e.g. empty input, max limits, duplicates).
6. Calculate confidence_score (0.0 - 1.0).

Return ONLY a valid JSON object matching this exact structure:
{{
  "passes_sample_tests": true,
  "validation_mode": "strict",
  "predicted_output": "...",
  "expected_output": "...",
  "failed_cases": ["Case 2: predicted 5, expected 6"],
  "edge_case_analysis": ["Empty array yields 0", "N at maximum triggers overflow if not 64-bit"],
  "confidence_score": 0.95
}}

Problem Description: {problem_desc[:1500]}
Strategy Plan: {json.dumps(strategy_plan)}
Constraints: {json.dumps(constraints)}
Expected TC: {expected_tc}
"""
    try:
        res = invoke_dict_with_retry([{"role": "system", "content": prompt}], agent_name="TestCaseValidator")
        logger.info(f"[TestCaseValidator] Simulation completed in {time.time()-start:.1f}s")
        return {
            "test_case_validation": {
                "passes_sample_tests": bool(res.get("passes_sample_tests", False)),
                "validation_mode": str(res.get("validation_mode", "uncertain")),
                "predicted_output": str(res.get("predicted_output", "")),
                "expected_output": str(res.get("expected_output", "")),
                "failed_cases": res.get("failed_cases", []),
                "edge_case_analysis": res.get("edge_case_analysis", []),
                "confidence_score": float(res.get("confidence_score", 0.0))
            }
        }
    except Exception as e:
        logger.error(f"[TestCaseValidator] Failed: {e}")
        return {"test_case_validation": default_output}

def hitl_decision_node(state: CodeReviewState):
    logger.info("[HITLDecisionNode] Execution resumed from user decision")
    return {"user_decision": state.get("user_decision", "pending")}

def return_hints_node(state: CodeReviewState):
    logger.info("[ReturnHintsNode] User opted for hints.")
    return {} # Halts further code generation

def code_generator_node(state: CodeReviewState):
    logger.info("[CodeGeneratorNode] Starting code generation based on Strategy Plan...")
    start = time.time()
    
    problem_desc = state.get("problem_description", "")
    strategy_plan = state.get("strategy_plan", {})
    expected_tc = state.get("expected_time_complexity", "unknown")
    expected_sc = state.get("expected_space_complexity", "unknown")
    edge_case_plan = strategy_plan.get("edge_case_plan", [])
    
    prompt = f"""You are an Expert Competitive Programmer. Write production-ready CP code.

YOUR TASK: Implement the Exact Strategy Plan provided. 
- Use Python.
- Provide fast input wrappers if necessary.
- Follow the expected time and space complexities STRICTLY.
- Handle ALL identified Edge Cases smoothly without runtime logic faults.
- Use a clean `solve()` layout. Do not over-engineer with abstracted classes unless naturally required.
- Do NOT generate pseudo-code. No partial answers.

Return ONLY a valid JSON object matching this exact structure:
{{
  "generated_code": "def solve():\\n    ...",
  "language": "python",
  "notes": ["Implemented prefix array for strict O(N) requirement.", "Added constraint check for empty edge case."]
}}

Problem Description: {problem_desc[:1500]}
Strategy Given: {json.dumps(strategy_plan)}
Expected Complexities: TC: {expected_tc} | SC: {expected_sc}
Edge Cases to Prevent: {json.dumps(edge_case_plan)}
"""
    try:
         res = invoke_dict_with_retry([{"role": "system", "content": prompt}], agent_name="CodeGeneratorNode")
         gen_code = res.get("generated_code", "")
         if not gen_code:
             raise ValueError("Empty generated code returned")
         logger.info(f"[CodeGeneratorNode] Generation completed in {time.time()-start:.1f}s")
         return {
             "input_code": gen_code,
             "language": str(res.get("language", "python")),
             "messages": [("ai", f"Code Generated: {'. '.join(res.get('notes', []))}")]
         }
    except Exception as e:
         logger.error(f"[CodeGeneratorNode] Generation failed: {e}")
         return {
             "input_code": "",
             "messages": [("ai", "Warning: Code Generation failed. Review Node Logs.")]
         }

def wait_for_user_code_node(state: CodeReviewState):
    logger.info("[WaitForUserCodeNode] User opted to write own code.")
    return {}

def route_decision(state: CodeReviewState):
    decision = state.get("user_decision", "pending")
    if decision == "hints":
        return "RETURN_HINTS"
    if decision == "generate":
        return "CodeGeneratorNode"
    if decision == "manual":
        return "WAIT_FOR_USER_CODE"
    return END

def bug_hunter(state: CodeReviewState):
    logger.info("[BugHunter] Starting analysis...")
    start = time.time()
    findings = []
    
    # 1. Sandboxed syntax check (Python only — no exec for security)
    if state.get("language") == "python":
        logger.info("[BugHunter] Running sandboxed syntax/runtime check...")
        try:
            code = state.get("input_code", "")
            # Phase 1: compile-time syntax check (safe, no execution)
            compile(code, "<user_code>", "exec")
            # Phase 2: sandboxed subprocess execution with timeout
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                # Try to extract line number from traceback
                line_no = None
                for line in stderr.split("\n"):
                    m = re.search(r'line (\d+)', line)
                    if m:
                        line_no = int(m.group(1))
                msg = f"Runtime Error: {stderr.split(chr(10))[-1]}" if stderr else "Unknown runtime error"
                error_finding = {
                    "type": "bug", "severity": "high",
                    "message": msg,
                    "suggestion": "Fix the runtime error and ensure inputs are valid.",
                    "source": "BugHunter"
                }
                if line_no:
                    error_finding["line"] = line_no
                findings.append(error_finding)
                logger.info(f"[BugHunter] Detected sandboxed error: {msg}")
        except SyntaxError as e:
            findings.append({
                "type": "bug", "severity": "high",
                "message": f"SyntaxError: {e.msg} on line {e.lineno}",
                "line": e.lineno,
                "suggestion": "Fix the syntax error.",
                "source": "BugHunter"
            })
        except subprocess.TimeoutExpired:
            findings.append({
                "type": "bug", "severity": "high",
                "message": "Code execution timed out (possible infinite loop)",
                "suggestion": "Check for infinite loops or very long-running operations.",
                "source": "BugHunter"
            })
        except Exception as e:
            logger.warning(f"[BugHunter] Sandboxed check failed: {e}")

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
    original_code = state.get("input_code", "")

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

    messages = [
        {"role": "system", "content": REFACTOR_PROMPT},
        {"role": "user", "content": prompt},
    ]

    # Try primary model, then fallbacks on connection errors
    models = _get_model_chain()
    last_error = None
    for model_name in models:
        try:
            current_llm = get_llm(model_name)
            response = current_llm.invoke(messages)
            break
        except Exception as e:
            last_error = e
            if _is_connection_error(e):
                logger.warning(f"[Refactor] ↻ Connection failed with {model_name}, trying fallback...")
                continue
            raise
    else:
        raise last_error  # type: ignore[misc]

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
    checkpointer=review_checkpointer,
    interrupt_before=["Refactor"],
)

problem_graph_builder = StateGraph(CodeReviewState)
problem_graph_builder.add_node("ProblemAnalyzer", problem_analyzer)
problem_graph_builder.add_node("ConstraintAnalyzer", constraint_analyzer)
problem_graph_builder.add_node("ComplexityEstimator", complexity_estimator)
problem_graph_builder.add_node("PatternRecognizer", pattern_recognizer)
problem_graph_builder.add_node("StrategyPlanner", strategy_planner)
problem_graph_builder.add_node("TestCaseValidator", test_case_validator)
problem_graph_builder.add_node("HITLDecisionNode", hitl_decision_node)
problem_graph_builder.add_node("RETURN_HINTS", return_hints_node)
problem_graph_builder.add_node("CodeGeneratorNode", code_generator_node)
problem_graph_builder.add_node("WAIT_FOR_USER_CODE", wait_for_user_code_node)

problem_graph_builder.add_edge(START, "ProblemAnalyzer")
problem_graph_builder.add_edge("ProblemAnalyzer", "ConstraintAnalyzer")
problem_graph_builder.add_edge("ProblemAnalyzer", "ComplexityEstimator")
problem_graph_builder.add_edge("ProblemAnalyzer", "PatternRecognizer")

problem_graph_builder.add_edge("ConstraintAnalyzer", "StrategyPlanner")
problem_graph_builder.add_edge("ComplexityEstimator", "StrategyPlanner")
problem_graph_builder.add_edge("PatternRecognizer", "StrategyPlanner")

problem_graph_builder.add_edge("StrategyPlanner", "TestCaseValidator")
problem_graph_builder.add_edge("TestCaseValidator", "HITLDecisionNode")
problem_graph_builder.add_conditional_edges(
    "HITLDecisionNode",
    route_decision,
    ["RETURN_HINTS", "CodeGeneratorNode", "WAIT_FOR_USER_CODE", END]
)
problem_graph_builder.add_edge("RETURN_HINTS", END)
problem_graph_builder.add_edge("CodeGeneratorNode", END)
problem_graph_builder.add_edge("WAIT_FOR_USER_CODE", END)

problem_graph = problem_graph_builder.compile(
    checkpointer=problem_checkpointer,
    interrupt_before=["HITLDecisionNode"]
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
        if len(v) > 50000:
            raise ValueError("input_code exceeds 50000 character limit")
        return v


class AnalyzeProblemRequest(BaseModel):
    thread_id: str
    problem_description: str

    @field_validator("thread_id")
    @classmethod
    def thread_id_must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("thread_id must not be empty")
        return v

    @field_validator("problem_description")
    @classmethod
    def problem_must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("problem_description must not be empty")
        if len(v) > 10000:
            raise ValueError("problem_description exceeds 10000 character limit")
        return v

class VerifyDecisionRequest(BaseModel):
    session_id: str
    decision: str

    @field_validator("decision")
    @classmethod
    def valid_decision(cls, v: str) -> str:
        if v not in ["hints", "generate", "manual"]:
            raise ValueError("decision must be 'hints', 'generate', or 'manual'")
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


@app.post("/problem/analyze")
def analyze_problem(req: AnalyzeProblemRequest):
    logger.info(f"Analyzing problem for {req.thread_id}")
    try:
        config = {"configurable": {"thread_id": req.thread_id}}
        result = problem_graph.invoke({
            "thread_id": req.thread_id,
            "session_type": "problem",
            "problem_description": req.problem_description
        }, config)
        
        current_state = problem_graph.get_state(config)
        status = "PENDING_DECISION" if "HITLDecisionNode" in current_state.next else "COMPLETED"

        return {
            "status": status,
            "problem_analysis": result.get("problem_analysis", {}),
            "constraint_insights": result.get("constraint_insights", {}),
            "expected_time_complexity": result.get("expected_time_complexity", ""),
            "expected_space_complexity": result.get("expected_space_complexity", ""),
            "complexity_reasoning": result.get("complexity_reasoning", ""),
            "detected_pattern": result.get("detected_pattern", ""),
            "pattern_confidence": result.get("pattern_confidence", 0.0),
            "strategy_plan": result.get("strategy_plan", {}),
            "test_case_validation": result.get("test_case_validation", {})
        }
    except Exception as e:
        logger.error(f"Error in analyze_problem: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/problem/decision")
def submit_decision(req: VerifyDecisionRequest):
    logger.info(f"Received logic decision '{req.decision}' for session {req.session_id}")
    try:
        config = {"configurable": {"thread_id": req.session_id}}
        current_state = problem_graph.get_state(config)
        
        if "HITLDecisionNode" not in current_state.next:
            return JSONResponse(status_code=400, content={"error": "Not awaiting decision"})

        problem_graph.update_state(config, {"user_decision": req.decision})
        result = problem_graph.invoke(None, config=config)

        return {
            "status": "RESUMED",
            "input_code": result.get("input_code", ""),
            "language": result.get("language", "python")
        }
    except Exception as e:
        logger.error(f"Error mapping decision: {e}")
        return JSONResponse(status_code=500, content={"error": "System failure"})


@app.post("/review")
def review(request: ReviewRequest):
    request_id = str(uuid.uuid4())[:8]
    logger.info(f"[{request_id}] POST /review — {len(request.input_code)} chars")
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
    session_id = request.thread_id
    logger.info(f"[{request_id}] POST /review/session — session={session_id[:8]}")
    start = time.time()
    
    try:
        # Create a unique review thread_id for this review run
        review_thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": review_thread_id}}
        
        state_input = {
            "thread_id": session_id,
            "session_type": request.session_type,
            "input_code": request.input_code,
            "messages": [{"type": "input", "content": request.input_code, "session_type": request.session_type}]
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
            "thread_id": review_thread_id,
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
