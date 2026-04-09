export interface CodeReviewState {
  input_code: string;
  language?: string;
  findings?: any[];
  aggregated_report?: Record<string, any>[];
  approved_suggestions?: any[];
  final_code?: string;
}

export interface ReviewResponse {
  status: 'PENDING_APPROVAL';
  thread_id: string;
  input_code: string;
  findings: any[];
  aggregated_report: any[];
}

export interface ApproveResponse {
  status: 'COMPLETED';
  input_code: string;
  final_code: string;
}

export interface DiffChange {
  value: string;
  added?: boolean;
  removed?: boolean;
}

export interface DiffViewData {
  original_code: string;
  final_code: string;
  diff: DiffChange[];
}
