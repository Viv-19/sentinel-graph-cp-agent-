import { Router } from 'express';
import { z } from 'zod';
import { CodeReviewService } from '../../core/services/CodeReviewService';
import { PythonGraphAdapter } from '../adapters/PythonGraphAdapter';

const router = Router();
const pythonAdapter = new PythonGraphAdapter();
const coreService = new CodeReviewService(pythonAdapter);

const ReviewRequestSchema = z.object({
  input_code: z.string().min(1, 'input_code must not be empty'),
});

const ApproveRequestSchema = z.object({
  thread_id: z.string().min(1, 'thread_id is required'),
  approved_suggestions: z.array(z.any()).default([]),
});

router.get('/health', (req, res) => {
  res.json({ status: 'ok' });
});

router.get('/', (req, res) => {
  res.render('index');
});

router.post('/review', async (req, res) => {
  try {
    const parsed = ReviewRequestSchema.safeParse(req.body);
    if (!parsed.success) {
      res.status(400).json({
        error: parsed.error.errors.map((e) => e.message).join(', '),
      });
      return;
    }

    const { input_code } = parsed.data;
    const result = await coreService.reviewCode(input_code);
    res.json(result);
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Internal Server Error';
    console.error(`[POST /review] Error: ${message}`);
    res.status(500).json({ error: message });
  }
});

router.post('/review/approve', async (req, res) => {
  try {
    const parsed = ApproveRequestSchema.safeParse(req.body);
    if (!parsed.success) {
      res.status(400).json({
        error: parsed.error.errors.map((e) => e.message).join(', '),
      });
      return;
    }

    const { thread_id, approved_suggestions } = parsed.data;

    const result = await coreService.approveReview(
      thread_id,
      approved_suggestions,
    );
    res.json(result);
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Internal Server Error';
    console.error(`[POST /review/approve] Error: ${message}`);
    res.status(500).json({ error: message });
  }
});

// --- SESSION BASED SYSTEM (PHASE 1) ---

export interface Session {
  id: string;
  title: string;
  problem_description?: string;
  problem_analysis?: any;
  constraint_insights?: any;
  expected_time_complexity?: string;
  expected_space_complexity?: string;
  complexity_reasoning?: string;
  detected_pattern?: string;
  pattern_confidence?: number;
  strategy_plan?: any;
  test_case_validation?: any;
  analysis_status?: string;
  review_thread_id?: string;
  createdAt: string;
  updatedAt: string;
}

export const sessions: Session[] = [];
export const sessionContexts: Map<string, any[]> = new Map();

router.post('/sessions', (req, res) => {
  const newSession: Session = {
    id: require('crypto').randomUUID(),
    title: req.body.title || 'New Session',
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
  sessions.push(newSession);
  sessionContexts.set(newSession.id, []);
  res.status(201).json(newSession);
});

router.get('/sessions', (req, res) => {
  res.json(sessions);
});

router.get('/sessions/:id', (req, res) => {
  const context = sessionContexts.get(req.params.id);
  const session = sessions.find((s) => s.id === req.params.id);
  if (!context || !session) {
    res.status(404).json({ error: 'Session not found' });
    return;
  }
  res.json({ 
    id: req.params.id, 
    problem_description: session.problem_description || '', 
    problem_analysis: session.problem_analysis || null, 
    constraint_insights: session.constraint_insights || null,
    expected_time_complexity: session.expected_time_complexity || null,
    expected_space_complexity: session.expected_space_complexity || null,
    complexity_reasoning: session.complexity_reasoning || null,
    detected_pattern: session.detected_pattern || null,
    pattern_confidence: session.pattern_confidence || null,
    strategy_plan: session.strategy_plan || null,
    test_case_validation: session.test_case_validation || null,
    analysis_status: session.analysis_status || 'IDLE',
    context 
  });
});

const sessionLocks: Record<string, boolean> = {};

router.post('/sessions/:id/input', async (req, res) => {
  const session_id = req.params.id;
  const { type, content, input_code } = req.body;

  if (!input_code || typeof input_code !== 'string' || !input_code.trim()) {
    res.status(400).json({ error: 'input_code must not be empty' });
    return;
  }

  if (!sessionContexts.has(session_id)) {
    res.status(404).json({ error: 'Session not found' });
    return;
  }

  if (sessionLocks[session_id]) {
    res.status(429).json({ error: 'Session is currently processing a request' });
    return;
  }

  sessionLocks[session_id] = true;

  try {
    // Fetch session context & Append new input
    const context = sessionContexts.get(session_id)!;
    context.push({ type, content, timestamp: new Date().toISOString() });

    const sessionIndex = sessions.findIndex((s) => s.id === session_id);
    if (sessionIndex !== -1) {
      sessions[sessionIndex].updatedAt = new Date().toISOString();
    }

    const pythonUrl = process.env.PYTHON_SERVICE_URL || 'http://localhost:8000';
    console.log(`Forwarding context to Python service for session: ${session_id}`);
    
    const response = await fetch(`${pythonUrl}/review/session`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        thread_id: session_id,
        session_type: type || 'code',
        input_code: input_code,
      }),
    });

    if (!response.ok) {
      const errData: any = await response.json().catch(() => ({}));
      throw new Error(errData.error || `Python service responded with status: ${response.status}`);
    }

    const data: any = await response.json();
    
    // Store the review thread_id from Python so we can use it for approval
    if (data.thread_id) {
      const sess = sessions.find(s => s.id === session_id);
      if (sess) sess.review_thread_id = data.thread_id;
    }
    
    res.json(data);
  } catch (error) {
    console.error(`[POST /sessions/${session_id}/input] Error:`, error);
    res.status(500).json({ error: error instanceof Error ? error.message : 'Processing failed' });
  } finally {
    sessionLocks[session_id] = false;
  }
});

