import type { BlockEnvelope } from "@keelson/contracts";
import type { JSX } from "react";

/** User intents a block can raise; the app turns them into API calls (`65-web-internals.md`). */
export type BlockAction =
  | { type: "interrupt_decision"; interruptId: string; decision: string; payload?: Record<string, unknown> | undefined }
  | {
      type: "case_action";
      action: CaseActionType;
      optionId: string;
      planVersion: number;
      /** `confirm_assumptions` (M4): the unconfirmed values being confirmed (keys from `unconfirmed`). */
      assumptionKeys?: readonly string[] | undefined;
    }
  | { type: "connect"; connector: "merxa"; returnTo: string }
  | { type: "open_canvas"; blockId: string }
  /**
   * M5 commit tracker **Undo changes** (`docs/contracts/95` §3): case action
   * `{type: "rollback", change_set_id}` (`POST /api/cases/{id}/actions`). Raised only after the
   * person confirmed in the tracker's dialog.
   */
  | { type: "rollback"; changeSetId: string }
  /**
   * M5 Guide mode (`95` §2): a person marks a checklist step `done` or `skipped` by hand
   * (`POST /api/cases/{id}/checklists/{checklist_id}/steps/{seq}`; `verified` comes only from
   * the engine's read-back).
   */
  | { type: "checklist_step"; checklistId: string; seq: number; status: ManualStepStatus };

/** Case actions an option card can raise (`POST /api/cases/{id}/actions` `type`). */
export type CaseActionType = "apply" | "guide" | "promote" | "confirm_assumptions";

/** Every case action type the contracts know (`ActionPayload.type`; M5 adds `rollback`, `checklist_step`). */
export type AnyCaseActionType = CaseActionType | "rollback" | "checklist_step";

/** What a person may set on a guide step (`verified` is set only by read-back). */
export type ManualStepStatus = "done" | "skipped";

/**
 * A typed **Edit** of an approach-card value (M4, `docs/contracts/90` §2; contracts
 * `ApproachEdit`): `key` is one of the card's `editable` keys; `days` / `pct` values set
 * `value_int`, `ids` values set `value_ids`.
 */
export type ApproachEditValue = { key: string; value_int: number } | { key: string; value_ids: string[] };

/** Payload of an `edit` decision from a card with editable values or plan-task checks. */
export type ApproachEditPayload = { edits: ApproachEditValue[]; removed_checks: string[] };

export type BlockDensity = "inline" | "canvas";

/**
 * The **viewer's** own link to a connector (`GET /api/connectors/<slug>/link` for the current
 * user). Blocks carry the initiator's link state (computed when the block was created), which
 * must not decide what this viewer may do.
 *
 * - `checking`: the request is in flight (Approve stays disabled, no call to action yet);
 * - `unknown`: the check failed; the server's pre-flight (`412 connect_required`) decides;
 * - otherwise the link `status` and `applyReady` (active **and** allowed to apply changes).
 */
export type ViewerConnection =
  | { status: "checking" }
  | { status: "unknown" }
  | { status: "active" | "missing" | "expired"; applyReady: boolean };

/**
 * Facts about the person looking at the block. `sub` / `roles` (M5) let an approval card tell
 * whether this viewer already approved and whether *their* approval needs a Merxa link: only
 * the quorum member counted under `approver` executes the changes; a co-approver (e.g.
 * compliance) never needs one.
 */
export type BlockViewer = {
  connections?: { merxa?: ViewerConnection | undefined } | undefined;
  sub?: string | undefined;
  roles?: readonly string[] | undefined;
};

export type BlockRendererProps = {
  envelope: BlockEnvelope;
  onAction: (a: BlockAction) => Promise<void> | void;
  /** Disable every action button (e.g. a run is in flight). */
  busy?: boolean | undefined;
  /** `inline` in the thread (compact), `canvas` in the side panel (full detail). */
  density?: BlockDensity | undefined;
  /** The viewer's own connector links (overrides the initiator-evaluated state a block carries). */
  viewer?: BlockViewer | undefined;
  /** Approach / approval cards: the interrupt was decided or closed (no action buttons). */
  decision?: BlockDecision | undefined;
  /**
   * Fetches the payload behind `envelope.data_ref` (`GET /api/blocks/{id}/data`). The renderer
   * resolves it **before** rendering the block: a loading placeholder meanwhile, the payload
   * validated per `kind@v` (`parseBlockData`), a retryable card when it cannot be loaded.
   */
  loadData?: BlockDataLoader | undefined;
  /** An already resolved `data_ref` payload (fixtures, tests); skips `loadData`. */
  data?: unknown;
  /** Changes when the block is re-emitted (e.g. stream revision) so its payload is fetched again. */
  dataKey?: string | number | undefined;
};

/**
 * Loads a block's `data_ref` payload. Implementations must fetch the block's **own** endpoint
 * (`/api/blocks/{envelope.id}/data`), never an arbitrary URL taken from the envelope.
 */
export type BlockDataLoader = (envelope: BlockEnvelope, options: { signal: AbortSignal }) => Promise<unknown>;

/** Props every registered block component receives (`props` already validated). */
export type BlockComponentProps<P, D = unknown> = {
  props: P;
  /** The validated `data_ref` payload for kinds that have one (`network_map`, `impact_graph`, `stockout_timeline`). */
  data: D;
  envelope: BlockEnvelope;
  onAction: BlockRendererProps["onAction"];
  busy: boolean;
  density: BlockDensity;
  viewer: BlockViewer | undefined;
  decision: BlockDecision | undefined;
};

export type BlockElement = JSX.Element;

/**
 * What became of an interrupt card (approach / approval) that is no longer waiting for this
 * viewer. Given → the card shows "Decided by … · time" instead of buttons nobody can press.
 *
 * - `status: "decided"` (default): `decision` (`approve`, `confirm`, …), who (`by`, display
 *   names) and when (`at`, RFC 3339) when known;
 * - `status: "recorded"`: this viewer decided, other approvers are still needed (quorum);
 * - `status: "closed"`: closed without a known decision (superseded / expired / not reported).
 */
export type BlockDecision = {
  status?: "decided" | "recorded" | "closed" | undefined;
  decision?: string | null | undefined;
  by?: readonly string[] | string | null | undefined;
  at?: string | null | undefined;
};
