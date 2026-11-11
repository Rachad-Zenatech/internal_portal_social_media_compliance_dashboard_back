"""Review delegation: a reviewer hands a post in review to someone else and stays on it."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException

from models.compliance.submission_model import DelegateRequest
from services.compliance.submissions import delegation_service

CREATOR = uuid4()
REVIEWER = uuid4()
DELEGATE = uuid4()
SUBS = "services.compliance.submissions.submission_service"
DEL = "services.compliance.submissions.delegation_service"


def submission_row(**overrides):
    row = {"id": uuid4(), "reference_code": "SUB-1001", "title": "Expo post", "status": "IN_REVIEW",
           "created_by": CREATOR, "current_version": 2, "brand_code": "ZENADRONE", "reviewer_ids": [REVIEWER]}
    row.update(overrides)
    return row


def fake_pool(row):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"status": row["status"], "reviewer_ids": list(row["reviewer_ids"])})
    conn.fetch = AsyncMock(return_value=[{"id": REVIEWER, "full_name": "Riley", "email": "r@x"},
                                         {"id": DELEGATE, "full_name": "Dana", "email": "d@x"}])
    conn.execute = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    return pool, conn


class DelegateTests(unittest.IsolatedAsyncioTestCase):
    async def run_delegate(self, row, *, user=REVIEWER, to=DELEGATE, eligible=(DELEGATE,), is_reviewer=True):
        pool, conn = fake_pool(row)
        with (
            patch(f"{SUBS}._get_owned_row", new=AsyncMock(return_value=row)),
            patch(f"{SUBS}._can_review", new=AsyncMock(return_value=is_reviewer)),
            patch(f"{SUBS}._is_super_admin", new=AsyncMock(return_value=False)),
            patch(f"{SUBS}._creator_exclusion", new=AsyncMock(return_value=CREATOR)),
            patch(f"{SUBS}._reviewer_ids", new=AsyncMock(return_value=list(eligible))),
            patch(f"{SUBS}._notify", new=AsyncMock()) as notify,
            patch(f"{SUBS}.get_submission", new=AsyncMock(return_value="updated")),
            patch(f"{DEL}.get_pool", return_value=pool),
            patch(f"{DEL}.log_audit_event", new=AsyncMock()),
        ):
            result = await delegation_service.delegate(row["id"], user, DelegateRequest(to_user_id=to, note="Please"))
        return result, conn, notify

    async def test_the_delegate_joins_the_reviewers_and_the_delegator_stays(self):
        result, conn, notify = await self.run_delegate(submission_row())
        self.assertEqual(result, "updated")
        update = next(c for c in conn.execute.await_args_list if "UPDATE content_submissions" in c.args[0])
        self.assertEqual(update.args[2], [REVIEWER, DELEGATE])
        self.assertTrue(any("INSERT INTO content_review_delegations" in c.args[0] for c in conn.execute.await_args_list))
        self.assertEqual(notify.await_args.args[0], [DELEGATE])

    async def test_a_super_admin_who_wasnt_on_it_stays_on_it_too(self):
        boss = uuid4()
        _, conn, _ = await self.run_delegate(submission_row(), user=boss)
        update = next(c for c in conn.execute.await_args_list if "UPDATE content_submissions" in c.args[0])
        self.assertEqual(update.args[2], [REVIEWER, boss, DELEGATE])

    async def test_only_content_in_review(self):
        with self.assertRaises(HTTPException) as caught:
            await self.run_delegate(submission_row(status="APPROVED"))
        self.assertEqual(caught.exception.status_code, 409)

    async def test_only_reviewers(self):
        with self.assertRaises(HTTPException) as caught:
            await self.run_delegate(submission_row(), is_reviewer=False)
        self.assertEqual(caught.exception.status_code, 403)

    async def test_the_delegate_must_be_able_to_review_the_brand(self):
        with self.assertRaises(HTTPException) as caught:
            await self.run_delegate(submission_row(), eligible=())
        self.assertEqual(caught.exception.status_code, 422)

    async def test_not_to_someone_who_already_has_it(self):
        with self.assertRaises(HTTPException) as caught:
            await self.run_delegate(submission_row(reviewer_ids=[REVIEWER, DELEGATE]))
        self.assertEqual(caught.exception.status_code, 409)


class WithDelegatesTests(unittest.IsolatedAsyncioTestCase):
    async def test_follows_the_chain_from_named_approvers(self):
        a, b, c, stranger, sid = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
        rows = [{"submission_id": sid, "from_user_id": a, "to_user_id": b},
                {"submission_id": sid, "from_user_id": b, "to_user_id": c},
                {"submission_id": sid, "from_user_id": stranger, "to_user_id": uuid4()}]
        with patch(f"{DEL}.fetch_all", new=AsyncMock(return_value=rows)):
            self.assertEqual(await delegation_service.with_delegates(sid, [a]), [a, b, c])

    async def test_no_named_approvers_stays_empty(self):
        self.assertEqual(await delegation_service.with_delegates(uuid4(), []), [])


if __name__ == "__main__":
    unittest.main()
