"use client";

/**
 * Everything the case screen can ask engine-api to do, with the error handling the contract
 * requires (`30-engine-api-events.md`):
 *
 * - messages and case actions carry an `Idempotency-Key`; a key is reused until the server gives
 *   a definite answer, so retrying after a network failure can never start a second run;
 * - an approval answered `412 connect_required` navigates to the (validated) `connect_url`, back
 *   to this case afterwards; `409` (stale/superseded) refreshes the case;
 * - accepted work is announced to the stream reducer (`expect_run`) so the turn appears at once.
 */
import type { BlockAction } from "@keelson/viz";
import { toast } from "@keelson/ui";
import { useCallback, useEffect, useRef } from "react";
import { ApiError, describeError, isApiError, type ApiClient, type ApproachEditBody, type CaseActionBody, type DecisionBody } from "../api/client";
import { uuid } from "../ids";
import { connectUrl, safeConnectUrl, safeReturnPath } from "../paths";
import type { CaseStreamAction } from "../stream/reducer";

export type CaseActionDeps = {
  api: ApiClient;
  caseId: string;
  dispatch: (action: CaseStreamAction) => void;
  /** Re-read the case snapshot (after a 409). */
  refresh: () => void;
  /** A user message was accepted. */
  onMessageAccepted?: ((message: { text: string; runId: string; key: string }) => void) | undefined;
  /** Open the canvas on a block (`open_canvas`). */
  openCanvas?: ((blockId: string) => void) | undefined;
  /** Navigation seam (tests). */
  navigate?: ((url: string) => void) | undefined;
};

const DECISION_FIELDS = ["plan_version", "change_set_hash", "edits", "removed_checks", "reason"] as const;

/**
 * Client-decision limits (contracts `MAX_PAYLOAD_KEYS`, `MAX_EDIT_IDS`, `EditKey`,
 * `MAX_DECISION_REASON_CHARS`).
 */
export const DECISION_LIMITS = { maxKeys: 50, maxIds: 500, keyPattern: /^[a-z][a-z0-9_]{0,63}$/, maxReason: 500 } as const;

/** Decisions that may carry the decider's note (M5 `ResumePayload.reason`). */
const REASON_DECISIONS = new Set(["reject", "revise"]);

function stringList(value: unknown, max: number): string[] | null {
  if (!Array.isArray(value)) return null;
  const out = [...new Set(value.filter((v): v is string => typeof v === "string" && v.trim().length > 0))];
  return out.length <= max ? out : null;
}

/** Typed approach edits (M4): only well-formed `{key, value_int}` / `{key, value_ids}` items. */
export function typedEdits(value: unknown): ApproachEditBody[] | null {
  if (!Array.isArray(value) || value.length > DECISION_LIMITS.maxKeys) return null;
  const out: ApproachEditBody[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") return null;
    const { key, value_int, value_ids } = item as Record<string, unknown>;
    if (typeof key !== "string" || !DECISION_LIMITS.keyPattern.test(key)) return null;
    if (typeof value_int === "number" && value_ids == null && Number.isSafeInteger(value_int)) {
      out.push({ key, value_int });
      continue;
    }
    const ids = value_int == null ? stringList(value_ids, DECISION_LIMITS.maxIds) : null;
    if (!ids || ids.length === 0 || ids.length !== (value_ids as unknown[]).length) return null;
    out.push({ key, value_ids: ids });
  }
  return out;
}

/** A decision payload that must not be sent (it would change the approach in a way not asked for). */
export class InvalidDecisionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InvalidDecisionError";
  }
}

/**
 * The decision body: only the fields the API takes (`decision`, `plan_version`,
 * `change_set_hash`, `edits` as M1 text or M4 typed list, `removed_checks`). A malformed typed
 * edit list or removed-check list is never sent, neither half-way nor dropped (an `edit` without
 * its edits would redo the approach unchanged): `InvalidDecisionError` is thrown instead.
 */
