"""Local, review-first browser handoff for employer application forms.

The browser is a dedicated Neural Ant profile. It is visible to the user and is
never used to submit an application. Only blank, plainly labeled fields with a
direct match in the saved profile are populated.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import queue
import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit


_RUNS: dict[str, dict[str, Any]] = {}
_RUNS_LOCK = threading.Lock()
_TASKS: queue.Queue[tuple[str, str, dict[str, Any]] | None] = queue.Queue()
_WORKER: threading.Thread | None = None
_MAX_RUNS = 100
_RUN_PRIVATE: dict[str, dict[str, Any]] = {}
_ATS_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com", "workable.com",
    "myworkdayjobs.com", "workday.com", "icims.com", "jobvite.com", "successfactors.com",
    "taleo.net", "oraclecloud.com", "dayforcehcm.com", "paylocity.com", "bamboohr.com",
    "recruitee.com", "breezy.hr", "jazz.co", "personio.com", "personio.de", "jobscore.com",
)

_SENSITIVE_LABEL = re.compile(
    r"\b(visa|sponsor|sponsorship|work authorization|work authori[sz]ation|"
    r"immigration|h.?1b|opt|stem opt|citizenship|legally authorized|"
    r"race|ethnic|gender|sex|sexual orientation|pronoun|age|date of birth|birth date|"
    r"disability|veteran|military|self.?identif|voluntary disclosure|"
    r"demographic|eeo|equal employment|salary|compensation|pay expectation|"
    r"attest|certif(?:y|ication)|signature|consent|privacy agreement|terms|"
    r"captcha|robot|human verification)\b",
    re.I,
)


def is_public_https_url(value: str) -> bool:
    """Reject non-HTTPS, local, and private-network destinations."""
    try:
        parsed = urlsplit(str(value or ""))
        hostname = (parsed.hostname or "").rstrip(".").casefold()
        if parsed.scheme.casefold() != "https" or not hostname or parsed.username or parsed.password:
            return False
        if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".local", ".internal", ".test", ".invalid")):
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address and (not address.is_global):
            return False
        return "." in hostname
    except (ValueError, TypeError):
        return False


def _allowed_application_host(posting_url: str, form_url: str) -> bool:
    """Accept the posting's own site or a recognized ATS, not arbitrary redirects."""
    try:
        posting_host = (urlsplit(posting_url).hostname or "").casefold().rstrip(".")
        form_host = (urlsplit(form_url).hostname or "").casefold().rstrip(".")
        if not posting_host or not form_host:
            return False
        if posting_host == form_host or posting_host.endswith("." + form_host) or form_host.endswith("." + posting_host):
            return True
        multi_label_suffixes = {"co.uk", "com.au", "co.nz", "com.br", "co.in", "co.jp", "com.sg"}
        def registrable(host: str) -> str:
            labels = host.split(".")
            suffix = ".".join(labels[-2:])
            take = 3 if suffix in multi_label_suffixes and len(labels) >= 3 else 2
            return ".".join(labels[-take:])
        if registrable(posting_host) == registrable(form_host):
            return True
        return any(form_host == suffix or form_host.endswith("." + suffix) for suffix in _ATS_HOSTS)
    except ValueError:
        return False


