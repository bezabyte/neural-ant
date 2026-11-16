# Local employer-form preparation

## What one click does

For a single role, **Prepare to apply** follows Neural Ant's existing `/apply` path: it reuses or creates that role's tailored resume and cover letter, runs the document checks, and saves the review pack. It then asks the user's already-running Chrome to open a new tab in the same signed-in profile. Neural Ant follows one unambiguous direct **Apply** link when available, uploads the tailored DOCX files where the form accepts them, and fills blank fields that match saved profile facts. It reports what it filled and what still needs attention.

This handoff is additional to the reference repository's document workflow. The reference `/apply` command does not automate browser forms. Neural Ant's application content remains grounded in the saved resume and profile; this browser step uses deterministic label matching, not an LLM or generated code.

## Supported saved-profile fields

Neural Ant can fill name, email, phone, home address, city/state/postal code/country, LinkedIn, GitHub, website, current employer/title when explicitly labeled, years of experience, skills, languages, certifications, education, and explicit employment/education history fields. It never overwrites a value already present in the form. It can upload tailored resume and cover-letter DOCX files when the portal accepts them.

Some portals use repeatable experience forms, custom controls, or PDF-only attachments. Neural Ant leaves unsupported controls and incompatible file formats for you; it does not invent dates or other missing facts. Required unanswered fields are listed in the review panel.

## Browser and privacy behavior

- Portal preparation is local-only. `run-local.sh` binds the app to `127.0.0.1`; production and Docker mode cannot connect to a browser on your device.
- Neural Ant never launches a separate, guest, or dedicated Chrome profile. On Chrome 144 or newer, enable Remote Debugging once at `chrome://inspect/#remote-debugging` in your normal Chrome window. Chrome asks for permission when Neural Ant connects; approve that prompt to continue. Chrome's remote debugging access can see and control tabs and browser state in the selected profile while connected, so enable it only when you want Neural Ant to prepare an application.
- **Disconnect Neural Ant from Chrome** stops the local browser-control process but leaves your open Chrome tabs and sign-in sessions intact. Chrome DevTools MCP usage statistics and CrUX reporting are disabled for this connection.
- Neural Ant never stores employer passwords. A specific **Prepare to apply** click is the user action that sends the selected role's tailored documents and matched profile fields to the employer portal.
- The browser follows only public HTTPS postings and direct Apply links on the same employer site or a recognized ATS. Unknown hosts are left for manual review. A login, CAPTCHA, or human verification is never bypassed.
- Neural Ant leaves existing form values intact. It does not answer work-authorization, visa/sponsorship, legal, EEO/demographic, salary, consent, signature, CAPTCHA, or unverified role-specific questions.
- It does not click application form buttons or submit. The last **Submit** action remains yours.

## Bulk preparation

**Prepare selected for review** saves several role packs without opening employer portals. Use **Prepare to apply** on one role at a time to open and fill its application form.
