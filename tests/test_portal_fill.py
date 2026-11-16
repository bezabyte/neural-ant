import tempfile
import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from api import portal_fill
from api.portal_fill import _allowed_application_host, _field_value, _fill_connected_chrome_page, is_public_https_url


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
        self.assertTrue(_allowed_application_host("https://account.ycombinator.com/authenticate", "https://www.workatastartup.com/application/personal"))
        self.assertFalse(_allowed_application_host("https://careers.example.com/role", "https://example-apply.com/form"))

    def test_mapping_is_grounded_and_skips_sensitive_or_ambiguous_fields(self):
        self.assertEqual(_field_value({"label": "First Name"}, PROFILE), ("Joshua", "name"))
        self.assertEqual(_field_value({"label": "Current employer"}, PROFILE), ("Gainwell Technologies", "current_company"))
        self.assertIsNone(_field_value({"label": "Company"}, PROFILE))
        self.assertIsNone(_field_value({"label": "Do you need H-1B sponsorship?"}, PROFILE))
        self.assertIsNone(_field_value({"label": "Gender"}, PROFILE))
        self.assertIsNone(_field_value({"label": "Expected salary"}, PROFILE))

    def test_explicitly_approved_yes_no_answers_are_scoped_and_option_matched(self):
        approved = {"us_work_authorized": "Yes", "us_visa_sponsorship": "Yes"}
        authorized_yes = {"type": "radio", "label": "Yes Are you legally authorized to work in the United States?", "optionValue": "Yes", "_approved_sensitive_answers": approved}
        authorized_no = {**authorized_yes, "label": "No Are you legally authorized to work in the United States?", "optionValue": "No"}
        sponsor_yes = {"type": "radio", "label": "Yes Do you or will you require sponsorship for a US employment visa?", "optionValue": "Yes", "_approved_sensitive_answers": approved}
        self.assertEqual(_field_value(authorized_yes, PROFILE), ("Yes", "work_authorization"))
        self.assertEqual(_field_value(sponsor_yes, PROFILE), ("Yes", "sponsorship_preferences"))
        self.assertIsNone(_field_value(authorized_no, PROFILE))
        self.assertIsNone(_field_value({**authorized_yes, "_approved_sensitive_answers": {}}, PROFILE))

    def test_current_location_uses_saved_city_and_state(self):
        self.assertEqual(_field_value({"label": "Current location*"}, PROFILE), ("Overland Park, Kansas", "location"))


class ConnectedChromePreparationTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, state, after_navigation=None):
            self.state = state
            self.after_navigation = after_navigation
            self.calls = []

        def tool(self, name, arguments=None, timeout=30):
            arguments = arguments or {}
            self.calls.append((name, arguments))
            if name == "list_pages":
                return {"structuredContent": {"pages": [{"id": 7, "url": self.state.get("url", "https://jobs.ashbyhq.com/example/role"), "title": self.state.get("title", "Application")}]}}
            if name == "evaluate_script":
                function = arguments.get("function", "")
                match = re.search(r"const operations = (\[.*?\]);", function, re.S)
                value = [item["index"] for item in json.loads(match.group(1))] if match else self.state
                return {"content": [{"type": "text", "text": "Script ran on page and returned:\n```json\n" + json.dumps(value) + "\n```"}]}
            if name == "navigate_page":
                self.state = self.after_navigation or self.state
                return {"content": [{"type": "text", "text": "Navigated."}]}
            if name == "take_snapshot":
                return {"structuredContent": {"snapshot": {"root": {"role": "RootWebArea", "name": "Application", "children": [
                    {"id": "resume-button", "role": "button", "name": "Upload Resume"},
                ]}}}}
            if name == "upload_file":
                return {"content": [{"type": "text", "text": "File uploaded."}]}
            raise AssertionError(f"Unexpected Chrome tool call: {name}")

    def test_existing_chrome_form_gets_only_supported_values_and_tailored_resume(self):
        state = {
            "url": "https://jobs.ashbyhq.com/example/role",
            "title": "Application",
            "text": "Applied AI Engineer",
            "passwordCount": 0,
            "fields": [
                {"index": 0, "tag": "input", "type": "text", "visible": True, "label": "First Name", "required": True, "value": ""},
                {"index": 1, "tag": "input", "type": "email", "visible": True, "label": "Email", "required": True, "value": ""},
                {"index": 2, "tag": "input", "type": "text", "visible": True, "label": "Do you need H-1B sponsorship?", "required": True, "value": ""},
                {"index": 3, "tag": "input", "type": "text", "visible": True, "label": "Expected salary", "required": True, "value": ""},
            ],
            "files": [{"index": 0, "label": "Resume", "accept": ".docx", "visible": True}],
            "links": [],
        }
        with tempfile.TemporaryDirectory(prefix="neural-ant-connected-chrome-test-") as temp_dir:
            resume = Path(temp_dir) / "role-resume.docx"
            resume.write_bytes(b"tailored resume fixture")
            client = self.FakeClient(state)
            result = _fill_connected_chrome_page(client, 7, {
                "url": "https://jobs.ashbyhq.com/example/role",
                "profile": PROFILE,
                "resume_path": str(resume),
                "cover_letter_path": "",
            })
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["filled"], ["First Name", "Email"])
        self.assertEqual(result["uploaded"], ["tailored resume"])
        self.assertTrue(any("H-1B" in item for item in result["needs_user"]))
        self.assertTrue(any("salary" in item.casefold() for item in result["needs_user"]))
        names = [name for name, _ in client.calls]
        self.assertIn("upload_file", names)
        self.assertNotIn("click", names)
        upload = next(args for name, args in client.calls if name == "upload_file")
        self.assertEqual(upload["uid"], "resume-button")

    def test_direct_apply_anchor_opens_form_without_clicking_page_controls(self):
        posting = {
            "url": "https://jobs.ashbyhq.com/example/role",
            "title": "Applied AI Engineer",
            "text": "Role details",
            "passwordCount": 0,
            "fields": [],
            "files": [],
            "links": [{"text": "Apply now", "href": "https://jobs.ashbyhq.com/example/application", "visible": True, "inForm": False}],
        }
        form = {**posting, "url": "https://jobs.ashbyhq.com/example/application", "links": [], "fields": [
            {"index": 0, "tag": "input", "type": "email", "visible": True, "label": "Email", "required": True, "value": ""},
        ]}
        client = self.FakeClient(posting, after_navigation=form)
        result = _fill_connected_chrome_page(client, 7, {
            "url": "https://jobs.ashbyhq.com/example/role",
            "profile": PROFILE,
            "resume_path": "",
            "cover_letter_path": "",
        })
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["filled"], ["Email"])
        self.assertIn("navigate_page", [name for name, _ in client.calls])
        self.assertNotIn("click", [name for name, _ in client.calls])

    def test_yc_role_apply_link_is_followed_instead_of_generic_apply_link(self):
        posting = {
            "url": "https://www.ycombinator.com/companies/epsilab-com/jobs/example",
            "title": "Research Engineer at Epsilab",
            "text": "Role details",
            "passwordCount": 0,
            "fields": [],
            "files": [],
            "links": [
                {"text": "Apply", "href": "https://www.ycombinator.com/apply", "visible": True, "inForm": False},
                {"text": "Apply to role ›", "href": "https://account.ycombinator.com/authenticate?continue=https%3A%2F%2Fwww.workatastartup.com%2Fapplication%3Fsignup_job_id%3D1", "visible": True, "inForm": False},
            ],
        }
        auth = {**posting, "url": "https://account.ycombinator.com/authenticate?continue=...", "title": "Sign in to Y Combinator", "passwordCount": 1, "links": []}
        client = self.FakeClient(posting, after_navigation=auth)
        result = _fill_connected_chrome_page(client, 7, {
            "url": posting["url"], "profile": PROFILE, "resume_path": "", "cover_letter_path": "",
        })
        self.assertEqual(result["status"], "sign_in_required")
        navigation = next(args for name, args in client.calls if name == "navigate_page")
        self.assertIn("account.ycombinator.com/authenticate", navigation["url"])

    def test_yc_profile_redirect_accepts_saved_data_and_attaches_resume(self):
        posting_url = "https://www.ycombinator.com/companies/epsilab-com/jobs/PqLmCN4-member-of-technical-staff-research-engineer-reinforcement-learning"
        posting = {
            "url": posting_url, "title": "Research Engineer at Epsilab", "text": "Role details",
            "passwordCount": 0, "fields": [], "files": [],
            "links": [{"text": "Apply to role ›", "href": "https://account.ycombinator.com/authenticate?continue=https%3A%2F%2Fwww.workatastartup.com%2Fapplication%3Fsignup_job_id%3D121224", "visible": True, "inForm": False}],
        }
        profile_page = {
            "url": "https://www.workatastartup.com/application/personal", "title": "My Profile | Y Combinator's Work at a Startup",
            "text": "About me. Connect with over one thousand YC startup founders.", "passwordCount": 0,
            "fields": [
                {"index": 0, "tag": "input", "type": "text", "visible": True, "label": "First Name", "required": True, "value": ""},
                {"index": 1, "tag": "input", "type": "email", "visible": True, "label": "Email Address (Personal)", "required": True, "value": ""},
            ],
            "files": [{"index": 0, "label": "Autofill with resume", "accept": "", "visible": True}], "links": [],
        }
        with tempfile.TemporaryDirectory(prefix="neural-ant-yc-profile-test-") as temp_dir:
            resume = Path(temp_dir) / "epsilab-resume.docx"
            resume.write_bytes(b"tailored resume")
            client = self.FakeClient(posting, after_navigation=profile_page)
            result = _fill_connected_chrome_page(client, 7, {
                "url": posting_url, "profile": PROFILE, "resume_path": str(resume), "cover_letter_path": "",
                "approved_sensitive_answers": {"us_work_authorized": "Yes", "us_visa_sponsorship": "Yes"},
            })
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["uploaded"], ["tailored resume"])
        self.assertEqual(result["filled"], ["First Name", "Email Address (Personal)"])
        self.assertEqual(result["_page_id"], 7)
        self.assertNotIn("click", [name for name, _ in client.calls])

    def test_generic_yc_apply_page_is_not_reported_ready(self):
        posting = {
            "url": "https://jobs.ashbyhq.com/example/role", "title": "Application", "text": "",
            "passwordCount": 0, "fields": [], "files": [],
            "links": [{"text": "Apply", "href": "https://www.ycombinator.com/apply", "visible": True, "inForm": False}],
        }
        client = self.FakeClient(posting)
        result = _fill_connected_chrome_page(client, 7, {
            "url": "https://jobs.ashbyhq.com/example/role", "profile": PROFILE, "resume_path": "", "cover_letter_path": "",
        })
        self.assertEqual(result["status"], "needs_manual_navigation")
        self.assertNotIn("navigate_page", [name for name, _ in client.calls])

    def test_epsilab_profile_answers_and_resume_are_prepared_together(self):
        state = {
            "url": "https://www.ycombinator.com/companies/epsilab-com/jobs/example/application",
            "title": "Apply to Epsilab", "text": "", "passwordCount": 0,
            "fields": [
                {"index": 0, "tag": "input", "type": "text", "visible": True, "label": "First Name", "required": True, "value": ""},
                {"index": 1, "tag": "input", "type": "text", "visible": True, "label": "Current location*", "required": True, "value": ""},
                {"index": 2, "tag": "input", "type": "radio", "visible": True, "label": "Yes Are you legally authorized to work in the United States?", "optionValue": "Yes", "checked": False, "required": True, "value": ""},
                {"index": 3, "tag": "input", "type": "radio", "visible": True, "label": "No Are you legally authorized to work in the United States?", "optionValue": "No", "checked": False, "required": True, "value": ""},
                {"index": 4, "tag": "input", "type": "radio", "visible": True, "label": "Yes Do you or will you require sponsorship for a US employment visa?", "optionValue": "Yes", "checked": False, "required": True, "value": ""},
                {"index": 5, "tag": "input", "type": "radio", "visible": True, "label": "No Do you or will you require sponsorship for a US employment visa?", "optionValue": "No", "checked": False, "required": True, "value": ""},
            ],
            "files": [{"index": 0, "label": "Resume", "accept": ".docx", "visible": True}], "links": [],
        }
        with tempfile.TemporaryDirectory(prefix="neural-ant-epsilab-test-") as temp_dir:
            resume = Path(temp_dir) / "epsilab-resume.docx"
            resume.write_bytes(b"tailored resume")
            client = self.FakeClient(state)
            result = _fill_connected_chrome_page(client, 7, {
                "url": state["url"], "profile": PROFILE, "resume_path": str(resume), "cover_letter_path": "",
                "approved_sensitive_answers": {"us_work_authorized": "Yes", "us_visa_sponsorship": "Yes"},
            })
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["uploaded"], ["tailored resume"])
        self.assertIn("Current location*", result["filled"])
        self.assertTrue(any("authorized to work" in label for label in result["filled"]))
        self.assertTrue(any("require sponsorship" in label for label in result["filled"]))
        operations = next(args for name, args in client.calls if name == "evaluate_script" and "const operations" in args.get("function", ""))
        serialized = re.search(r"const operations = (\[.*?\]);", operations["function"], re.S)
        self.assertEqual([item["index"] for item in json.loads(serialized.group(1))], [0, 1, 2, 4])
        self.assertNotIn("click", [name for name, _ in client.calls])

    def test_unrecognized_portal_gets_no_profile_data(self):
        client = self.FakeClient({})
        client.tool = lambda name, arguments=None, timeout=30: {
            "structuredContent": {"pages": [{"id": 7, "url": "https://lookalike-apply.example.test/form", "title": "Application"}]}
        }
        result = _fill_connected_chrome_page(client, 7, {
            "url": "https://jobs.ashbyhq.com/example/role",
            "profile": PROFILE,
            "resume_path": "/private/tailored.docx",
            "cover_letter_path": "",
        })
        self.assertEqual(result["status"], "unrecognized_portal")
        self.assertEqual(result["filled"], [])

    def test_mcp_bridge_uses_current_chrome_and_disables_telemetry(self):
        with tempfile.TemporaryDirectory(prefix="neural-ant-fake-mcp-") as temp_dir:
            executable = Path(temp_dir) / "fake-chrome-devtools-mcp"
            args_record = Path(temp_dir) / "args.json"
            executable.write_text("""#!/usr/bin/env python3
import json, os, sys
Path = __import__('pathlib').Path
Path(os.environ['NA_ARGS_RECORD']).write_text(json.dumps(sys.argv[1:]))
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if 'id' not in message:
        continue
    if method == 'initialize':
        result = {'protocolVersion': '2024-11-05', 'capabilities': {}, 'serverInfo': {'name': 'fake', 'version': '1'}}
    elif method == 'tools/list':
        names = ['list_pages', 'new_page', 'navigate_page', 'evaluate_script', 'take_snapshot', 'upload_file']
        result = {'tools': [{'name': name, 'inputSchema': {'type': 'object'}} for name in names]}
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)
""")
            executable.chmod(0o700)
            with patch.object(portal_fill, "_CHROME_MCP_BIN", executable), patch.dict(os.environ, {"NA_ARGS_RECORD": str(args_record)}):
                client = portal_fill._ChromeDevtoolsClient(timeout=5)
                client.close()
            args = json.loads(args_record.read_text())
        self.assertIn("--autoConnect", args)
        self.assertIn("--experimentalStructuredContent", args)
        self.assertIn("--no-usage-statistics", args)
        self.assertIn("--no-performance-crux", args)
        self.assertFalse(any("userDataDir" in arg or "user-data-dir" in arg or "isolated" in arg for arg in args))

    def test_chrome_settings_open_in_existing_app_without_a_new_profile(self):
        with patch.object(portal_fill.platform, "system", return_value="Darwin"), patch.object(
            portal_fill.Path, "exists", return_value=True
        ), patch.object(portal_fill.subprocess, "Popen") as popen:
            result = portal_fill.open_chrome_remote_debugging_settings()
        self.assertTrue(result["ok"])
        args = popen.call_args.args[0]
        self.assertEqual(args[0:2], ["open", "-a"])
        self.assertEqual(args[-1], "chrome://inspect/#remote-debugging")
        self.assertNotIn("--user-data-dir", args)

    def test_macos_chrome_profile_permission_failure_has_specific_fix(self):
        guidance = portal_fill._macos_chrome_profile_access_guidance(
            "Could not connect to Chrome. Cause: Could not read DevToolsActivePort for chrome at "
            "/Users/josh/Library/Application Support/Google/Chrome/DevToolsActivePort: permission denied; EPERM"
        )
        self.assertIsNotNone(guidance)
        self.assertIn("Privacy & Security > Files & Folders", guidance)
        self.assertIn("allow Google Chrome", guidance)
        self.assertIsNone(portal_fill._macos_chrome_profile_access_guidance("Chrome remote debugging is not enabled"))


if __name__ == "__main__":
    unittest.main()