def _field_value(descriptor: dict[str, Any], profile: dict[str, Any]) -> tuple[str, str] | None:
    """Return (value, source key) only for high-confidence profile matches."""
    label = " ".join(str(descriptor.get(key) or "") for key in ("label", "name", "placeholder", "autocomplete", "id"))
    label = re.sub(r"[_-]+", " ", label).casefold().strip()
    if not label or _SENSITIVE_LABEL.search(label):
        return None

    def value(key: str, transform=lambda item: str(item or "").strip()):
        raw = profile.get(key)
        if raw in (None, "", [], {}):
            return None
        result = transform(raw)
        return (result, key) if result else None

    auto = str(descriptor.get("autocomplete") or "").casefold()
    name = str(profile.get("preferred_name") or profile.get("name") or "").strip()
    name_parts = name.split()
    if re.search(r"\b(legal name|full legal name)\b", label):
        legal_name = str(profile.get("name") or name).strip()
        return (legal_name, "name") if legal_name else None
    if auto == "given-name" or re.search(r"\b(first|given)\s+name\b", label):
        return (name_parts[0], "name") if name_parts else None
    if auto == "family-name" or re.search(r"\b(last|family|sur)\s*name\b", label):
        return (name_parts[-1], "name") if len(name_parts) > 1 else None
    if auto == "additional-name" or "middle name" in label:
        return (" ".join(name_parts[1:-1]), "name") if len(name_parts) > 2 else None
    if auto == "name" or re.search(r"\b(full name|candidate name|your name)\b", label) or label.strip() in {"name", "name *"}:
        return (name, "name") if name else None
    if auto == "email" or re.search(r"\b(e.?mail address|email)\b", label):
        return value("email")
    if auto == "tel" or re.search(r"\b(phone|telephone|mobile number|mobile phone)\b", label):
        return value("phone")
    if auto == "address-line1" or re.search(r"\b(street address|home address|address line 1|address 1|street)\b", label) or label.strip() == "address":
        return value("address_line1")
    if auto == "address-line2" or re.search(r"\b(address line 2|address 2|apartment|suite|unit)\b", label):
        return value("address_line2")
    if auto == "address-level2" or re.search(r"\b(city|town)\b", label):
        return value("city")
    if auto == "address-level1" or re.search(r"\b(state|province|region)\b", label):
        return value("state")
    if auto == "postal-code" or re.search(r"\b(zip(?: code)?|postal code)\b", label):
        return value("postal_code")
    if auto == "country-name" or re.search(r"\bcountry\b", label):
        return value("country")
    if re.search(r"\blinked.?in\b", label):
        return value("linkedin_url")
    if re.search(r"\bgithub\b", label):
        return value("github_url")
    if re.search(r"\b(portfolio|personal website|website|web site)\b", label):
        return value("portfolio_url", lambda item: str(item or profile.get("website_url") or "").strip())
    if re.search(r"\b(current employer|current company|employer name|company you currently work for)\b", label):
        return value("current_company")
    if re.search(r"\b(current job title|current title|current position title|present job title)\b", label):
        return value("role")
    if re.search(r"\byears? of (relevant )?experience\b", label):
        return value("years_experience")
    if re.search(r"\b(school|university|college|institution)\b", label):
        history = profile.get("education_history") or []
        if history and isinstance(history[0], dict):
            school = str(history[0].get("school") or "").strip()
            return (school, "education_history") if school else None
    if re.search(r"\b(field of study|major|academic discipline)\b", label):
        history = profile.get("education_history") or []
        if history and isinstance(history[0], dict):
            field = str(history[0].get("field") or "").strip()
            return (field, "education_history") if field else None
    if re.search(r"\b(degree|education level)\b", label):
        history = profile.get("education_history") or []
        if history and isinstance(history[0], dict):
            degree = str(history[0].get("degree") or "").strip()
            return (degree, "education_history") if degree else None
        return value("education")
    if re.search(r"\b(education history|academic history)\b", label):
        def education_text(items: Any) -> str:
            if not isinstance(items, list):
                return str(items or "").strip()
            return "\n".join(
                " — ".join(str(part).strip() for part in (
                    item.get("degree"), item.get("field"), item.get("school"), item.get("start_date"), item.get("end_date")
                ) if str(part or "").strip())
                for item in items if isinstance(item, dict)
            )
        return value("education_history", education_text)
    if re.search(r"\b(skills?|skill set|technical skills|technologies)\b", label):
        return value("skills", lambda items: ", ".join(str(item) for item in items) if isinstance(items, list) else str(items).strip())
    if re.search(r"\b(employment history|work history|professional experience|employment experience)\b", label):
        def employment_text(items: Any) -> str:
            if not isinstance(items, list):
                return str(items or "").strip()
            rows = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                heading = " — ".join(str(item.get(key) or "").strip() for key in ("title", "company") if str(item.get(key) or "").strip())
                dates = " – ".join(str(item.get(key) or "").strip() for key in ("start_date", "end_date") if str(item.get(key) or "").strip())
                detail = ". ".join(str(item.get(key) or "").strip() for key in ("location", "highlights") if str(item.get(key) or "").strip())
                rows.append(" · ".join(part for part in (heading, dates, detail) if part))
            return "\n".join(rows)
        return value("employment_history", employment_text)
    if re.search(r"\b(certifications?|licenses?)\b", label):
        return value("certifications", lambda items: ", ".join(str(item) for item in items) if isinstance(items, list) else str(items).strip())
    if re.search(r"\b(languages? spoken|languages? known|languages?)\b", label):
        def language_text(items: Any) -> str:
            if not isinstance(items, list):
                return str(items or "").strip()
            return ", ".join(
                " — ".join(str(part).strip() for part in item.values() if str(part or "").strip())
                if isinstance(item, dict) else str(item).strip()
                for item in items if item
            )
        return value("languages", language_text)
    if re.search(r"\b(professional summary|about you|tell us about yourself)\b", label):
        return value("summary")
    return None


