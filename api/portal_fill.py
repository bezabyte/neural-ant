"""Local, review-first handoff through the user's already-running Chrome.

Chrome's explicit remote-debugging consent connects Neural Ant to the user's
regular signed-in Chrome session. Neural Ant never launches a second profile or
submits an application. Only blank, plainly labeled fields with a direct match
in the saved profile are populated.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
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
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHROME_MCP_BIN = _REPO_ROOT / "node_modules" / ".bin" / "chrome-devtools-mcp"
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


class ChromeConnectionError(RuntimeError):
    """Chrome is not yet available for an explicitly approved local session."""


def _macos_chrome_profile_access_guidance(error: str) -> str | None:
    """Explain the macOS Files & Folders grant needed by Chrome auto-connect."""
    lowered = str(error).casefold()
    profile_endpoint = "devtoolsactiveport" in lowered and (
        "google/chrome" in lowered or "application support/google/chrome" in lowered
    )
    denied = any(term in lowered for term in ("eperm", "operation not permitted", "permission denied"))
    if profile_endpoint and denied:
        return (
            "Chrome Remote Debugging is on, but macOS is blocking the Neural Ant host app from reading Chrome's local connection file. "
            "Open System Settings > Privacy & Security > Files & Folders, find Codex (the app running Neural Ant), and allow Google Chrome. "
            "Then retry. This is separate from Chrome's Remote Debugging switch."
        )
    return None


class _ChromeDevtoolsClient:
    """Minimal stdio MCP client for the official Chrome DevTools MCP server."""

    def __init__(self, on_wait=None, timeout: float = 90.0):
        if not _CHROME_MCP_BIN.is_file():
            raise ChromeConnectionError("Chrome control is not installed. Restart Neural Ant with run-local.sh.")
        command = [
            str(_CHROME_MCP_BIN), "--autoConnect", "--experimentalStructuredContent",
            "--no-usage-statistics", "--no-performance-crux",
            "--no-categoryPerformance", "--no-categoryNetwork", "--no-categoryMemory",
            "--no-categoryEmulation", "--no-categoryNetworkMocks", "--no-categoryWebauthn",
        ]
        self.process = subprocess.Popen(
            command,
            cwd=str(_REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._errors: queue.Queue[str] = queue.Queue()
        self._next_id = 0
        self._write_lock = threading.Lock()
        threading.Thread(target=self._read_stdout, name="neural-ant-chrome-mcp-out", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="neural-ant-chrome-mcp-err", daemon=True).start()
        try:
            response_id = self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "neural-ant", "version": "0.1.0"},
            })
            response = self._wait_response(response_id, timeout, on_wait=on_wait)
            if response.get("error"):
                raise ChromeConnectionError(self._error_text(response))
            self._notify("notifications/initialized", {})
            listed = self.call("tools/list", {}, timeout=20)
            tools = {item.get("name") for item in listed.get("tools", [])}
            required = {"list_pages", "new_page", "navigate_page", "evaluate_script", "take_snapshot", "upload_file"}
            if not required.issubset(tools):
                raise ChromeConnectionError("Chrome connected, but the form-filling tools are unavailable. Restart Neural Ant and try again.")
        except Exception:
            self.close()
            raise

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._errors.put(line.strip()[:500])
                    continue
                if isinstance(message, dict):
                    self._messages.put(message)
        finally:
            self._messages.put(None)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            cleaned = line.strip()
            if cleaned:
                self._errors.put(cleaned[:500])

    def _send_request(self, method: str, params: dict[str, Any]) -> int:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        assert self.process.stdin is not None
        with self._write_lock:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        return self._next_id

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        with self._write_lock:
            self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, separators=(",", ":")) + "\n")
            self.process.stdin.flush()

    @staticmethod
    def _error_text(response: dict[str, Any]) -> str:
        error = response.get("error") or {}
        return str(error.get("message") or "Chrome refused the remote-debugging connection.")[:400]

    def _wait_response(self, response_id: int, timeout: float, on_wait=None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_update = 0.0
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                detail = self._recent_error()
                raise ChromeConnectionError(detail or "Chrome remote debugging is not enabled in your regular Chrome profile.")
            try:
                message = self._messages.get(timeout=min(0.5, max(0.05, deadline - time.monotonic())))
            except queue.Empty:
                message = {}
            if message is None:
                detail = self._recent_error()
                raise ChromeConnectionError(detail or "Chrome remote debugging is not enabled in your regular Chrome profile.")
            if message.get("id") == response_id:
                return message
            if on_wait and time.monotonic() - last_update >= 3:
                on_wait()
                last_update = time.monotonic()
        raise ChromeConnectionError("Timed out waiting for Chrome. Enable Remote Debugging in your regular Chrome, then approve Neural Ant's access prompt.")

    def _recent_error(self) -> str:
        lines = []
        while True:
            try:
                lines.append(self._errors.get_nowait())
            except queue.Empty:
                break
        message = " ".join(lines[-4:]).strip()
        if any(term in message.casefold() for term in ("remote debugging", "auto-connect", "autoconnect", "permission", "consent", "connect")):
            return message[:400]
        return ""

    def call(self, method: str, params: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        response_id = self._send_request(method, params)
        response = self._wait_response(response_id, timeout)
        if response.get("error"):
            raise RuntimeError(self._error_text(response))
        return response.get("result") or {}

    def tool(self, name: str, arguments: dict[str, Any] | None = None, timeout: float = 30) -> dict[str, Any]:
        response = self.call("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
        if response.get("isError"):
            texts = [part.get("text", "") for part in response.get("content", []) if part.get("type") == "text"]
            raise RuntimeError(" ".join(texts).strip()[:500] or f"Chrome DevTools tool {name} failed.")
        return response

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                if stream and not stream.closed:
                    stream.close()
            except OSError:
                pass


def open_chrome_remote_debugging_settings() -> dict[str, str]:
    """Open Chrome's opt-in page in its normal profile without launching another profile."""
    url = "chrome://inspect/#remote-debugging"
    if platform.system() == "Darwin":
        app = Path("/Applications/Google Chrome.app")
        user_app = Path.home() / "Applications/Google Chrome.app"
        if app.exists():
            subprocess.Popen(["open", "-a", str(app), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif user_app.exists():
            subprocess.Popen(["open", "-a", str(user_app), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            raise ChromeConnectionError("Google Chrome was not found on this Mac.")
    elif platform.system() == "Windows":
        os.startfile(url)  # type: ignore[attr-defined]
    else:
        browser = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        if not browser:
            raise ChromeConnectionError("Google Chrome was not found on this device.")
        subprocess.Popen([browser, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "message": "Opened Chrome's Remote Debugging settings in your regular Chrome window."}


def _mcp_content_text(response: dict[str, Any]) -> str:
    return "\n".join(
        str(item.get("text", ""))
        for item in response.get("content", [])
        if item.get("type") == "text"
    )


def _mcp_script_value(response: dict[str, Any]) -> Any:
    text = _mcp_content_text(response)
    match = re.search(r"```json\s*(.*?)\s*```", text, re.S)
    if not match:
        raise RuntimeError("Chrome could not read the application form.")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Chrome returned an unreadable form description.") from exc


def _mcp_pages(response: dict[str, Any]) -> list[dict[str, Any]]:
    structured = response.get("structuredContent") or {}
    pages = structured.get("pages") if isinstance(structured, dict) else None
    return pages if isinstance(pages, list) else []


def _mcp_page_state(client: _ChromeDevtoolsClient, page_id: int) -> dict[str, Any]:
    result = client.tool("evaluate_script", {
        "pageId": page_id,
        "waitForStableDom": False,
        "function": r"""() => {
          const clean = value => (value || '').replace(/\s+/g, ' ').trim();
          const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const labelsFor = el => {
            const labels = Array.from(el.labels || []).map(node => clean(node.innerText || node.textContent));
            const parent = el.closest('label');
            if (parent) labels.push(clean(parent.innerText || parent.textContent));
            const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
              .map(id => document.getElementById(id)).filter(Boolean).map(node => clean(node.innerText || node.textContent));
            const aria = el.getAttribute('aria-label') || '';
            const fieldset = el.closest('fieldset');
            const legend = fieldset?.querySelector('legend');
            if (legend) labels.push(clean(legend.innerText || legend.textContent));
            return [...new Set([...labels, ...labelledBy, aria].filter(Boolean))].join(' ');
          };
          const controls = Array.from(document.querySelectorAll('input:not([type=file]), textarea, select'));
          const fields = controls.map((el, index) => ({
            index, tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(), visible: visible(el),
            label: labelsFor(el), name: el.getAttribute('name') || '', id: el.id || '',
            placeholder: el.getAttribute('placeholder') || '', autocomplete: el.getAttribute('autocomplete') || '',
            required: !!el.required || el.getAttribute('aria-required') === 'true',
            disabled: !!el.disabled || !!el.readOnly, value: el.value || '',
            options: el.tagName === 'SELECT' ? Array.from(el.options).map(option => ({label: clean(option.textContent), value: option.value})) : []
          }));
          const files = Array.from(document.querySelectorAll('input[type=file]')).map((el, index) => ({
            index, label: [labelsFor(el), el.name || '', el.id || '', el.getAttribute('data-testid') || ''].filter(Boolean).join(' '),
            accept: el.accept || '', visible: visible(el), disabled: !!el.disabled
          }));
          const links = Array.from(document.querySelectorAll('a[href]')).map(el => ({
            text: clean(el.innerText || el.getAttribute('aria-label')), href: el.href,
            visible: visible(el), inForm: !!el.closest('form')
          })).filter(el => el.visible && !el.inForm && /^(apply|apply now|apply online|apply for this job|apply to this job|start application)$/i.test(el.text));
          return {url: location.href, title: document.title, text: clean(document.body?.innerText).slice(0, 18000),
            passwordCount: document.querySelectorAll('input[type=password]').length, fields, files, links};
        }""",
    })
    value = _mcp_script_value(result)
    if not isinstance(value, dict):
        raise RuntimeError("Chrome returned an invalid application form description.")
    return value


def _mcp_apply_operations(client: _ChromeDevtoolsClient, page_id: int, operations: list[dict[str, Any]]) -> set[int]:
    if not operations:
        return set()
    script = r"""() => {
      const controls = Array.from(document.querySelectorAll('input:not([type=file]), textarea, select'));
      const operations = __OPS__;
      const completed = [];
      for (const operation of operations) {
        const element = controls[operation.index];
        if (!element || element.disabled || element.readOnly || !element.getClientRects().length || String(element.value || '').trim()) continue;
        let value = operation.value;
        if (element.tagName === 'SELECT') {
          const target = element.options.find(option => option.label.trim().toLowerCase() === String(value).trim().toLowerCase() || option.value.trim().toLowerCase() === String(value).trim().toLowerCase());
          if (!target) continue;
          value = target.value;
          Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(element, value);
        } else if (element.tagName === 'TEXTAREA') {
          Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(element, value);
        } else {
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(element, value);
        }
        element.dispatchEvent(new Event('input', {bubbles: true}));
        element.dispatchEvent(new Event('change', {bubbles: true}));
        completed.push(operation.index);
      }
      return completed;
    }""".replace("__OPS__", json.dumps(operations, ensure_ascii=True))
    response = client.tool("evaluate_script", {"pageId": page_id, "function": script, "waitForStableDom": False})
    result = _mcp_script_value(response)
    return {int(index) for index in result if isinstance(index, (int, float))} if isinstance(result, list) else set()


def _flatten_accessibility_nodes(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        found.append(node)
        for child in node.get("children", []) if isinstance(node.get("children"), list) else []:
            visit(child)
    visit(value)
    return found


def _upload_uid(client: _ChromeDevtoolsClient, page_id: int, target_label: str) -> str | None:
    snapshot = client.tool("take_snapshot", {"pageId": page_id, "verbose": True})
    structured = snapshot.get("structuredContent") or {}
    tree = structured.get("snapshot") if isinstance(structured, dict) else None
    nodes = _flatten_accessibility_nodes(tree.get("root") if isinstance(tree, dict) else tree)
    pattern = r"\b(resume|résumé|cv|curriculum vitae)\b" if "resume" in target_label.casefold() else r"\b(cover letter|covering letter)\b"
    candidates = []
    for node in nodes:
        role = str(node.get("role") or "").casefold()
        name = str(node.get("name") or "").strip()
        uid = str(node.get("id") or "").strip()
        if uid and name and role in {"button", "textbox", "generic", "group"} and re.search(pattern, name, re.I):
            candidates.append(uid)
    return candidates[0] if len(set(candidates)) == 1 else None


def _fill_connected_chrome_page(client: _ChromeDevtoolsClient, page_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    pages_response = client.tool("list_pages")
    page = next((item for item in _mcp_pages(pages_response) if int(item.get("id", -1)) == int(page_id)), None)
    if not page:
        raise RuntimeError("The application tab is no longer available in Chrome. Prepare the role again to reopen it.")
    page_url = str(page.get("url") or "")
    posting_url = str(payload["url"])
    if not is_public_https_url(page_url) or not _allowed_application_host(posting_url, page_url):
        return {"status": "unrecognized_portal", "filled": [], "uploaded": [], "needs_user": ["Review the employer portal host before entering personal details."], "message": "The page is on a different or unrecognized site. Neural Ant left your profile and résumé unshared."}

    state = _mcp_page_state(client, page_id)
    page_text = (str(state.get("url", "")) + " " + str(state.get("title", ""))).casefold()
    if state.get("passwordCount") or any(term in page_text for term in ("/login", "sign-in", "signin", "log in", "login")):
        return {"status": "sign_in_required", "filled": [], "uploaded": [], "needs_user": ["Sign in to the employer portal in your regular Chrome window, then continue here."], "message": "Sign in using your existing Chrome session, then continue here."}

    fields = [item for item in state.get("fields", []) if isinstance(item, dict)]
    files = [item for item in state.get("files", []) if isinstance(item, dict)]
    application_form_found = bool(files) or any(
        item.get("visible") and _field_value(item, payload["profile"]) is not None
        for item in fields
    )
    if not application_form_found:
        links = [item for item in state.get("links", []) if isinstance(item, dict)]
        if len(links) != 1:
            return {"status": "needs_manual_navigation", "filled": [], "uploaded": [], "needs_user": ["Open the application form in Chrome, then choose Continue here."], "message": "Neural Ant did not find one direct Apply link. Open the form in your regular Chrome window, then continue here."}
        destination = str(links[0].get("href") or "")
        if not is_public_https_url(destination) or not _allowed_application_host(page_url, destination):
            return {"status": "unrecognized_portal", "filled": [], "uploaded": [], "needs_user": ["Review the Apply link destination in Chrome."], "message": "The Apply link opens an unrecognized site. Neural Ant left your profile and résumé unshared."}
        client.tool("navigate_page", {"pageId": page_id, "type": "url", "url": destination, "timeout": 30000}, timeout=35)
        pages_response = client.tool("list_pages")
        page = next((item for item in _mcp_pages(pages_response) if int(item.get("id", -1)) == int(page_id)), None)
        page_url = str(page.get("url") or "") if page else ""
        if not is_public_https_url(page_url) or not _allowed_application_host(destination, page_url):
            return {"status": "unrecognized_portal", "filled": [], "uploaded": [], "needs_user": ["Review the redirected employer portal in Chrome."], "message": "The Apply link redirected to an unrecognized site. Neural Ant left your profile and résumé unshared."}
        state = _mcp_page_state(client, page_id)
        if state.get("passwordCount") or any(term in str(state.get("url", "")).casefold() for term in ("/login", "sign-in", "signin", "login")):
            return {"status": "sign_in_required", "filled": [], "uploaded": [], "needs_user": ["Sign in to the employer portal in your regular Chrome window, then continue here."], "message": "Sign in using your existing Chrome session, then continue here."}
        fields = [item for item in state.get("fields", []) if isinstance(item, dict)]
        files = [item for item in state.get("files", []) if isinstance(item, dict)]

    operations = []
    labels_by_index: dict[int, str] = {}
    needs_user: list[str] = []
    controls_to_skip = {"hidden", "password", "checkbox", "radio", "submit", "button", "reset", "image", "file"}
    for item in fields:
        if not item.get("visible") or item.get("disabled"):
            continue
        label = str(item.get("label") or "").strip()
        match = _field_value(item, payload["profile"])
        if match and not str(item.get("value") or "").strip() and str(item.get("type") or "").casefold() not in controls_to_skip:
            value, source = match
            if item.get("tag") == "select":
                options = item.get("options") or []
                exact = next((option for option in options if str(option.get("label", "")).casefold().strip() == value.casefold().strip()), None)
                exact = exact or next((option for option in options if str(option.get("value", "")).casefold().strip() == value.casefold().strip()), None)
                if exact is None:
                    if item.get("required") and label:
                        needs_user.append("Review required field: " + label[:120])
                    continue
            index = int(item.get("index", -1))
            if index >= 0:
                operations.append({"index": index, "value": value})
                labels_by_index[index] = (label or source)[:80]
        elif item.get("required") and label:
            needs_user.append("Review required field: " + label[:120])

    completed = _mcp_apply_operations(client, page_id, operations)
    filled = list(dict.fromkeys(labels_by_index[index] for index in sorted(completed) if index in labels_by_index))

    uploaded: list[str] = []
    resume_inputs = [item for item in files if re.search(r"\b(resume|résumé|cv|curriculum vitae)\b", str(item.get("label", "")), re.I)]
    cover_inputs = [item for item in files if re.search(r"\b(cover letter|covering letter)\b", str(item.get("label", "")), re.I)]
    for target, matches, path_key, title in (
        ("resume", resume_inputs, "resume_path", "tailored resume"),
        ("cover letter", cover_inputs, "cover_letter_path", "tailored cover letter"),
    ):
        path = str(payload.get(path_key) or "")
        if not path or not Path(path).is_file():
            if target == "resume":
                needs_user.append("Attach the tailored résumé manually if this form has an upload field.")
            continue
        if len(matches) != 1:
            if target == "resume":
                needs_user.append("Resume upload field was not identified; attach the tailored résumé manually if needed.")
            continue
        if not _file_accepts_path(str(matches[0].get("accept") or ""), path):
            needs_user.append(f"The {target} upload accepts another file format; attach an accepted version manually.")
            continue
        uid = _upload_uid(client, page_id, target)
        if not uid:
            needs_user.append(f"Neural Ant could not identify the {target} upload control; attach it manually.")
            continue
        client.tool("upload_file", {"pageId": page_id, "uid": uid, "filePaths": [path]}, timeout=35)
        uploaded.append(title)

    all_text = (str(state.get("title") or "") + " " + str(state.get("text") or "")).casefold()
    human_check = any(term in all_text for term in ("captcha", "i'm not a robot", "verify you are human", "human verification", "security check"))
    if human_check:
        needs_user.append("Complete the human verification in Chrome.")
    return {
        "status": "human_verification_required" if human_check else "ready",
        "filled": filled,
        "uploaded": uploaded,
        "needs_user": list(dict.fromkeys(needs_user)),
        "message": "Review the open application in your regular Chrome window. Neural Ant will not submit it.",
    }


def _set_run(run_id: str, **updates) -> None:
    with _RUNS_LOCK:
        if run_id in _RUNS:
            _RUNS[run_id].update(updates)


def _cleanup_staged_run(run_id: str, payload: dict[str, Any]) -> None:
    for key in ("resume_path", "cover_letter_path"):
        try:
            if payload.get(key):
                os.unlink(payload[key])
        except OSError:
            pass
    with _RUNS_LOCK:
        _RUN_PRIVATE.pop(run_id, None)
    shutil.rmtree(payload.get("temp_dir", ""), ignore_errors=True)


def _worker_main() -> None:
    client: _ChromeDevtoolsClient | None = None
    pages: dict[str, int] = {}
    try:
        while True:
            task = _TASKS.get()
            if task is None:
                break
            run_id, action, payload = task
            try:
                if action == "disconnect":
                    if client:
                        client.close()
                        client = None
                    pages.clear()
                    _set_run(run_id, status="disconnected", message="Neural Ant disconnected from Chrome. Your Chrome tabs and sign-ins are still open.")
                    continue

                if client is None:
                    _set_run(run_id, status="connecting_chrome", message="Connecting to your regular Chrome. Approve Neural Ant's access prompt if Chrome shows one.")
                    try:
                        client = _ChromeDevtoolsClient(on_wait=lambda: _set_run(
                            run_id,
                            status="connecting_chrome",
                            message="Waiting for Chrome's permission prompt. Approve Neural Ant in your regular Chrome window.",
                        ))
                    except ChromeConnectionError as exc:
                        _set_run(
                            run_id,
                            status="chrome_setup_required",
                            message=str(exc) or "Enable Remote Debugging in your regular Chrome, then retry.",
                            needs_user=["In Chrome, open chrome://inspect/#remote-debugging and enable Remote Debugging. Then approve Neural Ant's permission prompt."],
                        )
                        continue

                if action == "open" or (action == "continue" and run_id not in pages):
                    before = _mcp_pages(client.tool("list_pages"))
                    before_ids = {int(item.get("id", -1)) for item in before}
                    _set_run(run_id, status="connecting_chrome", message="Connecting to your regular Chrome and opening the employer form. Approve Neural Ant's access prompt if it appears.")
                    created = client.tool("new_page", {"url": payload["url"], "background": False, "timeout": 120000}, timeout=130)
                    candidates = _mcp_pages(created)
                    target = next((item for item in candidates if int(item.get("id", -1)) not in before_ids), None)
                    target = target or next((item for item in candidates if str(item.get("url") or "").startswith(payload["url"])), None)
                    if target is None:
                        latest = _mcp_pages(client.tool("list_pages"))
                        target = next((item for item in latest if int(item.get("id", -1)) not in before_ids), None)
                    if target is None:
                        latest = _mcp_pages(client.tool("list_pages"))
                        target = next((item for item in latest if str(item.get("url") or "").startswith(payload["url"])), None)
                    if not target or target.get("id") is None:
                        raise RuntimeError("Chrome did not return the new application tab.")
                    page_id = int(target["id"])
                    pages[run_id] = page_id
                elif action == "continue":
                    page_id = pages[run_id]
                else:
                    continue

                _set_run(run_id, status="opening", message="Checking the employer form in your regular Chrome.")
                result = _fill_connected_chrome_page(client, page_id, payload)
                page_response = client.tool("list_pages")
                current = next((item for item in _mcp_pages(page_response) if int(item.get("id", -1)) == page_id), {})
                page_url = str(current.get("url") or result.get("url") or payload["url"])
                result.pop("url", None)
                if result.get("status") not in {"sign_in_required", "needs_manual_navigation", "chrome_setup_required"}:
                    _cleanup_staged_run(run_id, payload)
                _set_run(run_id, **result, url=page_url, host=urlsplit(page_url).hostname or "")
            except Exception as exc:
                error_detail = str(exc).strip()
                message = error_detail.splitlines()[0][:360] or "Could not prepare the employer form in Chrome."
                macos_access_message = _macos_chrome_profile_access_guidance(error_detail)
                if macos_access_message:
                    _set_run(run_id, status="chrome_setup_required", message=macos_access_message, needs_user=[macos_access_message])
                elif any(term in message.casefold() for term in ("connection", "connect", "browser is not", "not connected", "remote debugging")):
                    _set_run(run_id, status="chrome_setup_required", message=message, needs_user=[message])
                else:
                    _set_run(run_id, status="failed", message=message)
                if run_id in pages and _RUNS.get(run_id, {}).get("status") == "failed":
                    pages.pop(run_id, None)
    finally:
        if client:
            client.close()
        with _RUNS_LOCK:
            staged = list(_RUN_PRIVATE.items())
        for run_id, payload in staged:
            _cleanup_staged_run(run_id, payload)


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
            "message": "Opening the employer form in a new tab in your regular Chrome.",
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
        if current.get("status") not in {"chrome_setup_required", "sign_in_required", "human_verification_required", "ready", "needs_manual_navigation"}:
            return dict(current)
        current.update(status="queued", message="Continuing form preparation in your regular Chrome.")
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


def disconnect_portal_browser() -> dict[str, Any]:
    run_id = "disconnect-" + uuid.uuid4().hex
    with _RUNS_LOCK:
        _RUNS[run_id] = {"run_id": run_id, "status": "queued", "message": "Disconnecting Neural Ant from Chrome."}
    _ensure_worker()
    _TASKS.put((run_id, "disconnect", {}))
    return get_portal_run(run_id) or {}


def open_chrome_settings() -> dict[str, Any]:
    return open_chrome_remote_debugging_settings()


