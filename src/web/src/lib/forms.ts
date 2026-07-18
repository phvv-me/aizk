import type { SubmitFunction } from '@sveltejs/kit';
import { toast } from 'svelte-sonner';

export type FeedbackOptions = {
  reset?: boolean;
  pending?: (active: boolean) => void;
};

/** Standard `use:enhance` handler: toast the outcome and refresh server loads. */
export function feedback(success: string, options: FeedbackOptions = {}): SubmitFunction {
  return () => {
    options.pending?.(true);
    return async ({ result, update }) => {
      options.pending?.(false);
      if (result.type === 'failure') {
        toast.error(String(result.data?.message ?? 'The request failed.'));
      } else if (result.type === 'error') {
        toast.error(result.error.message);
      } else {
        toast.success(success);
      }
      await update({ reset: (options.reset ?? true) && result.type === 'success' });
    };
  };
}