def _describe_form(page) -> list[dict[str, Any]]:
    return page.locator("input:not([type=file]), textarea, select").evaluate_all("""elements => elements.map((el, index) => {
      const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      const labels = Array.from(el.labels || []).map(node => (node.innerText || node.textContent || '').trim()).filter(Boolean);
      const parentLabel = el.closest('label');
      if (parentLabel) labels.push((parentLabel.innerText || parentLabel.textContent || '').trim());
      const fieldset = el.closest('fieldset');
      const legend = fieldset?.querySelector('legend')?.innerText || '';
      return {
        index, tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(), visible,
        label: [...new Set([...labels, legend])].filter(Boolean).join(' '),
        name: el.getAttribute('name') || '', id: el.id || '', placeholder: el.getAttribute('placeholder') || '',
        autocomplete: el.getAttribute('autocomplete') || '', required: !!el.required,
        value: el.value || '', options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => ({label:o.textContent.trim(), value:o.value})) : []
      };
    })""")


def _page_frames(page) -> list[Any]:
    try:
        return list(page.frames)
    except Exception:
        return [page]


def _has_application_fields(page) -> bool:
    for frame in _page_frames(page):
        try:
            if frame != page.main_frame and frame.url != "about:blank" and not _allowed_application_host(page.url, frame.url):
                continue
            if frame.locator('input[type="file"]').count() > 0:
                return True
            fields = _describe_form(frame)
            if any(_field_value(item, {"name": "x", "email": "x", "phone": "x"}) for item in fields):
                return True
        except Exception:
            continue
    return False


def _page_has_auth_gate(page) -> bool:
    try:
        if any(
            frame.locator('input[type="password"]').count() > 0
            for frame in _page_frames(page)
            if frame == page.main_frame or frame.url == "about:blank" or _allowed_application_host(page.url, frame.url)
        ):
            return True
        url = page.url.casefold()
        title = page.title().casefold()
        return any(term in url or term in title for term in ("/login", "sign-in", "signin", "log in", "login"))
    except Exception:
        return False


def _page_needs_human_verification(page) -> bool:
    try:
        chunks = [page.title(), page.locator("body").inner_text(timeout=1200)[:12000]]
        for frame in _page_frames(page):
            if frame != page.main_frame:
                chunks.append(frame.locator("body").inner_text(timeout=600)[:6000])
        text = " ".join(chunks).casefold()
        return any(term in text for term in ("captcha", "i'm not a robot", "verify you are human", "human verification", "security check"))
    except Exception:
        return False


def _file_accepts_path(accept: str, path: str) -> bool:
    accept = str(accept or "").casefold().replace(" ", "")
    if not accept:
        return True
    suffix = Path(path).suffix.casefold()
    if suffix in accept:
        return True
    if suffix == ".docx" and "wordprocessingml.document" in accept:
        return True
    if suffix == ".pdf" and "application/pdf" in accept:
        return True
    return False


