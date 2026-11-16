# Neural Ant UI review from the transcript library

## Review scope

The local `aiDotEngineer_transcripts` folder contains 1,511 transcripts. I searched the complete file list and identified 44 titles focused on UX, UI, design, accessibility, interfaces, or human-computer interaction. I reviewed relevant sections from these talks and cross-checked the product-level guidance against `0640_the-ux-of-ai-making-ai-powered-apps-your-users-don-t-hate`, `0828_why-your-ai-ux-is-broken-and-it-s-not-the-model-s-fault`, `0394_design-at-the-speed-of-adjectives`, `0396_the-design-code-roundtrip-that-isn-t`, `0413_the-end-of-the-static-screen`, `0629_agent-output-is-not-ux-rendering-layer`, `0682_design-patterns-for-ai-trust`, `1055_everything-is-ugly-so-go-build-something-that-isn-t`, `1115_ux-design-principles-for-semi-autonomous-multi-agent-systems`, `1118_good-design-hasn-t-changed-with-ai`, `1278_invisible-users-invisible-interfaces`, `1287_chatgpt-is-poorly-designed-so-i-fixed-it`, and `1501_the-intelligent-interface`.

The remaining transcripts cover many unrelated engineering topics. They were included in the corpus search, while the changes below draw on the focused product and UX material.

## Audit and changes

| Principle | Previous gap | Change |
|---|---|---|
| Familiar workflow and clarity | Search setup, sources, and filters competed with the actual job results. | Résumé upload remains the starting point; search runs automatically. Results lead the search page, and secondary filters/source management sit behind a disclosure. |
| Trust through provenance | Fit, sponsorship evidence, and posting details were hard to distinguish. | Job cards give fit and sponsorship their own visual blocks and link to the evidence and official posting when available. The home screen states what stays local, what search providers receive, and where the user takes over. |
| Human control | The application pipeline used narrow stage columns with long stacks of repeated actions. | Replaced the cramped board with a searchable list and stage filters. Each role keeps its stage controls, submission marker, documents, notes, follow-ups, and interview prep. Preparing materials remains separate from submitting them. |
| Visible progress and recovery | Search exposed one status message, so it was unclear which source had completed or failed. | Upload/search now shows profile, search, and ranking stages plus separate employer-board and public-index status. Partial failures are reported while successful source results can still be ranked. |
| Useful feedback and accessible states | Progress and errors were mostly plain text. | Search progress uses visible current/complete/error states and live status text; filters expose pressed state; empty states explain what the user can do next. |
| Visual hierarchy | Role metadata and controls had similar weight, and the application board forced cards into narrow columns. | Search cards prioritize role, employer, location, fit, sponsorship, and primary actions. Application roles use wider rows; supporting controls are grouped under “Documents, notes & interview prep.” |
| Consistent design language | Prior screens and controls did not always use the requested color balance consistently. | Kept navy `#14213D` for structure, cream `#F4EDE0` for the canvas, and coral `#FF6B5B` for primary actions and emphasis across the app. |

## Still incomplete

- Search requests are still synchronous HTTP work. The screen can report source progress and partial errors, but it cannot pause or resume an in-flight request after a disconnect.
- Job sources can return stale or incomplete postings. The official posting remains the verification point; fit and sponsorship text are signals, not guarantees.
- The app does not submit employer applications. It prepares review materials and leaves the employer form and final submission to the user.

## Next usability check

Observe a first-time résumé upload, a search where one provider fails, and reviewing a role pack. The automated checks confirm the code and rendering; they do not replace testing with job seekers.
