"use client";

import { Badge, Button, Field, Select, StepLog, TextInput, cn, toast } from "@keelson/ui";
import { ArrowLeft, ArrowRight, CheckCircle2, Play, Power } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useReducer, useRef, useState, type FormEvent, type ReactNode, type Ref } from "react";
import { adminError } from "@/lib/admin/format";
import { AUTH_KIND_LABELS } from "@/lib/admin/tools";
import type { ConnectorTool } from "@/lib/admin/types";
import { useAdminApi } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import {
  TRUST_LEVEL_LABELS,
  WIZARD_STEPS,
  WIZARD_STEP_LABELS,
  canEnter,
  canRegisterFirstParty,
  createPayload,
  initialWizardState,
  testLogItems,
  testPassed,
  toolsNeedingReview,
  validateAuth,
  validateBasics,
  wizardReducer,
  type FieldErrors,
  type WizardAuthKind,
  type WizardState,
  type WizardStep,
} from "@/lib/admin/wizard";
import { AdminPageHeader, Facts, InlineError } from "./common";
import { TierBadges, linkClass } from "./ConnectorsPage";
import { announceChangeRequest } from "./SecondAdmin";
import { ToolsTable } from "./ToolsTable";

function Stepper({ state, onGo }: { state: WizardState; onGo: (s: WizardStep) => void }) {
  const currentIndex = WIZARD_STEPS.indexOf(state.step);
  return (
    <ol aria-label="Steps" className="flex flex-wrap gap-1.5">
      {WIZARD_STEPS.map((s, i) => {
        const current = s === state.step;
        const done = i < currentIndex;
        const reachable = canEnter(state, s) && !current;
        return (
          <li key={s} aria-current={current ? "step" : undefined}>
            <button
              type="button"
              disabled={!reachable}
              onClick={() => onGo(s)}
              className={cn(
                "flex h-8 items-center gap-2 rounded-pill border px-3 text-caption transition-colors duration-150",
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:cursor-default",
                current ? "border-accent bg-accent-soft font-medium text-accent-soft-fg" : done ? "border-border text-fg" : "border-border text-fg-muted",
                reachable && "hover:bg-surface-2",
              )}
            >
              <span aria-hidden="true" className="tabular-nums">
                {i + 1}
              </span>
              {WIZARD_STEP_LABELS[s]}
              <span className="sr-only">{current ? " (current step)" : done ? " (done)" : ""}</span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

function Panel({ title, headingRef, children }: { title: string; headingRef: Ref<HTMLHeadingElement>; children: ReactNode }) {
  return (
    <section aria-labelledby="wizard-step-title" className="flex flex-col gap-4 rounded-lg border border-border bg-surface p-4">
      <h2 id="wizard-step-title" ref={headingRef} tabIndex={-1} className="text-body font-semibold text-fg outline-none">
        {title}
      </h2>
      {children}
    </section>
  );
}

function NavRow({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">{children}</div>;
}

/** Add connector: Basics → Auth → Test (live step log) → Review tools → Activate. */
export function ConnectorWizard() {
  const admin = useAdminApi();
  const { me } = useSession();
  const firstPartyAllowed = canRegisterFirstParty(me.user);
  const [state, dispatch] = useReducer(wizardReducer, initialWizardState);
  const router = useRouter();
  const [showErrors, setShowErrors] = useState<{ basics: boolean; auth: boolean }>({ basics: false, auth: false });
  const headingRef = useRef<HTMLHeadingElement>(null);
  const firstRender = useRef(true);

  // Move focus to the new step's heading (not on first paint).
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    headingRef.current?.focus();
  }, [state.step]);

  const slug = state.created?.slug ?? null;

  // Entering Review: load the stored tools (status, drift); fall back to the test's discovery.
  useEffect(() => {
    if (state.step !== "review" || !slug || state.toolsLoaded) return;
    let cancelled = false;
    admin.connectorTools(slug).then(
      (tools) => {
        if (!cancelled) dispatch({ type: "tools_loaded", tools });
      },
      () => {
        if (cancelled || state.test.status !== "done") return;
        const fallback: ConnectorTool[] = state.test.result.tools.map((t) => ({
          name: t.name,
          title: t.title ?? null,
          description: t.description ?? null,
          // Unknown classification fails closed (never shown as a read tool with trusted data).
          access: t.suggested.access ?? "write",
          content_trust: t.suggested.content_trust ?? "untrusted_text",
          egress: t.suggested.egress ?? "internal",
          idempotency: t.suggested.idempotency ?? "none",
          status: "needs_review",
          suggested: t.suggested,
          annotations: t.annotations ?? null,
        }));
        dispatch({ type: "tools_loaded", tools: fallback });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [admin, slug, state.step, state.toolsLoaded, state.test]);

  const basicsErrors: FieldErrors = validateBasics(state.basics);
  const authErrors: FieldErrors = validateAuth(state.auth);
  const err = (errors: FieldErrors, show: boolean, key: string) => (show ? errors[key] : undefined);

  const runTest = async (forSlug: string) => {
    dispatch({ type: "test_start" });
    try {
      const result = await admin.testConnector(forSlug);
      dispatch({ type: "test_done", result });
    } catch (e) {
      dispatch({ type: "test_error", message: adminError(e) });
    }
  };

  const submitBasics = (e: FormEvent) => {
    e.preventDefault();
    setShowErrors((s) => ({ ...s, basics: true }));
    if (Object.keys(basicsErrors).length === 0) dispatch({ type: "goto", step: "auth" });
  };

  const submitAuth = async (e: FormEvent) => {
    e.preventDefault();
    if (state.created) {
      dispatch({ type: "goto", step: "test" });
      return;
    }
    setShowErrors((s) => ({ ...s, auth: true }));
    if (Object.keys(authErrors).length > 0) return;
    dispatch({ type: "busy", busy: true });
    try {
      const out = await admin.createConnector(createPayload(state));
      dispatch({ type: "created", slug: out.slug });
      toast.success(`${state.basics.name.trim()} saved as a draft`);
      void runTest(out.slug);
    } catch (e2) {
      dispatch({ type: "error", message: adminError(e2) });
    }
  };

  const activate = async () => {
    if (!slug) return;
    dispatch({ type: "busy", busy: true });
    try {
      const out = await admin.activateConnector(slug);
      if (out.kind === "change_request") {
        // An external-egress connector needs a second admin: it stays tested until approved.
        dispatch({ type: "busy", busy: false });
        announceChangeRequest(out.request, router.push);
        return;
      }
      dispatch({ type: "activated" });
      toast.success(`${state.basics.name.trim()} is active`);
    } catch (e) {
      dispatch({ type: "error", message: adminError(e) });
    }
  };

  const locked = state.created !== null;
  const lockedNote = locked ? (
    <p className="text-caption text-fg-muted">Saved as a draft: these settings can no longer be changed here. Delete the draft to start again.</p>
  ) : null;
  const review = toolsNeedingReview(state);

  return (
    <>
      <AdminPageHeader
        title="Add connector"
        description="Register an MCP server, test it, review its tools, then activate it."
        actions={
          <Link href="/admin/connectors" className={cn(linkClass, "text-ui")}>
            Back to connectors
          </Link>
        }
      />
      <Stepper state={state} onGo={(s) => dispatch({ type: "goto", step: s })} />
      {state.error ? <InlineError message={state.error} /> : null}

      {state.step === "basics" ? (
        <Panel title="1. Basics" headingRef={headingRef}>
          <form className="flex flex-col gap-3" onSubmit={submitBasics} noValidate aria-label="Basics">
            {lockedNote}
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Name" required error={err(basicsErrors, showErrors.basics, "name")}>
                <TextInput value={state.basics.name} disabled={locked} required onChange={(e) => dispatch({ type: "basics", patch: { name: e.target.value } })} />
              </Field>
              <Field label="Slug" required description="Used in URLs and the agent settings." error={err(basicsErrors, showErrors.basics, "slug")}>
                <TextInput value={state.basics.slug} mono disabled={locked} required onChange={(e) => dispatch({ type: "basics", patch: { slug: e.target.value } })} />
              </Field>
              <Field
                label="MCP server URL"
                required
                description="Public address, or a host allowed for development. Traffic goes through the egress proxy."
                error={err(basicsErrors, showErrors.basics, "mcp_url")}
              >
                <TextInput
                  type="url"
                  inputMode="url"
                  mono
                  placeholder="https://erp.example.com/mcp"
                  value={state.basics.mcp_url}
                  disabled={locked}
                  required
                  onChange={(e) => dispatch({ type: "basics", patch: { mcp_url: e.target.value } })}
                />
              </Field>
              <Field
                label="Trust level"
                description={firstPartyAllowed ? "First-party tools are approved as suggested; third-party tools wait for review." : undefined}
                lockedReason={firstPartyAllowed ? undefined : "First party requires platform admin"}
              >
                <Select
                  value={state.basics.trust_level}
                  disabled={locked}
                  onChange={(v) => dispatch({ type: "basics", patch: { trust_level: v as WizardState["basics"]["trust_level"] } })}
                  options={[
                    { value: "third_party", label: TRUST_LEVEL_LABELS["third_party"]! },
                    { value: "first_party", label: TRUST_LEVEL_LABELS["first_party"]!, disabled: !firstPartyAllowed },
                  ]}
                />
              </Field>
            </div>
            <NavRow>
              <Button size="sm" type="submit" variant="primary" icon={<ArrowRight className="size-4" />}>
                Next: authentication
              </Button>
            </NavRow>
          </form>
        </Panel>
      ) : null}

      {state.step === "auth" ? (
        <Panel title="2. Authentication" headingRef={headingRef}>
          <form className="flex flex-col gap-3" onSubmit={(e) => void submitAuth(e)} noValidate aria-label="Authentication">
            {lockedNote}
            <Field label="Method">
              <Select
                value={state.auth.kind}
                disabled={locked}
                onChange={(v) => dispatch({ type: "auth", patch: { kind: v as WizardAuthKind } })}
                options={(["static_header", "oauth_client_credentials", "none"] as const).map((k) => ({ value: k, label: AUTH_KIND_LABELS[k]! }))}
              />
            </Field>
            {state.auth.kind === "static_header" ? (
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Header name" required error={err(authErrors, showErrors.auth, "header_name")}>
                  <TextInput
                    mono
                    placeholder="X-Api-Key"
                    value={state.auth.header_name}
                    disabled={locked}
                    required
                    onChange={(e) => dispatch({ type: "auth", patch: { header_name: e.target.value } })}
                  />
                </Field>
                <Field label="Header value" required description="Stored encrypted; never shown again." error={err(authErrors, showErrors.auth, "secret")}>
                  <TextInput
                    type="password"
                    autoComplete="new-password"
                    value={state.auth.secret}
                    disabled={locked}
                    placeholder={locked ? "Stored" : undefined}
                    required
                    onChange={(e) => dispatch({ type: "auth", patch: { secret: e.target.value } })}
                  />
                </Field>
              </div>
            ) : null}
            {state.auth.kind === "oauth_client_credentials" ? (
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Token URL" required error={err(authErrors, showErrors.auth, "token_url")}>
                  <TextInput
                    type="url"
                    mono
                    value={state.auth.token_url}
                    disabled={locked}
                    required
                    onChange={(e) => dispatch({ type: "auth", patch: { token_url: e.target.value } })}
                  />
                </Field>
                <Field label="Client ID" required error={err(authErrors, showErrors.auth, "client_id")}>
                  <TextInput mono value={state.auth.client_id} disabled={locked} required onChange={(e) => dispatch({ type: "auth", patch: { client_id: e.target.value } })} />
                </Field>
                <Field label="Client secret" required description="Stored encrypted; never shown again." error={err(authErrors, showErrors.auth, "client_secret")}>
                  <TextInput
                    type="password"
                    autoComplete="new-password"
                    value={state.auth.client_secret}
                    disabled={locked}
                    placeholder={locked ? "Stored" : undefined}
                    required
                    onChange={(e) => dispatch({ type: "auth", patch: { client_secret: e.target.value } })}
                  />
                </Field>
                <Field label="Scopes" description="Space-separated, optional.">
                  <TextInput mono value={state.auth.scopes} disabled={locked} onChange={(e) => dispatch({ type: "auth", patch: { scopes: e.target.value } })} />
                </Field>
                <Field label="Resource" description="Optional audience / resource indicator." error={err(authErrors, showErrors.auth, "resource")}>
                  <TextInput type="url" mono value={state.auth.resource} disabled={locked} onChange={(e) => dispatch({ type: "auth", patch: { resource: e.target.value } })} />
                </Field>
              </div>
            ) : null}
            {state.auth.kind === "none" ? <p className="text-caption text-fg-muted">The server is called without credentials.</p> : null}
            <NavRow>
              <Button size="sm" variant="ghost" icon={<ArrowLeft className="size-4" />} onClick={() => dispatch({ type: "goto", step: "basics" })}>
                Back
              </Button>
              <Button size="sm" type="submit" variant="primary" icon={<Play className="size-4" />} loading={state.busy}>
                {locked ? "Next: test" : "Save draft and test"}
              </Button>
            </NavRow>
          </form>
        </Panel>
      ) : null}

      {state.step === "test" ? (
        <Panel title="3. Test" headingRef={headingRef}>
          <p className="text-ui text-fg-muted">
            Keelson checks the URL policy, reaches the server through the egress proxy, authenticates, lists its tools and detects what it supports.
          </p>
          <StepLog label="Connection test" steps={testLogItems(state.test)} />
          <div aria-live="polite" className="text-ui">
            {state.test.status === "running" ? <p className="text-fg-muted">Testing…</p> : null}
            {state.test.status === "error" ? <InlineError message={`The test could not run: ${state.test.message}`} /> : null}
            {state.test.status === "done" ? (
              state.test.result.ok ? (
                <p className="flex flex-wrap items-center gap-2 text-fg">
                  <CheckCircle2 aria-hidden="true" className="size-4 text-success" />
                  Test passed: {state.test.result.tools.length} tools found.
                  <TierBadges tiers={state.test.result.tier} />
                </p>
              ) : (
                <p className="font-medium text-danger">The test failed. Fix the server or the settings, then run it again.</p>
              )
            ) : null}
          </div>
          <NavRow>
            <Button size="sm" variant="ghost" icon={<ArrowLeft className="size-4" />} onClick={() => dispatch({ type: "goto", step: "auth" })}>
              Back
            </Button>
            <Button size="sm" variant="secondary" icon={<Play className="size-4" />} loading={state.test.status === "running"} onClick={() => slug && void runTest(slug)}>
              {state.test.status === "idle" ? "Run test" : "Run test again"}
            </Button>
            <Button size="sm" variant="primary" icon={<ArrowRight className="size-4" />} disabled={!testPassed(state)} onClick={() => dispatch({ type: "goto", step: "review" })}>
              Next: review tools
            </Button>
          </NavRow>
        </Panel>
      ) : null}

      {state.step === "review" && slug ? (
        <Panel title="4. Review tools" headingRef={headingRef}>
          <p className="text-ui text-fg-muted">
            Check how each tool is classified. Only <strong>approved</strong> tools that <strong>read</strong> <strong>trusted data</strong> are ever given to agents.
          </p>
          {state.toolsLoaded ? (
            <>
              <p className="text-ui" aria-live="polite">
                {review.length > 0 ? (
                  <Badge tone="warning">{review.length} need review</Badge>
                ) : (
                  <Badge tone="success">All tools reviewed</Badge>
                )}
              </p>
              <ToolsTable slug={slug} tools={state.tools} caption="Discovered tools" onToolUpdated={(tool) => dispatch({ type: "tool_updated", tool })} />
            </>
          ) : (
            <p className="text-ui text-fg-muted" role="status">
              Loading tools…
            </p>
          )}
          <NavRow>
            <Button size="sm" variant="ghost" icon={<ArrowLeft className="size-4" />} onClick={() => dispatch({ type: "goto", step: "test" })}>
              Back
            </Button>
            <Button size="sm" variant="primary" icon={<ArrowRight className="size-4" />} onClick={() => dispatch({ type: "goto", step: "activate" })}>
              Next: activate
            </Button>
          </NavRow>
        </Panel>
      ) : null}

      {state.step === "activate" && slug ? (
        <Panel title="5. Activate" headingRef={headingRef}>
          {state.activated ? (
            <div className="flex flex-col gap-3" role="status">
              <p className="flex items-center gap-2 text-ui font-medium text-fg">
                <CheckCircle2 aria-hidden="true" className="size-4 text-success" />
                {state.basics.name.trim()} is active.
              </p>
              <p className="text-ui text-fg-muted">Bind its tools to agents on the Agents page.</p>
              <div className="flex flex-wrap gap-3 text-ui">
                <Link href={`/admin/connectors/${encodeURIComponent(slug)}`} className={linkClass}>
                  Open {state.basics.name.trim()}
                </Link>
                <Link href="/admin/agents" className={linkClass}>
                  Go to Agents
                </Link>
              </div>
            </div>
          ) : (
            <>
              <Facts
                items={[
                  ["Name", state.basics.name.trim()],
                  ["URL", <span key="u" className="font-mono text-caption break-all">{state.basics.mcp_url.trim()}</span>],
                  ["Authentication", AUTH_KIND_LABELS[state.auth.kind] ?? state.auth.kind],
                  ["Tier", state.test.status === "done" ? <TierBadges key="t" tiers={state.test.result.tier} /> : "—"],
                  ["Approved tools", String(state.tools.filter((t) => t.status === "approved").length)],
                ]}
              />
              {review.length > 0 ? (
                <p className="text-ui text-warning">
                  {review.length} {review.length === 1 ? "tool still needs" : "tools still need"} review. They stay unavailable to agents until approved.
                </p>
              ) : null}
              <NavRow>
                <Button size="sm" variant="ghost" icon={<ArrowLeft className="size-4" />} onClick={() => dispatch({ type: "goto", step: "review" })}>
                  Back
                </Button>
                <Button size="sm" variant="primary" icon={<Power className="size-4" />} loading={state.busy} onClick={() => void activate()}>
                  Activate connector
                </Button>
              </NavRow>
            </>
          )}
        </Panel>
      ) : null}
    </>
  );
}