def _maybe_open_application(page, context):
    """Follow one direct Apply link; never click a form button or submit control."""
    try:
        if _has_application_fields(page):
            return page, ""
        matches = page.locator("a[href]").evaluate_all("""els => els.map((el, index) => {
          const text = (el.innerText || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
          const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const href = el.getAttribute('href') || '';
          return {index, text, href, visible, inForm: !!el.closest('form')};
        }).filter(x => x.visible && !x.inForm && x.href && !/^(#|javascript:|mailto:)/i.test(x.href) && /^(apply|apply now|apply online|apply for this job|apply to this job|start application)$/i.test(x.text))""")
        if len(matches) != 1:
            return page, "No single direct Apply link was found. The posting is open; open its application form in Chrome, then choose Fill after sign-in in Neural Ant."
        destination = urljoin(page.url, matches[0]["href"])
        if not is_public_https_url(destination):
            return page, "The Apply link was not a safe public HTTPS address. Open the application form yourself, then choose Fill after sign-in."
        if not _allowed_application_host(page.url, destination):
            return page, "The Apply link opens an unrecognized portal. Review the site in Chrome; Neural Ant left your profile and résumé unshared."
        page.goto(destination, wait_until="domcontentloaded", timeout=30000)
        if not is_public_https_url(page.url):
            return page, "The application link redirected to a non-public address. Review it in Chrome before continuing."
        return page, ""
    except Exception:
        return page, "The posting opened, but Neural Ant could not identify its application form. Open the form in Chrome and choose Fill after sign-in."


def fill_application_page(page, profile: dict[str, Any], resume_path: str, cover_letter_path: str = "", posting_url: str = "") -> dict[str, Any]:
    """Fill blank, supported fields and upload available tailored documents."""
    if posting_url and (not is_public_https_url(page.url) or not _allowed_application_host(posting_url, page.url)):
        return {"status": "unrecognized_portal", "filled": [], "uploaded": [], "needs_user": ["Review the employer portal host before entering personal details."], "message": "The page is on a different, unrecognized host. Neural Ant left your saved details and résumé unshared."}
    if _page_has_auth_gate(page):
        return {"status": "sign_in_required", "filled": [], "uploaded": [], "needs_user": ["Sign in to the employer portal, then return and choose Fill after sign-in."], "message": "Sign in in the visible Chrome window, then continue here."}

    filled: list[str] = []
    needs_user: list[str] = []
    frames = _page_frames(page)
    for frame in frames:
        try:
            if frame != page.main_frame and frame.url != "about:blank" and not _allowed_application_host(page.url, frame.url):
                continue
            fields = _describe_form(frame)
        except Exception:
            continue
        for item in fields:
            if not item.get("visible") or item.get("type") in {"hidden", "password", "checkbox", "radio", "submit", "button", "reset", "image"}:
                continue
            if item.get("value"):
                continue
            match = _field_value(item, profile)
            if not match:
                if item.get("required") and item.get("label"):
                    label = str(item["label"]).strip()
                    needs_user.append("Review required field: " + label[:120])
                continue
            value, source = match
            locator = frame.locator("input:not([type=file]), textarea, select").nth(int(item["index"]))
            try:
                if item["tag"] == "select":
                    options = item.get("options") or []
                    option = next((entry for entry in options if str(entry.get("label", "")).casefold().strip() == value.casefold().strip()), None)
                    if option is None:
                        option = next((entry for entry in options if str(entry.get("value", "")).casefold().strip() == value.casefold().strip()), None)
                    if option:
                        locator.select_option(value=str(option["value"]))
                        filled.append(str(item.get("label") or source)[:80])
                else:
                    locator.fill(value)
                    filled.append(str(item.get("label") or source)[:80])
            except Exception:
                if item.get("required") and item.get("label"):
                    needs_user.append("Review required field: " + str(item["label"])[:120])

    uploaded: list[str] = []
    try:
        files = []
        for frame in frames:
            if frame != page.main_frame and frame.url != "about:blank" and not _allowed_application_host(page.url, frame.url):
                continue
            for entry in frame.locator('input[type="file"]').evaluate_all("""els => els.map((el, index) => {
              const labels = Array.from(el.labels || []).map(node => (node.innerText || node.textContent || '').trim());
              const parent = el.closest('label'); if (parent) labels.push((parent.innerText || parent.textContent || '').trim());
              return {index, label:[...labels, el.name || '', el.id || '', el.getAttribute('aria-label') || ''].join(' '), accept:el.accept || ''};
            })"""):
                files.append({**entry, "frame": frame})
        resume_inputs = [entry for entry in files if re.search(r"\b(resume|résumé|cv|curriculum vitae)\b", entry["label"], re.I)]
        cover_inputs = [entry for entry in files if re.search(r"\b(cover letter|covering letter)\b", entry["label"], re.I)]
        if len(resume_inputs) == 1 and Path(resume_path).is_file():
            if _file_accepts_path(resume_inputs[0].get("accept", ""), resume_path):
                resume_inputs[0]["frame"].locator('input[type="file"]').nth(int(resume_inputs[0]["index"])).set_input_files(resume_path)
                uploaded.append("tailored resume")
            else:
                needs_user.append("The resume upload accepts a different file format; attach an accepted version manually.")
        elif len(files) == 1 and not resume_inputs:
            needs_user.append("Check the unlabeled file upload; Neural Ant left it unchanged.")
        else:
            needs_user.append("Resume upload field was not identified; attach the tailored resume manually if needed.")
        if len(cover_inputs) == 1 and cover_letter_path and Path(cover_letter_path).is_file():
            if _file_accepts_path(cover_inputs[0].get("accept", ""), cover_letter_path):
                cover_inputs[0]["frame"].locator('input[type="file"]').nth(int(cover_inputs[0]["index"])).set_input_files(cover_letter_path)
                uploaded.append("tailored cover letter")
            else:
                needs_user.append("The cover-letter upload accepts a different file format; attach an accepted version manually.")
    except Exception:
        needs_user.append("Check document uploads in the employer form.")

    human_check = _page_needs_human_verification(page)
    if human_check:
        needs_user.append("Complete the human verification in Chrome.")
    return {
        "status": "human_verification_required" if human_check else "ready",
        "filled": list(dict.fromkeys(filled)),
        "uploaded": uploaded,
        "needs_user": list(dict.fromkeys(needs_user)),
        "message": "Review the open employer form. Neural Ant will not submit it.",
    }


