import { z } from 'zod';

export const FindingSchema = z.object({
  type: z.enum(['bug', 'style', 'performance']),
  severity: z.enum(['low', 'medium', 'high']),
  message: z.string(),
  suggestion: z.string()
});

export const FindingsArraySchema = z.array(FindingSchema);

export type Finding = z.infer<typeof FindingSchema>;
