import tempfile
import unittest
from pathlib import Path

from api.portal_fill import _allowed_application_host, _field_value, _maybe_open_application, fill_application_page, is_public_https_url


PROFILE = {
    "name": "Joshua Kiran Yajjala",
    "preferred_name": "Joshua Kiran Yajjala",
    "email": "joshua@example.test",
    "phone": "+1 555 555 0100",
    "address_line1": "12421 Hardy Street",
    "city": "Overland Park",
    "state": "Kansas",
    "postal_code": "66213",
    "country": "United States",
    "current_company": "Gainwell Technologies",
    "role": "AI Engineer",
    "linkedin_url": "https://linkedin.com/in/joshua-kiran",
    "github_url": "https://github.com/joshua-kiran",
    "skills": ["Python", "AWS", "RAG"],
    "years_experience": 5,
    "education_history": [{"school": "Example University", "degree": "M.S.", "field": "Computer Science"}],
    "work_authorization": "OPT; needs future H-1B sponsorship",
    "summary": "Builds production AI and cloud services.",
}


class PortalFieldMappingTests(unittest.TestCase):
    def test_public_https_guard_rejects_local_and_insecure_urls(self):
        self.assertTrue(is_public_https_url("https://jobs.ashbyhq.com/example/123"))
        self.assertFalse(is_public_https_url("http://jobs.example.com/apply"))
        self.assertFalse(is_public_https_url("https://127.0.0.1/apply"))
        self.assertFalse(is_public_https_url("https://example.invalid/apply"))

    def test_portal_host_guard_allows_same_employer_and_known_ats_only(self):
        self.assertTrue(_allowed_application_host("https://careers.example.com/role", "https://apply.example.com/form"))
        self.assertTrue(_allowed_application_host("https://careers.example.com/role", "https://jobs.ashbyhq.com/example/role"))
        self.assertFalse(_allowed_application_host("https://careers.example.com/role", "https://example-apply.com/form"))

    def test_mapping_is_grounded_and_skips_sensitive_or_ambiguous_fields(self):
        self.assertEqual(_field_value({"label": "First Name"}, PROFILE), ("Joshua", "name"))
        self.assertEqual(_field_value({"label": "Current employer"}, PROFILE), ("Gainwell Technologies", "current_company"))
        self.assertIsNone(_field_value({"label": "Company"}, PROFILE))
        self.assertIsNone(_field_value({"label": "Do you need H-1B sponsorship?"}, PROFILE))
        self.assertIsNone(_field_value({"label": "Gender"}, PROFILE))
        self.assertIsNone(_field_value({"label": "Expected salary"}, PROFILE))


class PortalFormBrowserTests(unittest.TestCase):
    def test_local_application_form_is_filled_and_tailored_files_are_attached(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self.skipTest(f"Playwright is unavailable: {exc}")

        with tempfile.TemporaryDirectory(prefix="neural-ant-form-test-") as temp_dir:
            resume = Path(temp_dir) / "tailored-resume.docx"
            cover = Path(temp_dir) / "tailored-cover-letter.docx"
            resume.write_bytes(b"resume fixture")
            cover.write_bytes(b"cover fixture")
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True, channel="chrome")
                except Exception:
                    try:
                        browser = playwright.chromium.launch(headless=True)
                    except Exception as exc:
                        self.skipTest(f"No Playwright browser is installed: {exc}")
                page = browser.new_page()
                page.set_content("""
                  <form>
                    <label>First Name<input autocomplete="given-name" required></label>
                    <label>Last Name<input autocomplete="family-name" required></label>
                    <label>Email<input type="email" autocomplete="email" required></label>
                    <label>Phone<input type="tel" autocomplete="tel"></label>
                    <label>Street Address<input autocomplete="address-line1"></label>
                    <label>City<input autocomplete="address-level2"></label>
                    <label>State<input autocomplete="address-level1"></label>
                    <label>ZIP Code<input autocomplete="postal-code"></label>
                    <label>Current employer<input></label>
                    <label>Current job title<input></label>
                    <label>LinkedIn profile<input></label>
                    <label>GitHub profile<input></label>
                    <label>School<input></label>
                    <label>Degree<input></label>
                    <label>Skills<textarea></textarea></label>
                    <label>Do you need visa sponsorship?<input required></label>
                    <label>Expected salary<input required></label>
                    <label for="resume">Resume/CV<input id="resume" type="file" required></label>
                    <label for="cover">Cover Letter<input id="cover" type="file"></label>
                    <button type="submit">Submit application</button>
                  </form>
                  <iframe name="embedded-profile"></iframe>
                """)
                embedded = page.frame(name="embedded-profile")
                embedded.set_content('<label>GitHub profile<input></label>')
                result = fill_application_page(page, PROFILE, str(resume), str(cover))
                self.assertEqual(result["status"], "ready")
                self.assertIn("tailored resume", result["uploaded"])
                self.assertIn("tailored cover letter", result["uploaded"])
                self.assertEqual(page.locator('input[autocomplete="given-name"]').input_value(), "Joshua")
                self.assertEqual(page.locator('input[type="email"]').input_value(), PROFILE["email"])
                self.assertEqual(page.locator('input[autocomplete="address-level2"]').input_value(), PROFILE["city"])
                self.assertEqual(embedded.locator('input').input_value(), PROFILE["github_url"])
                self.assertEqual(page.locator('input').nth(14).input_value(), "")
                self.assertEqual(page.locator('input').nth(15).input_value(), "")
                self.assertEqual(page.locator('button[type="submit"]').count(), 1)
                self.assertIn("Current employer", result["filled"])
                page.set_content('<form><button type="button" onclick="document.body.dataset.clicked=\'yes\'">Apply</button></form>')
                _, navigation_note = _maybe_open_application(page, page.context)
                self.assertIn("direct Apply link", navigation_note)
                self.assertEqual(page.locator("body").get_attribute("data-clicked"), None)
                page.set_content('<label for="resume">Resume/CV<input id="resume" type="file" accept=".pdf" required></label>')
                pdf_only = fill_application_page(page, PROFILE, str(resume))
                self.assertNotIn("tailored resume", pdf_only["uploaded"])
                self.assertTrue(any("different file format" in item for item in pdf_only["needs_user"]))
                browser.close()


if __name__ == "__main__":
    unittest.main()