def _browser_profile_dir() -> Path:
    configured = os.getenv("NEURAL_ANT_BROWSER_PROFILE", "").strip()
    path = Path(configured).expanduser() if configured else Path.home() / ".neural-ant" / "portal-browser"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _launch_context(playwright):
    profile_dir = _browser_profile_dir()
    options = {"headless": False, "accept_downloads": False, "viewport": {"width": 1365, "height": 900}}
    system = platform.system()
    if system == "Darwin":
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        user_chrome = Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if chrome.is_file() or user_chrome.is_file():
            options["channel"] = "chrome"
    elif system == "Windows":
        options["channel"] = "chrome"
    else:
        chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        chromium = shutil.which("chromium") or shutil.which("chromium-browser")
        if chrome:
            options["channel"] = "chrome"
        elif chromium:
            options["executable_path"] = chromium
    return playwright.chromium.launch_persistent_context(str(profile_dir), **options)


def _set_run(run_id: str, **updates) -> None:
    with _RUNS_LOCK:
        if run_id in _RUNS:
            _RUNS[run_id].update(updates)


def _worker_main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        while True:
            task = _TASKS.get()
            if task is None:
                return
            _set_run(task[0], status="failed", message="Browser automation is not installed. Restart Neural Ant using run-local.sh.")
            with _RUNS_LOCK:
                private = _RUN_PRIVATE.pop(task[0], None)
            if private:
                shutil.rmtree(private.get("temp_dir", ""), ignore_errors=True)

    pages: dict[str, Any] = {}
    contexts = None
    try:
        playwright = sync_playwright().start()
    except Exception:
        while True:
            task = _TASKS.get()
            if task is None:
                return
            _set_run(task[0], status="failed", message="Could not start the local browser. Restart Neural Ant and try again.")
            with _RUNS_LOCK:
                private = _RUN_PRIVATE.pop(task[0], None)
            if private:
                shutil.rmtree(private.get("temp_dir", ""), ignore_errors=True)
    
    try:
        while True:
            task = _TASKS.get()
            if task is None:
                break
            run_id, action, payload = task
            try:
                if action == "forget":
                    for page in list(pages.values()):
                        try:
                            page.close()
                        except Exception:
                            pass
                    pages.clear()
                    if contexts:
                        contexts.close()
                        contexts = None
                    with _RUNS_LOCK:
                        staged = list(_RUN_PRIVATE.values())
                        _RUN_PRIVATE.clear()
                    for item in staged:
                        shutil.rmtree(item.get("temp_dir", ""), ignore_errors=True)
                    shutil.rmtree(_browser_profile_dir(), ignore_errors=True)
                    _set_run(run_id, status="ready", message="Saved employer sign-ins were removed from this device.")
                    continue

                if contexts is None:
                    contexts = _launch_context(playwright)
                    contexts.set_default_timeout(2500)
                if action == "open":
                    _set_run(run_id, status="opening", message="Opening the employer form in Chrome.")
                    page = contexts.new_page()
                    pages[run_id] = page
                    page.on("popup", lambda popup, key=run_id: pages.__setitem__(key, popup))
                    page.goto(payload["url"], wait_until="domcontentloaded", timeout=30000)
                    if not is_public_https_url(page.url):
                        raise RuntimeError("The posting redirected to a non-public HTTPS address.")
                    try:
                        page.wait_for_timeout(700)
                    except Exception:
                        pass
                    nav_note = ""
                    if not _page_has_auth_gate(page):
                        page, nav_note = _maybe_open_application(page, contexts)
                        pages[run_id] = page
                    result = fill_application_page(page, payload["profile"], payload["resume_path"], payload.get("cover_letter_path", ""), payload["url"])
                    if nav_note:
                        result["needs_user"].append(nav_note)
                        result["status"] = "needs_manual_navigation"
                    if result["status"] not in {"sign_in_required", "needs_manual_navigation"}:
                        try:
                            os.unlink(payload["resume_path"])
                        except OSError:
                            pass
                        if payload.get("cover_letter_path"):
                            try:
                                os.unlink(payload["cover_letter_path"])
                            except OSError:
                                pass
                        with _RUNS_LOCK:
                            _RUN_PRIVATE.pop(run_id, None)
                        shutil.rmtree(payload.get("temp_dir", ""), ignore_errors=True)
                    _set_run(run_id, **result, url=page.url, host=urlsplit(page.url).hostname or "")
                elif action == "continue":
                    page = pages.get(run_id)
                    if page is None or page.is_closed():
                        raise RuntimeError("The review tab is no longer open. Prepare this application again to reopen it.")
                    result = fill_application_page(page, payload["profile"], payload["resume_path"], payload.get("cover_letter_path", ""), payload["url"])
                    if result["status"] != "sign_in_required":
                        for key in ("resume_path", "cover_letter_path"):
                            try:
                                if payload.get(key):
                                    os.unlink(payload[key])
                            except OSError:
                                pass
                        with _RUNS_LOCK:
                            _RUN_PRIVATE.pop(run_id, None)
                        shutil.rmtree(payload.get("temp_dir", ""), ignore_errors=True)
                    _set_run(run_id, **result, url=page.url, host=urlsplit(page.url).hostname or "")
            except Exception as exc:
                message = str(exc).splitlines()[0][:240] or "Could not open the application form."
                if "Executable doesn't exist" in message or "browserType.launch" in message:
                    message = "No supported browser was found. Run Neural Ant with Chrome installed or install Playwright Chromium using run-local.sh."
                with _RUNS_LOCK:
                    private = _RUN_PRIVATE.pop(run_id, None)
                if private:
                    shutil.rmtree(private.get("temp_dir", ""), ignore_errors=True)
                _set_run(run_id, status="failed", message=message)
    finally:
        if contexts:
            try:
                contexts.close()
            except Exception:
                pass
        with _RUNS_LOCK:
            staged = list(_RUN_PRIVATE.values())
            _RUN_PRIVATE.clear()
        for item in staged:
            shutil.rmtree(item.get("temp_dir", ""), ignore_errors=True)
        try:
            playwright.stop()
        except Exception:
            pass


