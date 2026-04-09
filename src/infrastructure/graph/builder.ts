import { StateGraph } from '@langchain/langgraph';
import { CodeReviewState } from '../../core/state/CodeReviewState';

export function createGraph() {
  const builder = new StateGraph<CodeReviewState>({
    channels: {
      input_code: {
        value: (x, y) => y ?? x,
        default: () => ""
      },
      language: {
        value: (x, y) => y ?? x,
        default: () => ""
      },
      findings: {
        value: (x, y) => x.concat(y ?? []),
        default: () => []
      },
      aggregated_report: {
        value: (x, y) => y ?? x,
        default: () => ({})
      },
      approved_suggestions: {
        value: (x, y) => x.concat(y ?? []),
        default: () => []
      },
      final_code: {
        value: (x, y) => y ?? x,
        default: () => ""
      }
    }
  });

  return builder;
}
