import copy
import asyncio
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch


_test_storage = tempfile.TemporaryDirectory(prefix="neural-ant-tests-")
os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_test_storage.name, "tests.sqlite3")
os.environ["UPLOAD_DIR"] = os.path.join(_test_storage.name, "resumes")
os.environ["SEARCH_INTERVAL_SECONDS"] = "0"

import httpx
from sqlalchemy import delete, event, func, select

from api import main as appmod


class BatchPrepareEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        appmod.initialize_database()

    @classmethod
    def tearDownClass(cls):
        appmod.close_http_client()
        appmod.engine.dispose()
        _test_storage.cleanup()

    def setUp(self):
        with appmod.SessionLocal() as db:
            for model in (appmod.DraftRow, appmod.ApplicationRow, appmod.JobRow, appmod.ResumeRow, appmod.ProfileRow):
                db.execute(delete(model))
            profile = copy.deepcopy(appmod.DEFAULT_PROFILE)
            profile.update({
                "name": "Smoke Test Candidate",
                "preferred_name": "Smoke Test Candidate",
                "target_titles": ["AI Engineer"],
                "skills": ["Python", "AWS", "FastAPI"],
                "years_experience": 5,
                "summary": "Builds Python services and cloud applications.",
            })
            db.add(appmod.ProfileRow(id=1, data=profile))
            db.add(appmod.ResumeRow(
                id=1,
                filename="sample.pdf",
                file_key="smoke/sample.pdf",
                file_sha256="0" * 64,
                extracted_text=(
                    "Smoke Test Candidate\nPython, AWS, FastAPI\n"
                    "Built cloud services and production software."
                ),
            ))
            db.add_all([
                appmod.JobRow(
                    id=number,
                    url=f"https://example.invalid/jobs/{number}",
                    company_name=f"Example {number}",
                    title="AI Engineer",
                    source="test-fixture",
                    location="Remote, US",
                    work_mode="Remote",
                    employment_type="Full-time",
                    description="Build reliable Python and AWS services for AI products.",
                    fit_score=80,
                    fit_breakdown={},
                )
                for number in range(1, 8)
            ])
            db.add(appmod.ApplicationRow(job_id=7, stage="Done", notes="Already completed"))
            db.commit()

    def post_batch(self, job_ids, **extra):
        body = {"confirmed_resume_use": True, "job_ids": job_ids, **extra}
        return self.request_json("POST", "/api/applications/batch-prepare", body)

    @staticmethod
    def request_json(method, path, body=None):
        async def send():
            transport = httpx.ASGITransport(app=appmod.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.request(method, path, json=body)
        return asyncio.run(send())

    def test_requires_resume_confirmation_and_external_ai_opt_in(self):
        missing_confirmation = self.request_json("POST", "/api/applications/batch-prepare", {"job_ids": [1]})
        self.assertEqual(missing_confirmation.status_code, 400)

        missing_external_opt_in = self.post_batch([1], use_external_ai=True, external_ai_confirmed=False)
        self.assertEqual(missing_external_opt_in.status_code, 400)

        with appmod.SessionLocal() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(appmod.ApplicationRow).where(appmod.ApplicationRow.job_id == 1)),
                0,
            )
            self.assertEqual(db.scalar(select(func.count()).select_from(appmod.DraftRow)), 0)

    def test_rejects_duplicate_or_oversized_batches(self):
        duplicates = self.post_batch([1, 1])
        oversized = self.post_batch([1, 2, 3, 4, 5, 6])
        self.assertEqual(duplicates.status_code, 422)
        self.assertEqual(oversized.status_code, 422)

    def test_creates_once_reuses_existing_draft_and_skips_completed_role(self):
        response = self.post_batch([1, 7], execution_mode="planned")
        self.assertEqual(response.status_code, 200, response.text)
        roles = response.json()["roles"]
        self.assertEqual([role["status"] for role in roles], ["prepared", "skipped_completed"])
        first_draft_id = roles[0]["draft_id"]
        self.assertEqual(response.json()["execution_mode"], "planned")

        repeated = self.post_batch([1])
        self.assertEqual(repeated.status_code, 200, repeated.text)
        existing = repeated.json()["roles"][0]
        self.assertEqual(existing["status"], "existing")
        self.assertEqual(existing["draft_id"], first_draft_id)

        with appmod.SessionLocal() as db:
            drafts = db.scalars(select(appmod.DraftRow)).all()
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0].job_id, 1)

    def test_hard_fit_gate_blocks_draft_creation(self):
        with appmod.SessionLocal() as db:
            job = db.get(appmod.JobRow, 5)
            job.fit_breakdown = {"eligibility_gate": {"status": "FAIL", "detail": "Fixture mismatch."}}
            db.commit()

        response = self.post_batch([5])
        self.assertEqual(response.status_code, 409)
        with appmod.SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(appmod.DraftRow)), 0)

    def test_role_pack_concurrency_is_two_planned_and_one_sequential(self):
        def run_and_measure(job_ids, mode):
            state = {"active": 0, "peak": 0}
            lock = threading.Lock()

            def fake_draft(*args, **kwargs):
                with lock:
                    state["active"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                try:
                    time.sleep(0.04)
                finally:
                    with lock:
                        state["active"] -= 1
                return {"content": "resume", "cover_letter": "letter", "review_notes": "review", "model": "test"}

            with patch.object(appmod, "build_application_draft", side_effect=fake_draft):
                response = self.post_batch(job_ids, execution_mode=mode)
            return response, state["peak"]

        planned, planned_peak = run_and_measure([1, 2, 3], "planned")
        self.assertEqual(planned.status_code, 200, planned.text)
        self.assertEqual(planned.json()["max_concurrency"], 2)
        self.assertEqual(planned_peak, 2)

        sequential, sequential_peak = run_and_measure([4, 5], "sequential")
        self.assertEqual(sequential.status_code, 200, sequential.text)
        self.assertEqual(sequential.json()["max_concurrency"], 1)
        self.assertEqual(sequential_peak, 1)

    def test_board_refresh_bulk_loads_existing_jobs_and_sponsor_evidence(self):
        fixtures = [
            {
                "company": f"Performance Fixture {number}",
                "title": "AI Engineer",
                "url": f"https://example.invalid/perf/{number}",
                "description": "Build Python services and reliable AI products.",
                "location": "Remote, US",
                "work_mode": "Remote",
                "source": "test-fixture",
            }
            for number in range(40)
        ]
        with appmod.SessionLocal() as db:
            db.execute(delete(appmod.SourceRow))
            db.execute(delete(appmod.SponsorRow))
            db.add(appmod.SourceRow(ats_type="greenhouse", slug="fixture", company_name="Performance Fixture"))
            db.add(appmod.SponsorRow(
                company_key=appmod.company_key("Performance Fixture"),
                company_name="Performance Fixture",
                evidence_url="https://example.invalid/sponsor",
                evidence_year="2026",
                filing_count=1,
            ))
            db.commit()

        counts = {"sponsor_selects": 0, "url_equality_selects": 0, "url_batch_selects": 0}

        def count_queries(_conn, _cursor, statement, _parameters, _context, _many):
            sql = " ".join(statement.casefold().split())
            if "from sponsor_evidence" in sql:
                counts["sponsor_selects"] += 1
            if "from jobs" in sql and "where jobs.url =" in sql:
                counts["url_equality_selects"] += 1
            if "from jobs" in sql and "where jobs.url in" in sql:
                counts["url_batch_selects"] += 1

        event.listen(appmod.engine, "before_cursor_execute", count_queries)
        try:
            with appmod.SessionLocal() as db, patch.object(appmod, "parse_greenhouse", return_value=fixtures):
                result = appmod.run_search(db, send_alerts=False, days=14)
            self.assertEqual(result["jobs_saved"], len(fixtures))
            self.assertEqual(counts["sponsor_selects"], 1)
            self.assertEqual(counts["url_equality_selects"], 0)
            self.assertEqual(counts["url_batch_selects"], 1)
            self.assertIn("timings_ms", result)
        finally:
            event.remove(appmod.engine, "before_cursor_execute", count_queries)
            with appmod.SessionLocal() as db:
                fixture_ids = select(appmod.JobRow.id).where(appmod.JobRow.source == "test-fixture")
                db.execute(delete(appmod.ApplicationRow).where(appmod.ApplicationRow.job_id.in_(fixture_ids)))
                db.execute(delete(appmod.JobRow).where(appmod.JobRow.source == "test-fixture"))
                db.execute(delete(appmod.SourceRow).where(appmod.SourceRow.slug == "fixture"))
                db.execute(delete(appmod.SponsorRow).where(appmod.SponsorRow.evidence_url == "https://example.invalid/sponsor"))
                db.commit()

    def test_freehire_search_bulk_loads_existing_jobs_and_sponsor_evidence(self):
        fixtures = [
            {
                "company": f"Freehire Fixture {number}",
                "title": "AI Engineer",
                "url": f"https://example.invalid/freehire/{number}",
                "description": "Build Python services and reliable AI products.",
                "work_mode": "remote",
                "enrichment": {},
            }
            for number in range(40)
        ]

        class FakeResponse:
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"data": fixtures, "meta": {"total": len(fixtures)}}

        class FakeClient:
            @staticmethod
            def get(*_args, **_kwargs):
                return FakeResponse()

        with appmod.SessionLocal() as db:
            db.execute(delete(appmod.SponsorRow))
            db.add(appmod.SponsorRow(
                company_key=appmod.company_key("Freehire Fixture"),
                company_name="Freehire Fixture",
                evidence_url="https://example.invalid/freehire-sponsor",
                evidence_year="2026",
                filing_count=1,
            ))
            db.commit()

        counts = {"sponsor_selects": 0, "url_equality_selects": 0, "url_batch_selects": 0}

        def count_queries(_conn, _cursor, statement, _parameters, _context, _many):
            sql = " ".join(statement.casefold().split())
            if "from sponsor_evidence" in sql:
                counts["sponsor_selects"] += 1
            if "from jobs" in sql and "where jobs.url =" in sql:
                counts["url_equality_selects"] += 1
            if "from jobs" in sql and "where jobs.url in" in sql:
                counts["url_batch_selects"] += 1

        event.listen(appmod.engine, "before_cursor_execute", count_queries)
        try:
            body = appmod.FreehireSearchBody(query='title:"AI Engineer"', country="US", work_mode="remote", days=14, limit=50)
            with appmod.SessionLocal() as db, patch.object(appmod, "get_http_client", return_value=FakeClient()):
                result = appmod.search_freehire(body, None, db)
            self.assertEqual(result["jobs_saved"], len(fixtures))
            self.assertEqual(counts["sponsor_selects"], 1)
            self.assertEqual(counts["url_equality_selects"], 0)
            self.assertEqual(counts["url_batch_selects"], 1)
            self.assertIn("timings_ms", result)
        finally:
            event.remove(appmod.engine, "before_cursor_execute", count_queries)
            with appmod.SessionLocal() as db:
                fixture_ids = select(appmod.JobRow.id).where(appmod.JobRow.url.like("https://example.invalid/freehire/%"))
                db.execute(delete(appmod.ApplicationRow).where(appmod.ApplicationRow.job_id.in_(fixture_ids)))
                db.execute(delete(appmod.JobRow).where(appmod.JobRow.url.like("https://example.invalid/freehire/%")))
                db.execute(delete(appmod.SponsorRow).where(appmod.SponsorRow.evidence_url == "https://example.invalid/freehire-sponsor"))
                db.commit()

    def test_shared_outbound_client_is_reused_and_can_be_reopened(self):
        appmod.close_http_client()
        first = appmod.get_http_client()
        self.assertIs(first, appmod.get_http_client())
        appmod.close_http_client()
        reopened = appmod.get_http_client()
        self.assertIsNot(first, reopened)
        self.assertFalse(reopened.is_closed)


