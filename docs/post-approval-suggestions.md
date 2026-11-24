# Suggestions after post approval

The first approval keeps the post in `APPROVED`. Other assigned reviewers and suggesting reviewers can leave comments until the creator's `approval_needed_by` deadline. They cannot approve it again, reopen it, or reschedule it. The original approving reviewer retains the existing approval-management actions.

The approved workspace shows a countdown. Once the deadline is reached, other reviewers' comments become read-only. The backend checks the deadline again under the submission row lock when adding, resolving, or deleting a comment, so an old browser tab cannot bypass the cutoff. Existing creator and original-approver comment permissions remain unchanged.

Publishing and final scheduling wait until the same deadline. The creator then selects the final publish time through the existing calendar flow. Each simulated schedule or publish action checks the new authenticated `GET /api/submissions/{id}/publishing-access` endpoint immediately before writing its result. Failure to reach the server blocks the action. Sample calendar items retain their demo behavior. The existing publishing integration remains simulated.

No new database schema or approval state is required. The window uses the saved creator deadline and the current version's recorded approving reviewer. Check snapshots, original scores, and finding feedback are unchanged. An older approved post with no deadline has no extra suggestion window and retains its publishing availability.

## Local testing

1. As a creator, send a post to two reviewers with a deadline a few minutes in the future.
2. Approve it as the first reviewer. Its status should become Approved, and it should remain approved throughout the suggestion window.
3. As the other reviewer, open the post in the Approved tab. Verify the countdown, add a general comment, reply, and add a video or image annotation when applicable. Approval, reopening, and rescheduling controls should be absent.
4. As the creator, verify the new suggestions are visible and publishing is blocked, including from the calendar or a previously opened publish page.
5. Wait for the deadline. Verify the other reviewer's suggestion controls become read-only, and attempts to submit from a stale tab fail without changing the approval or saved comments.
6. As the creator, choose a future final publish time and confirm publishing. The simulated calendar should now accept it.

An already-open reviewer workspace keeps the approved post selected after the queue refresh, so it switches to suggestion-only mode instead of moving to the next post.