export function decisionBody(decision: string, payload: Record<string, unknown> | undefined): DecisionBody {
  const body: DecisionBody = { decision };
  for (const field of DECISION_FIELDS) {
    const value = payload?.[field];
    if (field === "plan_version" && typeof value === "number" && Number.isInteger(value)) body.plan_version = value;
    if (field === "change_set_hash" && typeof value === "string") body.change_set_hash = value;
    if (field === "edits" && typeof value === "string" && value.trim()) body.edits = value.trim();
    if (field === "edits" && Array.isArray(value)) {
      const edits = typedEdits(value);
      if (!edits) throw new InvalidDecisionError("Some of the changed values are not valid.");
      body.edits = edits;
    }
    if (field === "removed_checks" && value !== undefined && value !== null) {
      const ids = stringList(value, DECISION_LIMITS.maxKeys);
      if (!ids) throw new InvalidDecisionError("The checks to remove are not valid.");
      if (ids.length > 0) body.removed_checks = ids;
    }
    if (field === "reason" && typeof value === "string" && value.trim()) {
      // The engine refuses a reason on any other decision (422): never send one there.
      if (!REASON_DECISIONS.has(decision)) continue;
      const reason = value.trim();
      if (reason.length > DECISION_LIMITS.maxReason) {
        throw new InvalidDecisionError(`The reason is too long (at most ${DECISION_LIMITS.maxReason} characters).`);
      }
      body.reason = reason;
    }
  }
  return body;
}

/**
 * The action body; `confirm_assumptions` carries the confirmed keys — always 1–50 of them. The
 * engine reads an empty list as "every pending value", which could confirm a value this viewer
 * was never shown (plan §3: inference is confirmed by a human), so an empty or oversized list is
 * refused (`InvalidDecisionError`) instead of being sent as `[]`.
 */
export function actionBody(action: Extract<BlockAction, { type: "case_action" }>): CaseActionBody {
  const body: Extract<CaseActionBody, { option_id: string }> = { type: action.action, option_id: action.optionId, plan_version: action.planVersion };
  if (action.action === "confirm_assumptions") {
    const keys = stringList(action.assumptionKeys ?? [], DECISION_LIMITS.maxKeys);
    if (!keys || keys.length === 0) throw new InvalidDecisionError("There are no values to confirm, or too many at once.");
    body.assumption_keys = keys;
  }
  return body;
}

/** `rollback` (M5 Undo) carries only the change set id (1–200 characters, contracts `ActionPayload`). */
export function rollbackBody(changeSetId: string): CaseActionBody {
  const id = changeSetId.trim();
  if (!id || id.length > 200) throw new InvalidDecisionError("This change set can't be undone from here.");
  return { type: "rollback", change_set_id: id };
}

/** A definite answer from the server (the key must not be reused for a different attempt). */
function isDefinite(err: unknown): boolean {
  return err instanceof ApiError && err.status >= 400 && err.status < 500;
}

function currentPath(): string {
  if (typeof window === "undefined") return "/";
  return safeReturnPath(`${window.location.pathname}${window.location.search}`);
}

function defaultNavigate(url: string): void {
  window.location.assign(url);
}

