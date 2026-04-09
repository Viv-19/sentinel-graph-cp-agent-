import { CodeReviewPort } from '../../core/interfaces/CodeReviewPort';
import { ReviewResponse, ApproveResponse } from '../../core/state/CodeReviewState';

export class PythonGraphAdapter implements CodeReviewPort {
  private baseUrl: string;

  constructor() {
    this.baseUrl = process.env.PYTHON_SERVICE_URL || 'http://localhost:8000';
  }

  public async executeReview(input_code: string): Promise<ReviewResponse> {
    const response = await fetch(`${this.baseUrl}/review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input_code }),
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error((errorData as any).error || `Python service error (${response.status})`);
    }

    return response.json() as Promise<ReviewResponse>;
  }

  public async approveReview(
    thread_id: string,
    approved_suggestions: any[],
  ): Promise<ApproveResponse> {
    const response = await fetch(`${this.baseUrl}/review/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ thread_id, approved_suggestions }),
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error((errorData as any).error || `Python service error (${response.status})`);
    }

    return response.json() as Promise<ApproveResponse>;
  }
}
