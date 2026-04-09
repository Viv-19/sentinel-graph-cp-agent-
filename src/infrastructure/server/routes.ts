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
  if (!context) {
    res.status(404).json({ error: 'Session not found' });
    return;
  }
  res.json({ id: req.params.id, context });
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

    const data = await response.json();
    res.json(data);
  } catch (error) {
    console.error(`[POST /sessions/${session_id}/input] Error:`, error);
    res.status(500).json({ error: error instanceof Error ? error.message : 'Processing failed' });
  } finally {
    sessionLocks[session_id] = false;
  }
});

export default router;
