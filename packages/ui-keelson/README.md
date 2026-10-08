# @keelson/ui

Presentational, accessible React primitives for Keelson — Base UI 1.8 under the hood, Tailwind 4
utilities on `@keelson/tokens`, Motion 13.5.1, Lucide icons. No data fetching; every component is
controlled by props. Calm, Gemini-like *behaviour* (pill composer, shimmer while thinking,
rounded surfaces) with an original, neutral brand.

Consumed as TypeScript source (`"main": "src/index.ts"`); the app lists it in
`transpilePackages`. Interactive modules carry `"use client"`; pure helpers do not.

## Setup (app)

```css
@import "tailwindcss";
@import "@keelson/tokens/theme.css";
@import "@keelson/ui/styles.css";   /* registers this package with Tailwind (@source) + shimmer/hatch CSS */
@import "@keelson/viz/styles.css";
```

```tsx
import "@fontsource-variable/inter";
import { UiProvider } from "@keelson/ui";
// Motion reducedMotion="user", shared tooltip delays, toast viewport
<UiProvider>{children}</UiProvider>
```

## Components

| Export | Notes |
|---|---|
| `Button`, `IconButton` | Pill buttons (Base UI `Button`). `variant` primary/secondary/ghost/subtle/danger, `size`, `loading` (spinner + `aria-busy`). Disabled buttons stay focusable (`aria-disabled`) so `aria-describedby` reasons are reachable. `IconButton` requires `label` (also its tooltip). |
| `Card`, `CardHeader`, `CardFooter`, `EmptyState` | 16 px rounded surfaces; `Card` is a labelled `section`. `EmptyState headingLevel={1}` makes its title the page heading (not-found / no-access / error pages); default is plain text. |
| `Badge`, `Chip` | Status label; chip is static text or a toggle button (`aria-pressed`). |
| `ProvenanceChip({prov, verified?})` | Colour **+ icon + border style**: fact solid/database · inference dashed/lightbulb · assumption dotted/sliders · simulated hatched/flask · decision borderless/person · committed double/check · verified = committed + badge/double-check. Screen readers hear "Provenance: Simulated". `compact` = icon only. |
| `Avatar`, `AvatarStack({people \| agents, max})` | Initials fallback, deterministic tone; the stack is one labelled group ("Agents consulted: …") with `+N`. |
| `Tabs`, `TabList` | Controlled Base UI tabs (pill list + sliding indicator), optional `actions` slot. |
| `Dialog`, `Sheet` | Base UI Dialog: centred modal or edge sheet (right/left/bottom). |
| `Tooltip`, `TooltipProvider` | Supplementary only — never the sole accessible name. |
| `Skeleton`, `Shimmer`, `Spinner` | Shimmer = teal → green → amber sweep; static under reduced motion. |
| `Composer({value, onChange, onSubmit, busy, onStop, placeholder, suggestions})` | Hero pill composer: Enter sends, Shift+Enter newline, IME-safe, autosizes; Send→Stop while busy; suggestions shown while empty (`suggestionBehavior` fill/submit). |
| `SuggestionChips({suggestions, onSelect})` | Home / follow-up chips. |
| `StageChips({stages})` | Ordered list, `aria-current="step"` on the active stage, state spelled out. |
| `ThinkingTimeline(props)` | See below. |
| `MessageBubble({role, children})` | User = soft bubble right; assistant = unboxed prose with a small mark. `article` labelled "You said" / "<author> said". |
| `CanvasPanel({open, onOpenChange, tabs, activeTab, onTabChange, children, returnFocusRef?})` | ≥ 1280 px: non-modal `aside` (55 % basis, canvas-reveal spring 380/31.2) with tabs + close; below: modal bottom sheet. `mode` can force side/sheet. Place it as a flex sibling of the thread column. **Focus returns to the opener on close** (whatever had focus when `open` became true; side panel: only when focus was inside the panel, so a close from the toggle leaves focus there; sheet: Base UI `finalFocus`); `returnFocusRef` is the fallback when the opener is gone or unknown (e.g. the panel opened itself). `Sheet` also takes `finalFocus`. |
| `Toaster`, `toast()` | Sonner-like minimal API over Base UI Toast: `toast("…")`, `toast.success`, `toast.error`, `toast.dismiss(id)`. Shown top right under the header (never over the composer). Toasts raised before the `Toaster` subscribes (e.g. in a page's mount effect) are queued and replayed (`createBufferedToastManager`). |
| `UiProvider` | MotionConfig `reducedMotion="user"` + TooltipProvider + Toaster. |

### Back-office primitives (M2, compact density)

| Export | Notes |
|---|---|
| `Badge` variants | `variant` soft (default) / outline / solid, `size` sm (20 px) / md, `dot`, `title`. Meaning is always in the text. |
| `Field` | Label + description + `lockedReason` (lock icon + text, e.g. "Requires platform admin") + `error`; controls inside pick up `id`, `aria-describedby`, `aria-invalid` from it. `withCounter` wires a `Textarea` counter into the description. |
| `TextInput`, `Select` | 32 px controls; `Select` is a styled **native** select (`options`, `placeholder`). |
| `Textarea` | `value`/`onChange(value)`, `maxLength` → "120 / 4,000" counter; a polite live region speaks only in the last 10 % ("300 characters left") and over the limit (then `aria-invalid`). `counterAnnouncement()` exported. |
| `Checkbox` | Native checkbox with label, `description` and `disabledReason` (announced with the box). |
| `Switch` | `button role="switch"` + `aria-checked`, label / description. |
| `Table` | `columns` (`cell`, `align`, `nowrap`, `rowHeader`, `srOnlyHeader`), `caption` (optionally hidden), `rowTone` (accent / warning / danger / muted), `rowProps`, `empty`, `busy`; scrolls inside a focusable `group` named by the caption (not a landmark). |
| `StepLog` | Ordered step list (`pending` / `running` / `ok` / `failed` / `skipped`, icon + text, `ms`, `detail`); changes are announced politely (`stepLogAnnouncement()`). |
| `JsonDiff`, `diffJson`, `jsonEqual`, `formatJsonValue` | Structural diff table (setting path · change · before · after); arrays of objects matched by `slug`/`id`/`name`/`alias`/`key`, arrays of primitives report added / removed members; `ignorePaths`. |
| `Meter` | `role="meter"` bar with `valueText`, warning ≥ 80 %, danger ≥ 100 %. |
| `ConfirmDialog` | Base UI AlertDialog: focus starts on Cancel, async `onConfirm` (returning `false` keeps it open), `tone="danger"`. |
| `Drawer` | Right-hand `Sheet` with `size` md 560 / lg 720 / xl 960 px (`Sheet` gained `widthClassName`). |
| hooks | `useMediaQuery`, `usePrefersReducedMotion`, `CANVAS_SIDE_QUERY`. |

### ThinkingTimeline

```ts
type ThinkingTimelineProps = {
  status: 'running' | 'paused' | 'done' | 'error' | 'cancelled';
  headline: string;
  stages: { id: StageId; label: string; state: 'pending'|'active'|'done' }[];
  agents: { id: string; name: string; description: string; state: 'running'|'done'|'error';
            activities: { id: string; label: string; state: 'running'|'done'|'error' }[] }[];
  reasoning?: { agentId: string; text: string }[];
  elapsedMs?: number; defaultOpen?: boolean; onStop?: () => void;
  label?: string;                   // region name, default "Thinking"
};
```

- The timeline is a `<section>` region named by `label`. A thread shows one per turn: pass a
  unique `label` per turn (e.g. `Thinking, turn 2`) so axe's `landmark-unique` holds.

- Collapsed by default: one status line (shimmering headline while running) + "Show thinking",
  an avatar stack of agents consulted, elapsed time and Stop (running + `onStop`).
- Expanded: stage chips, one collapsible row per agent (activity sentences collapsed by default,
  no durations, no tool names), reasoning text per agent.
- Many agents (M4 parallel specialists): the header stack shows 3 avatars then "+N" (the
  overflow's tooltip names the rest; the group label always names everyone); from 4 agents
  (`MANY_AGENTS`) the panel starts with a summary line ("5 agents · 2 working · 1 ran into a
  problem", `agentsSummary`). Inside an agent row, consecutive identical activity sentences are
  grouped ("Read supplier messages · 3 times", `groupActivities`; running if any member runs,
  failed if any failed), and only the latest 6 groups show (`ACTIVITY_PREVIEW`), so the step in
  progress is always visible; older ones fold behind "Show N earlier steps" (`aria-expanded`). The
  step count still counts every activity.
- Screen readers: a `role="status" aria-live="polite"` region announces **stage changes only**
  (keyed by stage id, so a stage finishing is not re-announced). A timeline that mounts for a run
  that is not `running` (thread reload, earlier turns) stays silent until its stage changes; the
  status (Working / Waiting for you / Done / …) is prefixed for the headline.
- `prefers-reduced-motion`: no shimmer (`data-motion="reduced"`), no pulses, Motion reduced.

## Tests

`pnpm --filter @keelson/ui test` (Vitest 5 + Testing Library + jsdom + axe-core). Covers the
timeline (collapsed default, expansion, live-region announcements, reduced motion, Stop, axe),
composer keyboard/IME/busy/suggestions, provenance chip styles, avatar stack, stage chips,
canvas side/sheet modes, tabs keyboard, dialog, toast. `pnpm --filter @keelson/ui typecheck`
(`tsc --noEmit`, TypeScript 5.9.3, strict + `exactOptionalPropertyTypes`).
