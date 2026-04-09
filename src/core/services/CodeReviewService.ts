import { CodeReviewPort } from '../interfaces/CodeReviewPort';
import { ReviewResponse, ApproveResponse, DiffViewData } from '../state/CodeReviewState';
import { DiffService } from './DiffService';

export class CodeReviewService {
  private diffService: DiffService;

  constructor(private codeReviewPort: CodeReviewPort) {
    this.diffService = new DiffService();
  }

  public async reviewCode(input_code: string): Promise<ReviewResponse> {
    return this.codeReviewPort.executeReview(input_code);
  }

  public async approveReview(
    thread_id: string,
    approved_suggestions: any[],
  ): Promise<ApproveResponse & { diffData: DiffViewData }> {
    const result = await this.codeReviewPort.approveReview(thread_id, approved_suggestions);

    const diffData = this.diffService.computeDiff(result.input_code, result.final_code);

    return {
      ...result,
      diffData,
    };
  }
}
