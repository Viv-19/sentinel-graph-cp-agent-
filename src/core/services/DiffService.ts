import * as Diff from 'diff';
import { DiffChange, DiffViewData } from '../state/CodeReviewState';

export class DiffService {
  public computeDiff(originalCode: string, finalCode: string): DiffViewData {
    const changes = Diff.diffLines(originalCode, finalCode);

    const diff: DiffChange[] = changes.map((change) => ({
      value: change.value,
      added: change.added || false,
      removed: change.removed || false,
    }));

    return {
      original_code: originalCode,
      final_code: finalCode,
      diff,
    };
  }
}