class ExternalDraftPlanTests(unittest.TestCase):
    def test_parallel_mode_overlaps_independent_ai_calls_but_keeps_review_between_stages(self):
        job = SimpleNamespace(title="AI Engineer", company_name="Example", description="Build Python services.")
        profile = {"name": "Test Candidate", "preferred_name": "Test Candidate", "skills": ["Python"]}

        for mode, expected_peak in (("planned", 2), ("sequential", 1)):
            with self.subTest(mode=mode):
                state = {"active": 0, "peak": 0, "calls": []}
                lock = threading.Lock()

                def tracked_call(label):
                    with lock:
                        state["active"] += 1
                        state["peak"] = max(state["peak"], state["active"])
                        state["calls"].append(label)
                    try:
                        time.sleep(0.025)
                    finally:
                        with lock:
                            state["active"] -= 1

                def tailor(*args):
                    tracked_call("tailor")
                    return "resume draft with Python", "test-model"

                def cover(*args):
                    tracked_call("cover")
                    return "Dear Hiring Manager, Sincerely, [Your name]", "test-model"

                def review(*args):
                    state["calls"].append("review")
                    return "review notes", "test-model"

                def revise(document_name, source, job_row, draft, review_notes):
                    tracked_call("revise:" + document_name)
                    return draft, "test-model"

                with (
                    patch.object(appmod, "external_tailor", side_effect=tailor),
                    patch.object(appmod, "external_cover_letter", side_effect=cover),
                    patch.object(appmod, "external_application_review", side_effect=review),
                    patch.object(appmod, "external_revise_document", side_effect=revise),
                    patch.object(appmod, "build_resume_docx", return_value=b"fixture-docx"),
                    patch.object(appmod, "final_resume_docx_text_audit", return_value="fixture audit"),
                ):
                    result = appmod.build_application_draft(
                        "Test Candidate has Python experience.",
                        job,
                        profile,
                        None,
                        use_external_ai=True,
                        execution_mode=mode,
                    )

                self.assertEqual(state["peak"], expected_peak)
                self.assertIn("Test Candidate", result["cover_letter"])
                self.assertIn("Independent review", result["review_notes"])
                review_index = state["calls"].index("review")
                self.assertGreater(review_index, state["calls"].index("tailor"))
                self.assertGreater(review_index, state["calls"].index("cover"))
                self.assertLess(review_index, state["calls"].index("revise:resume"))
                self.assertLess(review_index, state["calls"].index("revise:cover letter"))


if __name__ == "__main__":
    unittest.main()
