# Non-disruptive browser control

Keep the user’s computer available while carrying out development, support, testing, approval, and deployment work.

## Default order

1. Use a purpose-built connector, API, or CLI when it fully handles the operation and the user did not explicitly request browser interaction.
2. For browser interaction, honor an explicitly selected browser. Otherwise follow the current Browser skill's URL/default selection and use the selected Browser surface for authenticated sessions, support chat, administration, testing, and monitoring.
3. Use Computer Use only for a required native desktop or browser-chrome interaction that the selected Browser surface cannot perform. Preserve every confirmation requirement applicable to the action; a required confirmation is not a missing technical capability.

When the user explicitly requests Chrome, keep Chrome as the primary surface. Computer Use may supplement one necessary native step; it must not replace the Chrome workflow.

## Do not interrupt for convenience

Do not use Computer Use merely because:

- a locator is inconvenient;
- the page is visually complex;
- a tab binding became stale;
- the extension has an empty tab list;
- routine navigation, testing, monitoring, or messaging that the selected Browser surface can complete under its current policy is required;
- Computer Use appears faster.

Recover or troubleshoot the selected Browser surface according to its current skill before escalating. When Chrome was selected and only the tab binding is stale, obtain or create a fresh tab from the existing Chrome binding.

## Decide whether interruption is worth it

Use Computer Use only when every condition is true:

1. The action is necessary for the user-authorized outcome.
2. The selected Browser surface or another non-disruptive tool cannot complete it after the required troubleshooting.
3. Deferring the action would block or materially delay the workflow.
4. The native interaction can be kept narrow and its effect can be verified.
5. The action satisfies the Computer Use confirmation and safety policy.

If the user is actively using the computer and the step is not urgent, wait for a natural gap or leave a handoff instead of taking focus.

## Appropriate Computer Use cases

Examples can include:

- a native file picker that the selected Browser surface's file-chooser support cannot complete;
- an operating-system permission or application dialog required by the authorized task;
- a desktop application with no purpose-built control surface;
- a browser setting or native browser-chrome control unavailable through the extension.

These are examples, not automatic permission. Re-evaluate necessity, scope, and confirmation requirements for the actual action.

## Route outbound communication by capability

Use a capable purpose-built tool or the selected Browser surface for authorized
communication, honoring an explicit browser preference. Keep reading, preparation,
and sending on that surface when it supports the operation.

Confirm the identified conversation, recipient, purpose, and existing authority.
Follow the current tool policy and every confirmation requirement applicable to
the message. Reuse valid authorization within its scope; ask only for a missing
decision or required action confirmation.

If a required native step is technically unavailable on that surface, use the
native fallback only under the necessity and interruption conditions above.
Give a brief heads-up before taking focus, perform the narrow step, verify the
result, and return to the selected Browser surface.

Do not choose another surface because it has weaker confirmation behavior.
An access denial, sensitive-data restriction, required confirmation, or human
handoff remains binding when tools change. If that boundary blocks the send,
preserve the draft and continue independent authorized work.

## File uploads

1. Prefer the selected Browser surface's supported file-chooser flow.
2. Resolve and verify the exact file, destination, and upload authorization.
3. If the selected Browser surface cannot complete the upload and the native picker is required, use Computer Use only for that picker interaction.
4. Verify the uploaded filename and resulting page state through the selected Browser surface.
5. Stop using Computer Use immediately after the native step.

Do not ask again when the user already authorized that specific upload and the verified artifact and destination are unambiguous. Ask at action time only when authorization, file scope, destination, sensitive-data transmission, or another Computer Use policy requirement remains unresolved.

Never use an upload workaround to transmit credentials, private logs, or another sensitive file without the required destination-specific authorization.

## Minimize the interruption

- Give the user a brief heads-up before taking focus when the interruption will be visible.
- Do not turn the heads-up into a blocking confirmation when the action is already authorized and policy does not require one.
- Keep the takeover as short as possible.
- For communication requiring a native step, take focus only for that step and its immediate verification.
- Avoid inspecting or changing unrelated windows, tabs, applications, clipboard data, or user activity.
- Preserve the user's existing browser tabs and page state.
- Return to Browser-based control after the blocking native step.
- Leave requested result tabs as deliverables and unfinished tabs as handoffs according to the selected Browser skill.
- Tell the user what required the interruption and whether control was returned.
