# Future Test: Live REPL Multiline Submission

## Edge Case
The interactive REPL binds `Esc` + `Enter` to submit multiline input while plain `Enter` inserts a newline.

## Why This Needs a Future Test
Current automated coverage verifies prompt-session configuration, but it does not exercise a real TTY key sequence. A live terminal integration test is still needed to confirm the actual keystroke behavior.

## What To Verify
- In `foundation chat`, pressing `Enter` inserts a newline instead of submitting.
- Pressing `Esc` then `Enter` submits the full multiline request.
- The submitted request reaches planning as one user turn.
- Persisted transcript/history records the request as a single turn.
- Prompt rendering remains intact after submission.

## Suggested Test Shape
- Spawn `foundation chat` in a PTY-backed integration test.
- Send a multiline request such as:
  - line 1: `summarize`
  - line 2: `git status`
- Submit with `Esc` + `Enter`.
- Assert the planner receives one request with an embedded newline or equivalent combined content.
- Assert the session exits cleanly afterward.
