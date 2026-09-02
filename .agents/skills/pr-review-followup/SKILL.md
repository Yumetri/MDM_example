---
name: pr-review-followup
description: Complete GitHub follow-up after a pushed commit addresses review feedback on an existing pull request. Use only for that post-push situation, not for a new pull request, description-only edits, comment-only updates, or an ordinary push without review fixes.
---

# PR Review Follow-up

Finish the review-feedback cycle for the current pull request without starting another fix cycle.

## Preconditions

Confirm that all of the following are true before taking action:

- The pull request already existed before the fixing commit was pushed.
- The current pull request head contains a newly pushed commit that addresses reviewer feedback.
- The update changes more than only the pull request description or comments.

If these conditions are not met, do not run this workflow. If the pull request or fixing commit
cannot be identified unambiguously, stop and report the blocker.

## Workflow

1. Identify the current pull request head SHA, the fixing commit SHA, the review threads addressed
   by that commit, and the commit covered by the latest Codex review.
2. Wait for the required `quality` check on the current head to pass. If it fails or cannot complete,
   report the result and stop the workflow without replying to or resolving threads, or requesting
   another review.
3. Reply to every addressed review thread with a concise summary of the fix and the fixing commit
   SHA. Resolve a thread only when its feedback is fully addressed; leave partially addressed or
   unrelated threads unresolved.
4. Compare the current pull request head SHA with the commit covered by the latest Codex review. If
   they differ and no Codex review is pending, inspect existing top-level comments and request a
   review only when that exact head SHA has not already received a request. Make the request as one
   separate top-level comment containing exactly `@codex review`.
5. Wait for the resulting Codex review when one was requested, then report its outcome to the user.
   If no request was needed, report the current review state and the actions completed.

Never request more than one Codex review for the same head commit. Do not fix findings from the
resulting review or begin another fix-and-rereview cycle unless the user explicitly asks for it.