export function useCaseActions(deps: CaseActionDeps) {
  const depsRef = useRef(deps);
  useEffect(() => {
    depsRef.current = deps;
  });
  /** Pending idempotency keys by request signature. */
  const keys = useRef(new Map<string, string>());

  const keyFor = useCallback((signature: string) => {
    let key = keys.current.get(signature);
    if (!key) {
      key = uuid();
      keys.current.set(signature, key);
    }
    return key;
  }, []);

  const sendMessage = useCallback(async (text: string): Promise<boolean> => {
    const { api, caseId, dispatch, onMessageAccepted } = depsRef.current;
    const signature = `message:${text}`;
    const key = keyFor(signature);
    try {
      const runId = await api.postMessage(caseId, text, key);
      keys.current.delete(signature);
      dispatch({ type: "expect_run", runId });
      onMessageAccepted?.({ text, runId, key });
      return true;
    } catch (err) {
      if (isDefinite(err)) keys.current.delete(signature);
      toast.error("Your message wasn't sent", { description: describeError(err) });
      return false;
    }
  }, [keyFor]);

  const cancelRun = useCallback(async (runId: string): Promise<void> => {
    const { api } = depsRef.current;
    // The outcome arrives on the stream (RUN_ERROR CANCELLED with metadata.sc.runId = runId).
    try {
      await api.cancelRun(runId);
    } catch (err) {
      toast.error("Couldn't stop the run", { description: describeError(err) });
    }
  }, []);

  const onBlockAction = useCallback(async (action: BlockAction): Promise<void> => {
    const { api, caseId, dispatch, refresh, openCanvas } = depsRef.current;
    const navigate = depsRef.current.navigate ?? defaultNavigate;
    switch (action.type) {
      case "interrupt_decision": {
        try {
          const out = await api.decide(action.interruptId, decisionBody(action.decision, action.payload));
          dispatch({
            type: "interrupt_decided",
            interruptId: action.interruptId,
            decision: action.decision,
            at: new Date().toISOString(),
            recorded: out.status === "recorded",
          });
          if (out.status === "resumed" && out.run_id) dispatch({ type: "expect_run", runId: out.run_id });
          else if (out.status === "recorded") toast("Your decision is recorded", { description: "Waiting for the other approvers." });
        } catch (err) {
          if (isApiError(err, "connect_required")) {
            navigate(
              safeConnectUrl(err.body["connect_url"], window.location.origin, {
                connector: "merxa",
                returnTo: currentPath(),
              }),
            );
            return;
          }
          if (err instanceof ApiError && err.status === 409) refresh();
          toast.error("That didn't go through", {
            description: err instanceof InvalidDecisionError ? err.message : describeError(err),
          });
        }
        return;
      }
      case "case_action": {
        let body: CaseActionBody;
        try {
          body = actionBody(action);
        } catch (err) {
          toast.error("That didn't go through", { description: err instanceof Error ? err.message : describeError(err) });
          return;
        }
        const signature = `action:${action.action}:${action.optionId}:${action.planVersion}:${("assumption_keys" in body ? (body.assumption_keys ?? []) : []).join(",")}`;
        const key = keyFor(signature);
        try {
          const runId = await api.postAction(caseId, body, key);
          keys.current.delete(signature);
          dispatch({ type: "expect_run", runId });
        } catch (err) {
          if (isDefinite(err)) keys.current.delete(signature);
          if (err instanceof ApiError && err.status === 409) refresh();
          toast.error("That didn't go through", { description: describeError(err) });
        }
        return;
      }
      case "rollback": {
        let body: CaseActionBody;
        try {
          body = rollbackBody(action.changeSetId);
        } catch (err) {
          toast.error("That didn't go through", { description: err instanceof Error ? err.message : describeError(err) });
          return;
        }
        const signature = `rollback:${action.changeSetId}`;
        const key = keyFor(signature);
        try {
          const runId = await api.postAction(caseId, body, key);
          keys.current.delete(signature);
          dispatch({ type: "expect_run", runId });
        } catch (err) {
          if (isDefinite(err)) keys.current.delete(signature);
          if (err instanceof ApiError && err.status === 409) refresh();
          toast.error("Couldn't undo the changes", { description: describeError(err) });
        }
        return;
      }
      case "checklist_step": {
        try {
          await api.tickChecklistStep(caseId, action.checklistId, action.seq, action.status);
          // The refreshed checklist block arrives on the case stream.
        } catch (err) {
          if (isApiError(err, "already_verified")) {
            toast("This step is already verified", { description: "Merxa already shows the change, so it can't be changed by hand." });
            return;
          }
          toast.error("Couldn't update the step", { description: describeError(err) });
        }
        return;
      }
      case "connect":
        navigate(connectUrl(action.connector, action.returnTo || currentPath()));
        return;
      case "open_canvas":
        openCanvas?.(action.blockId);
        return;
    }
  }, [keyFor]);

  return { sendMessage, cancelRun, onBlockAction };
}
