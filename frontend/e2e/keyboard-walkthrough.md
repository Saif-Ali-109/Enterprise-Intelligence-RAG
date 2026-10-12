# Keyboard walkthrough

**T169 / FR-057 / SC-022.** The manual walkthrough R-012 requires, executed and recorded so it
cannot quietly stop being true.

This is not a transcript of one person's session. It is the walkthrough performed by
[`keyboard.spec.ts`](keyboard.spec.ts) — the same steps, driven by Tab, Shift+Tab, Enter and Space
against the production build — with the result of each step stated. Run it yourself:

```bash
cd frontend
npx playwright test e2e/keyboard.spec.ts
```

Two things this file is not, deliberately:

- **It is not an axe run.** [`accessibility.spec.ts`](accessibility.spec.ts) does that, over every view
  in both colour schemes. axe cannot check any of what follows: a control with `outline: none` is not a
  violation, it is simply invisible while focused.
- **It is not a claim about assistive technology.** No screen reader was driven here. What is verified
  is the part that can be: focus reaches every control, focus is never trapped, focus is never lost,
  and every focused control renders differently from its resting state.

---

## 1. The rule this walkthrough exists to check

`:focus-visible { outline: 2px solid var(--color-accent); outline-offset: 2px; }` in
`app/globals.css`, with no rule anywhere that suppresses it. The check is by measurement rather than
by grep: `hasVisibleFocusIndicator()` reads the computed outline on the focused element and fails when
there is none. A Tailwind `focus-visible:ring-*` compiles to a `box-shadow` with an alpha channel that
can evaluate to nothing, and a class-name check would pass it.

**Result: every tab stop on all five views shows a focus indicator.** The test walks 60 tab stops per
view and measures each one; `/`, `/sources`, `/documents`, `/evaluations`, `/settings` all pass.

## 2. Focus is never trapped

**Result: Tab reaches the browser chrome and wraps on every view.** Each view is tabbed until a tab
stop repeats. A view that never wraps is holding focus — the failure mode of a modal that forgot to
close, and the reason this is checked per view rather than once.

## 3. Asking a question, start to finish

| Step | Action | Result |
|---|---|---|
| 1 | `Tab` from the top of `/` | First stop is the question box, `textarea`. Nothing before it is a trap. |
| 2 | Look at the focused box | Focus indicator visible. |
| 3 | Type `How do I save a filter?` | Text appears; the character counter updates in the field's description. |
| 4 | `Tab` | Stop is `Ask` (`button[submit]`), enabled because the box is no longer empty. |
| 5 | `Enter` | The button disables itself and the stream begins. |
| 6 | Wait for the answer | **Focus lands on the `Answer` heading**, not on `<body>`. |
| 7 | `Tab` | Stop is `Citation 1` — the marker inside the answer text. |
| 8 | `Shift+Tab` | Returns to the answer heading. The order is reversible, which is what makes it navigable. |

**Step 6 is a fix, and it was a defect.** Ask disables itself while a question streams, and a disabled
button drops focus to `<body>` in Chromium. The next Tab then restarts from the top of the document: a
keyboard user who asked a question and waited was returned to the banner, their place lost, with the
answer below them announced only by the live region. `chat-panel.tsx` now recovers focus to the
result's heading, conditionally — only when the browser actually dropped it, never from a reader who
still has it. The headings carry `tabIndex={-1}` so focus can land there without adding them to the
tab order.

Submitting with `Enter` from the textarea does **not** work: a textarea inserts a newline, so the
form is not submitted. That is native behaviour and the walkthrough uses the button instead. It is
recorded here because it is the kind of thing a reader tries first.

## 4. The inspect toggle

| Step | Action | Result |
|---|---|---|
| 1 | `Tab` to the checkbox | Stop is `input[checkbox]`, named "Show the pipeline trace". |
| 2 | `Space` | The checkbox toggles. It is a real checkbox with a wrapping label, not a styled `div`. |

## 5. The corpus console

| Step | Action | Result |
|---|---|---|
| 1 | Go to `/sources` | Tab order reaches the source's origin link, then `Crawl now`, then the rest of the row's actions. |
| 2 | Focus `Crawl now` | Focus indicator visible. |

The source's **name** is not a tab stop — it is a `<span>`, and the focusable element in that cell is
the link to the page the crawl started from. That is deliberate (a name is not an action) and it is
recorded because "the name was not focusable" reads like a defect in a transcript and is not one.

## 6. The evaluation dashboard

| Step | Action | Result |
|---|---|---|
| 1 | Go to `/evaluations` with no run | Tab reaches `Run the evaluation suite`. The page states in words that no results exist and renders no number. |
| 2 | `Enter` on the button | A run starts; the control becomes `Running…` and disabled. |

## 7. What the walkthrough does not cover

- **Screen reader output.** No AT was driven. The live-region behaviour is verified separately by
  [`live-region.spec.ts`](live-region.spec.ts) against the announcement set in `events.md` §8, and the
  regions' roles are asserted in [`semantics.spec.ts`](semantics.spec.ts) — but that is structure, not
  what a given reader says.
- **Zoom and reflow.** Not checked here. The layout uses intrinsic sizing and wraps with `flex-wrap`,
  and axe checks colour and names; neither is a reflow test.
- **Reduced motion.** Declared in `globals.css` and not re-tested here — a `prefers-reduced-motion`
  emulation belongs with the semantics pass, and is not yet one of its cases.

## 8. Defects this walkthrough found

| Defect | Symptom | Fix |
|---|---|---|
| Focus dropped on submit | After asking, focus landed on `<body>`; the next Tab restarted at the top of the document | Recover focus to the result's heading, only when the browser dropped it (`chat-panel.tsx`) |
| No programmatic focus target | Nothing on the result could receive focus | `tabIndex={-1}` on the answer, refusal and unavailable headings |

Everything else the accessibility block found is recorded in the commit for `T168`: the inverted
colour scheme, the stock status colours used as text, and the `definition-list` in `/evaluations`.