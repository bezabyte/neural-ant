# Neural Ant interface refresh

This refresh is based on the user's AI Engineer transcript collection and the existing app workflow. It keeps the current HTML, CSS, and JavaScript stack; no UI framework or runtime dependency was added.

## Transcript lessons used

- **“Good Design Hasn't Changed with AI” — John Pham, SF Compute** (transcript `1118_good-design-hasn-t-changed-with-ai-john-pham-sf-compute_7e7eVtcygCM.txt`): design is the whole experience, not decoration. The talk highlights perceived speed, useful feedback, trust, and the way a product makes people feel. Applied here as a clearer first action, quieter layout, shorter progress messages, and one consistent color system.
- **“The Human Is an Async API” — Melanie Warrick, Temporal** (transcript `0294_the-human-is-an-async-api-melanie-warrick-temporal_jc3kbZkuHTo.txt`): keep human decisions as explicit signals in a workflow, especially when a wrong decision has a high cost. Neural Ant keeps resume/profile verification, sponsorship uncertainty, document review, and employer submission under the user's control.

## Interface changes

- Made resume upload and **Find jobs & prepare drafts** the primary home-screen path.
- Moved optional AI review, timing comparison, and individual commands into native expandable sections. Existing controls and command IDs remain available to the app.
- Shortened workflow status text so users see what is happening without needing to know slash-command names.
- Simplified job search copy, removed a repeated banner, and folded one-time board and sponsorship-history setup into an expandable section. Search filters and match evidence remain visible.
- Restyled search results, forms, navigation, and cards around the user's navy, cream, and coral palette.
- Added responsive spacing, visible keyboard focus, and reduced-motion handling with current browser-native HTML/CSS features.

## Limits

These changes were visually reviewed in the local app. They are not the result of a formal usability study. No new UI framework or third-party browser dependency was introduced.
