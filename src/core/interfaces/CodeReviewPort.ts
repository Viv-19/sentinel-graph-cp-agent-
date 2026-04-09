import { ReviewResponse, ApproveResponse } from '../state/CodeReviewState';

export interface CodeReviewPort {
  executeReview(input_code: string): Promise<ReviewResponse>;
  approveReview(thread_id: string, approved_suggestions: any[]): Promise<ApproveResponse>;
}