const ProblemSchema = z.object({
  problem_description: z.string().min(1, 'problem_description cannot be empty'),
});

router.post('/sessions/:id/problem', (req, res) => {
  const session_id = req.params.id;
  const parsed = ProblemSchema.safeParse(req.body);
  
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.errors.map(e => e.message).join(', ') });
    return;
  }

  const session = sessions.find(s => s.id === session_id);
  if (!session) {
    res.status(404).json({ error: 'Session not found' });
    return;
  }

  session.problem_description = parsed.data.problem_description;
  session.updatedAt = new Date().toISOString();
  
  res.json({ status: 'OK' });
});

router.post('/sessions/:id/problem/analyze', async (req, res) => {
  const session_id = req.params.id;
  
  const session = sessions.find(s => s.id === session_id);
  if (!session) {
    res.status(404).json({ error: 'Session not found' });
    return;
  }

  if (sessionLocks[session_id]) {
    res.status(429).json({ error: 'Session is currently processing a request' });
    return;
  }

  // Accept problem_description from body (preferred) or fall back to stored value
  const problem_description = req.body?.problem_description || session.problem_description || '';
  if (!problem_description.trim()) {
    res.status(400).json({ error: 'problem_description is required. Paste a problem first.' });
    return;
  }

  // Save to session (so it persists for session reload)
  session.problem_description = problem_description;
  session.updatedAt = new Date().toISOString();

  sessionLocks[session_id] = true;

  try {
    const pythonUrl = process.env.PYTHON_SERVICE_URL || 'http://localhost:8000';
    // Problem analysis runs 6+ sequential LLM agents — allow up to 5 minutes
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5 * 60 * 1000);
    let response: Response;
    try {
      response = await fetch(`${pythonUrl}/problem/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          thread_id: session_id,
          problem_description: problem_description
        }),
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeout);
    }

    if (!response.ok) {
      const errData: any = await response.json().catch(() => ({}));
      throw new Error(errData.error || `Python service responded with status: ${response.status}`);
    }

    const data: any = await response.json();
    if (data.problem_analysis) {
      session.problem_analysis = data.problem_analysis;
      session.constraint_insights = data.constraint_insights;
      session.expected_time_complexity = data.expected_time_complexity;
      session.expected_space_complexity = data.expected_space_complexity;
      session.complexity_reasoning = data.complexity_reasoning;
      session.detected_pattern = data.detected_pattern;
      session.pattern_confidence = data.pattern_confidence;
      session.strategy_plan = data.strategy_plan;
      session.test_case_validation = data.test_case_validation;
    }
    if (data.status) {
      session.analysis_status = data.status;
    }
    res.json(data);
  } catch (error) {
    console.error(`[POST /sessions/${session_id}/problem/analyze] Error:`, error);
    res.status(500).json({ error: error instanceof Error ? error.message : 'Processing failed' });
  } finally {
    sessionLocks[session_id] = false;
  }
});

const VALID_DECISIONS = ['hints', 'generate', 'manual'];

router.post('/sessions/:id/problem/decision', async (req, res) => {
  const session_id = req.params.id;
  const decision = req.body.decision;

  if (!decision || !VALID_DECISIONS.includes(decision)) {
    res.status(400).json({ error: `decision must be one of: ${VALID_DECISIONS.join(', ')}` });
    return;
  }
  
  const session = sessions.find(s => s.id === session_id);
  if (!session) {
    res.status(404).json({ error: 'Session not found' });
    return;
  }

  if (sessionLocks[session_id]) {
    res.status(429).json({ error: 'Session is currently processing a request' });
    return;
  }

  sessionLocks[session_id] = true;

  try {
    const pythonUrl = process.env.PYTHON_SERVICE_URL || 'http://localhost:8000';
    const response = await fetch(`${pythonUrl}/problem/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: session_id,
        decision: decision
      }),
    });

    if (!response.ok) {
      const errData: any = await response.json().catch(() => ({}));
      throw new Error(errData.error || `Python service responded with status: ${response.status}`);
    }

    const data: any = await response.json();
    if (data.status) {
       session.analysis_status = data.status;
    }
    res.json({
      ...data,
      input_code: data.input_code || ''
    });
  } catch (error) {
    console.error(`[POST /sessions/${session_id}/problem/decision] Error:`, error);
    res.status(500).json({ error: error instanceof Error ? error.message : 'Processing failed' });
  } finally {
    sessionLocks[session_id] = false;
  }
});

// Session-aware review approval using the stored review_thread_id
router.post('/sessions/:id/review/approve', async (req, res) => {
  const session_id = req.params.id;
  const session = sessions.find(s => s.id === session_id);
  if (!session) {
    res.status(404).json({ error: 'Session not found' });
    return;
  }

  const review_tid = session.review_thread_id;
  if (!review_tid) {
    res.status(400).json({ error: 'No active review found for this session. Run code review first.' });
    return;
  }

  try {
    const { approved_suggestions } = req.body;
    const result = await coreService.approveReview(review_tid, approved_suggestions || []);
    res.json(result);
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Internal Server Error';
    console.error(`[POST /sessions/${session_id}/review/approve] Error: ${message}`);
    res.status(500).json({ error: message });
  }
});

export default router;