def _ensure_worker() -> None:
    global _WORKER
    if _WORKER is None or not _WORKER.is_alive():
        _WORKER = threading.Thread(target=_worker_main, name="neural-ant-portal-browser", daemon=True)
        _WORKER.start()


def shutdown_portal_browser_worker() -> None:
    if _WORKER and _WORKER.is_alive():
        _TASKS.put(None)
        _WORKER.join(timeout=8)


def start_portal_run(*, application_id: int, job: Any, profile: dict[str, Any], resume_bytes: bytes, resume_filename: str, cover_letter_bytes: bytes = b"", cover_letter_filename: str = "") -> dict[str, Any]:
    """Stage generated files locally and enqueue a visible Chrome form handoff."""
    run_id = uuid.uuid4().hex
    temp_dir = Path(tempfile.mkdtemp(prefix="neural-ant-portal-"))
    try:
        temp_dir.chmod(0o700)
    except OSError:
        pass
    resume_path = temp_dir / Path(resume_filename).name
    resume_path.write_bytes(resume_bytes)
    try:
        resume_path.chmod(0o600)
    except OSError:
        pass
    cover_path = ""
    if cover_letter_bytes and cover_letter_filename:
        cover_path_obj = temp_dir / Path(cover_letter_filename).name
        cover_path_obj.write_bytes(cover_letter_bytes)
        try:
            cover_path_obj.chmod(0o600)
        except OSError:
            pass
        cover_path = str(cover_path_obj)
    parsed = urlsplit(str(job.url or ""))
    with _RUNS_LOCK:
        if len(_RUNS) >= _MAX_RUNS:
            for key in list(_RUNS):
                if _RUNS[key].get("status") not in {"queued", "opening"}:
                    _RUNS.pop(key, None)
                    if len(_RUNS) < _MAX_RUNS:
                        break
        _RUNS[run_id] = {
            "run_id": run_id,
            "application_id": application_id,
            "company": str(job.company_name or ""),
            "title": str(job.title or ""),
            "host": parsed.hostname or "",
            "status": "queued",
            "filled": [],
            "uploaded": [],
            "needs_user": [],
            "message": "Opening the employer form in a separate Chrome tab.",
            "url": str(job.url or ""),
        }
    payload = {
        "url": str(job.url or ""),
        "profile": json.loads(json.dumps(profile)),
        "resume_path": str(resume_path),
        "cover_letter_path": cover_path,
        "temp_dir": str(temp_dir),
    }
    with _RUNS_LOCK:
        _RUN_PRIVATE[run_id] = payload
    _ensure_worker()
    _TASKS.put((run_id, "open", payload))
    return get_portal_run(run_id) or {}


def continue_portal_run(run_id: str) -> dict[str, Any] | None:
    with _RUNS_LOCK:
        current = _RUNS.get(run_id)
        if not current:
            return None
        if current.get("status") not in {"sign_in_required", "human_verification_required", "ready", "needs_manual_navigation"}:
            return dict(current)
        current.update(status="queued", message="Continuing form preparation in Chrome.")
    private = _RUN_PRIVATE.get(run_id)
    if not private:
        return get_portal_run(run_id)
    _ensure_worker()
    _TASKS.put((run_id, "continue", private))
    return get_portal_run(run_id)

def get_portal_run(run_id: str) -> dict[str, Any] | None:
    with _RUNS_LOCK:
        current = _RUNS.get(run_id)
        return dict(current) if current else None


def forget_portal_sessions() -> dict[str, Any]:
    run_id = "forget-" + uuid.uuid4().hex
    with _RUNS_LOCK:
        _RUNS[run_id] = {"run_id": run_id, "status": "queued", "message": "Removing saved browser sign-ins."}
    _ensure_worker()
    _TASKS.put((run_id, "forget", {}))
    return get_portal_run(run_id) or {}


