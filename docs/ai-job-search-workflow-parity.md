# AI Job Search workflow mapping

This document maps the user's canonical [ai-job-search repository](https://github.com/ai-job-search-dev/ai-job-search) to Neural Ant. Keep that repository as the default workflow reference; do not ask the user to repeat the URL. Neural Ant uses its own private database and web UI, while preserving the reference repo's setup → scrape → rank → apply order.

## End-to-end in Neural Ant

Upload a resume and Neural Ant immediately saves supported profile facts and starts `/scrape`; ranking waits for the independent employer-board and public-index searches to finish. Review the extracted profile and missing search preferences once. After results appear, either prepare one posting or select up to five and run `/apply` as a batch. The backend keeps existing drafts unchanged and prepares no more than two new role packs at once. Each pack includes a job-specific resume, cover letter, review notes, and copy-ready saved-profile facts. External AI remains opt-in; within that pass, independent resume/cover-letter calls and revisions run in parallel, while review stays between those stages. Drafts are saved to the Applications tracker for later review. This workflow does not fill employer portals or submit applications.

The **A/B timing** control switches between planned parallel execution and a sequential orchestration baseline. Neural Ant records the selected mode, total and phase durations, and counts in browser local storage; it does not store role names, resume text, or profile data in this history. Each run also shows a sequential estimate from that run's overlapping task durations. Live search results and reused drafts vary, so the comparison is directional rather than a controlled benchmark. The plan is a fixed list of approved operations; the app does not evaluate arbitrary model-generated code.

Ranking uses the reference repo's four weights: technical skills 30%, experience 25%, behavioral/work-style fit 15%, and career alignment 30%. Eligibility, language, and location/logistics are separate PASS/FLAG/FAIL gates. Hard mismatches score zero and block drafting; flags remain visible for the user to judge. Salary, work mode, employment type, and sponsorship are evidence signals outside the weighted score. Contract and C2C roles remain distinct from sponsorship evidence.

External AI is optional and requires an explicit checkbox or per-role confirmation. Neural Ant redacts common contact and work-authorization details before sending selected resume/profile/job text to the configured provider. Redaction is best-effort. Local drafting does not send resume text outside Neural Ant.

After preparation, the user opens the official posting, uploads the files, completes the employer's application, and submits it manually. Neural Ant has no browser extension, employer-form autofill, or application submission.

## Command mapping and limits

| Source workflow | Neural Ant workflow | Parity and limits |
| --- | --- | --- |
| `/setup` | Resume upload and My profile | Upload parsing fills supported facts. Search goals, professional languages, and work-style preferences can be saved once and reused. Extraction still needs review; it does not run the source repo's structured interview or cross-document conflict workflow. |
| `/scrape` | Agent search | Searches configured public ATS/API boards and the Freehire public index with recency filters and source health. It does not scrape LinkedIn, authenticated boards, or every aggregator. |
| `/rank` | Profile-aware job ranking | Uses the same four score weights and separate eligibility, language, and location gates. Scores are deterministic triage aids; Neural Ant does not run isolated posting-scorer agents or company research during ranking. |
| `/apply` | **Prepare application** on a role or **Prepare selected for review** | Saves a job-specific resume and cover letter, checks generated document text, creates a copy-ready sheet from saved profile facts, and offers an opt-in external drafter → reviewer → revision pass. Users may batch up to five roles; at most two new role packs are prepared concurrently. It does not research company news/culture, spawn an isolated reviewer agent, compile LaTeX PDFs, fill employer forms, submit applications, or visually inspect page layout. Existing drafts remain unchanged. |
| `/interview` | Interview prep within each tracked role | Produces a saved, stage-specific question and STAR pack from resume examples, role requirements, tracker notes, and latest drafts. It does not fetch company research or assume a draft was submitted. |
| `/outcome` | Applications tracker and stale-role review | Stores stage, notes, outcome, and follow-up date; uses Done for roles the user confirms were submitted; excludes completed roles and duplicate company/title listings from fresh search. |
| `/gmail-sync` | Workflow tools → Review a Gmail message | Manual paste-and-preview only. There is no direct Gmail connection. |
| `/html-report` | Download offline report | Self-contained report includes the tracker, fit and sponsorship signals, filters, and charts without resume/profile contact details. |
| `/notion-sync` | Download Notion-ready CSV | One-way manual import, not live Notion synchronization. |
| `/expand` | Workflow tools → Expand profile from public sources | Scans text the user pastes, stores source evidence, and waits for approval before adding skills. Neural Ant does not fetch the supplied URL. |
| `/upskill` | AI Copilot → Skill-gap learning plan | Local checklist based on posting terms missing from the saved skills list; it does not assert a skill is absent from the candidate's experience. |
| `/add-template` | Workflow tools → Custom resume templates | Supports Word `.docx` page setup and style reuse. It does not provide arbitrary LaTeX or custom compile toolchains. |
| `/add-portal` | Agent search → Public job source | Supports built-in public ATS/API adapters. It does not generate and execute a new portal-specific CLI integration. |
| `/reset` | Workflow tools → Reset local workspace data | Typed confirmation is required for scoped deletion. |

Neural Ant does not open employer application pages, upload documents to a portal, answer work-authorization or legal questions, send recruiter messages, or submit applications. The user reviews each role and controls those actions.
