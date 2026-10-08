"use client";

import { mappingActionSchema, type MappingAction, type MappingRead } from "@keelson/contracts";
import { Badge, Button, Checkbox, Field, Select, Tabs, TextInput, Textarea, cn, toast } from "@keelson/ui";
import { opLabel } from "@keelson/viz";
import { FlaskConical, Save, TestTube2 } from "lucide-react";
import Link from "next/link";
import { useId, useMemo, useState } from "react";
import { isApiError } from "@/lib/api/client";
import { adminError, when } from "@/lib/admin/format";
import {
  checkMapping,
  convertText,
  mappedOps,
  mappingTemplate,
  mappingText,
  parsePayload,
  type Diagnostic,
  type DryRunResult,
  type MappingEnvelope,
  type MappingFormat,
  type MappingKind,
  type ReadPreview,
} from "@/lib/admin/mappings";
import type { ConnectorTool } from "@/lib/admin/types";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { CapabilitiesSection } from "./ConnectorCapabilities";
import { linkClass } from "./ConnectorsPage";
import { AdminPageHeader, AdminSection, InlineError, ResourceView } from "./common";
import { LazyCodeEditor } from "./lazy";

const KIND_INFO: Record<MappingKind, { title: string; description: string }> = {
  read: {
    title: "Read mapping",
    description:
      "Per scn entity: which read tool to page through (args, page and cursor, items path, page limit) and how each column is computed from a row ($.field, text joins, int(), date(), constants, lookups). Expressions are never run as code.",
  },
  action: {
    title: "Action mapping",
    description:
      "Per canonical change: the vendor tool, its argument template ({{payload.*}}, {{target_ref}}, {{expected_version}}), how safe a retry is, and the read-back that confirms it. Changes without a mapping are guided.",
  },
};

function DiagnosticList({ id, diagnostics }: { id: string; diagnostics: readonly Diagnostic[] }) {
  const errors = diagnostics.filter((d) => d.severity === "error");
  const warnings = diagnostics.filter((d) => d.severity === "warning");
  const shown = [...errors, ...warnings].slice(0, 12);
  return (
    <div id={id} aria-live="polite" className="flex flex-col gap-1">
      <p className={cn("text-caption font-medium", errors.length ? "text-danger" : "text-fg-muted")} data-mapping-summary="">
        {errors.length
          ? `${errors.length} ${errors.length === 1 ? "problem" : "problems"} to fix before saving`
          : warnings.length
            ? `Valid · ${warnings.length} ${warnings.length === 1 ? "warning" : "warnings"}`
            : "Valid against the mapping contract."}
      </p>
      {shown.length ? (
        <ul className="flex flex-col gap-0.5 text-caption">
          {shown.map((d, i) => (
            <li key={`${d.from}-${i}`} className={d.severity === "error" ? "text-danger" : "text-fg-muted"}>
              Line {d.line}: {d.message}
            </li>
          ))}
          {errors.length + warnings.length > shown.length ? <li className="text-fg-subtle">…and {errors.length + warnings.length - shown.length} more.</li> : null}
        </ul>
      ) : null}
    </div>
  );
}

function summaryOf(kind: MappingKind, value: MappingRead | MappingAction | null): string | null {
  if (!value) return null;
  if (kind === "read" && "entities" in value) {
    const lookups = Object.keys(value.lookups ?? {}).length;
    return `${value.entities.length} ${value.entities.length === 1 ? "entity" : "entities"}: ${value.entities.map((e) => e.entity).join(", ")}${lookups ? ` · ${lookups} ${lookups === 1 ? "lookup" : "lookups"}` : ""}`;
  }
  if ("ops" in value) return `${value.ops.length} mapped ${value.ops.length === 1 ? "change" : "changes"}: ${value.ops.map((o) => opLabel(o.op)).join(", ")}`;
  return null;
}

/** One mapping editor: YAML ⇄ JSON, live contract validation, save as a new version. */
function MappingEditor({
  slug,
  kind,
  saved,
  tools,
  onSaved,
}: {
  slug: string;
  kind: MappingKind;
  saved: MappingEnvelope | null;
  tools: readonly ConnectorTool[] | null;
  onSaved: () => void;
}) {
  const admin = useAdminApi();
  const initialFormat: MappingFormat = "yaml";
  const initialText = saved ? mappingText(saved, initialFormat) : mappingTemplate(kind);
  const [format, setFormat] = useState<MappingFormat>(initialFormat);
  const [baseline, setBaseline] = useState(initialText);
  const [text, setText] = useState(initialText);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<{ message: string; details: string[]; conflict: boolean } | null>(null);
  const [testing, setTesting] = useState(false);
  const [preview, setPreview] = useState<{ result: ReadPreview; draft: boolean } | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const descId = useId();
  const checked = useMemo(() => checkMapping(kind, text, tools), [kind, text, tools]);
  const errors = checked.diagnostics.filter((d) => d.severity === "error").length;
  const dirty = text !== baseline || !saved;

  const switchFormat = (to: MappingFormat) => {
    if (to === format) return;
    const converted = convertText(text, to);
    if (converted === null) {
      toast.error("Fix the syntax first", { description: "The mapping can only be converted once it parses." });
      return;
    }
    // Converting is not an edit of the saved mapping: compare against the converted baseline too.
    const convertedBase = convertText(baseline, to);
    setFormat(to);
    setText(converted);
    if (convertedBase !== null && text === baseline) setBaseline(converted);
  };

  const save = async () => {
    if (!checked.value) return;
    setSaving(true);
    setSaveError(null);
    try {
      const out = await admin.saveMapping(slug, kind, {
        base_version: saved?.version ?? null,
        mapping: checked.value as unknown as Record<string, unknown>,
      });
      if (out.unchanged) toast(`${KIND_INFO[kind].title} unchanged`, { description: `It is the same as v${out.version}.` });
      else toast.success(`${KIND_INFO[kind].title} saved as v${out.version}`, { description: kind === "read" ? "The next crawl uses it." : "New plans use it for per-op changes." });
      setBaseline(text);
      onSaved();
    } catch (err) {
      const details =
        isApiError(err) && Array.isArray(err.body["errors"])
          ? (err.body["errors"] as { loc?: unknown; msg?: unknown }[]).map((e) => {
              const loc = Array.isArray(e.loc) ? e.loc.join(".") : typeof e.loc === "string" ? e.loc : "";
              return loc ? `${loc}: ${String(e.msg ?? "")}` : String(e.msg ?? "");
            })
          : isApiError(err) && Array.isArray(err.body["problems"])
            ? (err.body["problems"] as unknown[]).map(String)
            : [];
      setSaveError({ message: adminError(err), details, conflict: isApiError(err) && err.status === 409 });
    } finally {
      setSaving(false);
    }
  };

  /** Read mapping: crawl one page per source with the draft (when edited and valid) and show the mapped rows. */
  const test = async () => {
    setTesting(true);
    setTestError(null);
    try {
      const draft = dirty && checked.value ? (checked.value as unknown as Record<string, unknown>) : undefined;
      setPreview({ result: await admin.testReadMapping(slug, { mapping: draft, max_pages: 1 }), draft: draft !== undefined });
    } catch (err) {
      setPreview(null);
      const problems = isApiError(err) && Array.isArray(err.body["problems"]) ? (err.body["problems"] as unknown[]).map(String) : [];
      setTestError(problems.length > 1 ? `${adminError(err)} (+${problems.length - 1} more)` : adminError(err));
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="flex flex-col gap-3" data-mapping-editor={kind}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-ui text-fg-muted">
          {saved ? (
            <>
              Saved <Badge size="sm" tone="accent">v{saved.version ?? "?"}</Badge>
              {saved.created_at ? ` · ${when(saved.created_at)}` : ""}
              {saved.created_by ? ` by ${saved.created_by}` : ""}
            </>
          ) : (
            "No mapping saved yet: start from this example."
          )}
        </span>
        <span className="ml-auto" />
        <Field label="Format" hideLabel>
          <Select
            aria-label={`${KIND_INFO[kind].title} format`}
            value={format}
            onChange={(v) => switchFormat(v === "json" ? "json" : "yaml")}
            options={[
              { value: "yaml", label: "YAML" },
              { value: "json", label: "JSON" },
            ]}
          />
        </Field>
      </div>
      {saved?.problems.length ? (
        <div role="alert" className="rounded-sm border border-danger/40 bg-danger-soft px-3 py-2 text-ui text-danger">
          <p className="font-medium">The saved mapping no longer passes the tool checks (a tool was reclassified or disabled since):</p>
          <ul className="list-disc pl-5 text-caption">
            {saved.problems.slice(0, 10).map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <LazyCodeEditor value={text} onChange={setText} language={format} diagnostics={checked.diagnostics} label={`${KIND_INFO[kind].title} (${format.toUpperCase()})`} describedBy={descId} />
      <DiagnosticList id={descId} diagnostics={checked.diagnostics} />
      {summaryOf(kind, checked.value) ? <p className="text-caption text-fg-muted">{summaryOf(kind, checked.value)}</p> : null}
      {saveError ? (
        <div className="flex flex-col gap-1">
          <InlineError message={saveError.conflict ? "Someone saved a newer version of this mapping meanwhile." : saveError.message} />
          {saveError.details.length ? (
            <ul className="text-caption text-danger">
              {saveError.details.map((d) => (
                <li key={d}>{d}</li>
              ))}
            </ul>
          ) : null}
          {saveError.conflict ? (
            <div>
              <Button size="sm" onClick={onSaved}>
                Load the latest version
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}
      {testError ? <InlineError message={testError} /> : null}
      {preview ? <ReadPreviewView preview={preview.result} draft={preview.draft} /> : null}
      <div className="flex flex-wrap justify-end gap-2">
        {kind === "read" ? (
          <Button
            size="sm"
            variant="secondary"
            icon={<TestTube2 className="size-4" />}
            loading={testing}
            disabled={(dirty && !checked.value) || (!saved && !checked.value)}
            onClick={() => void test()}
          >
            Test with one page
          </Button>
        ) : null}
        <Button size="sm" variant="ghost" disabled={text === baseline || saving} onClick={() => setText(baseline)}>
          Discard changes
        </Button>
        <Button size="sm" variant="primary" icon={<Save className="size-4" />} disabled={!dirty || errors > 0 || !checked.value} loading={saving} onClick={() => void save()}>
          Save mapping
        </Button>
      </div>
    </div>
  );
}

/** `POST …/mappings/read/test` result: rows mapped per entity, a few sample rows, the first error. */
function ReadPreviewView({ preview, draft }: { preview: ReadPreview; draft: boolean }) {
  return (
    <div className="flex flex-col gap-2 rounded-sm border border-border p-3" data-read-preview="" aria-live="polite">
      <p className="flex flex-wrap items-center gap-2 text-ui">
        <Badge size="sm" tone={preview.ok ? "success" : "danger"} dot>
          {preview.ok ? "Mapped" : "Problems found"}
        </Badge>
        <span className="text-fg-muted">
          Test of the {draft ? "unsaved draft" : "saved mapping"}: one page per source, nothing stored
          {typeof preview.pages === "number" ? ` · ${preview.pages} ${preview.pages === 1 ? "page" : "pages"} read` : ""}
        </span>
      </p>
      {preview.error ? <InlineError message={preview.error} /> : null}
      {preview.entities.map((e, i) => {
        const cols = [...new Set(e.sample.flatMap((r) => Object.keys(r)))];
        return (
          <details key={`${e.entity}-${i}`} open={i === 0 || Boolean(e.error)}>
            <summary className="cursor-pointer text-ui">
              <span className="font-mono">{e.entity}</span>
              {e.where ? <span className="text-caption text-fg-muted"> ({e.where})</span> : null}: {e.rows} {e.rows === 1 ? "row" : "rows"}
              {e.error ? <span className="text-danger"> · {e.error}</span> : null}
            </summary>
            {e.sample.length ? (
              <div className="mt-1 max-w-full overflow-x-auto">
                <table className="min-w-full text-caption">
                  <caption className="sr-only">Sample rows of {e.entity}</caption>
                  <thead>
                    <tr>
                      {cols.map((c) => (
                        <th key={c} scope="col" className="px-2 py-1 text-left font-mono font-medium text-fg-muted">
                          {c}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {e.sample.map((r, j) => (
                      <tr key={j} className="border-t border-border">
                        {cols.map((c) => (
                          <td key={c} className="px-2 py-1 font-mono break-all">
                            {r[c] === null || r[c] === undefined ? "—" : typeof r[c] === "object" ? JSON.stringify(r[c]) : String(r[c])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </details>
        );
      })}
      {preview.truncated.length ? <p className="text-caption text-fg-muted">More pages exist for {preview.truncated.join(", ")} (the test reads one).</p> : null}
    </div>
  );
}

function DryRunResultView({ result }: { result: DryRunResult }) {
  return (
    <div className="flex flex-col gap-2" data-dry-run-result="" aria-live="polite">
      <p className="flex flex-wrap items-center gap-2 text-ui">
        <Badge size="sm" tone="neutral">
          Not executed
        </Badge>
        {result.mapped ? (
          <>
            <span>
              <span className="font-medium">{opLabel(result.op)}</span> would call <code className="font-mono">{result.call?.tool}</code>
            </span>
            {result.idempotency ? (
              <Badge size="sm" tone={result.idempotency === "enforced" ? "success" : result.idempotency === "none" ? "warning" : "info"}>
                Idempotency: {result.idempotency.replace("_", " ")}
              </Badge>
            ) : null}
            {result.request_id_arg ? <span className="text-caption text-fg-muted">request id in “{result.request_id_arg}”</span> : null}
          </>
        ) : (
          <span>
            <span className="font-medium">{opLabel(result.op)}</span> is not mapped: it becomes a guided step.
          </span>
        )}
      </p>
      {result.guided_reason ? <p className="text-ui text-fg-muted">{result.guided_reason}</p> : null}
      {result.call ? (
        <div>
          <p className="text-caption font-medium text-fg-muted">Rendered arguments</p>
          <pre className="max-h-64 overflow-auto rounded-sm bg-surface-2 p-2 font-mono text-caption" aria-label="Rendered arguments">
            {JSON.stringify(result.call.args, null, 2)}
          </pre>
        </div>
      ) : null}
      {result.readback ? (
        <div className="flex flex-col gap-1">
          <p className="text-caption font-medium text-fg-muted">
            Read-back: <code className="font-mono">{result.readback.call.tool}</code>
            {result.readback.executed ? " (query run)" : " (not run)"}
          </p>
          <pre className="max-h-40 overflow-auto rounded-sm bg-surface-2 p-2 font-mono text-caption" aria-label="Read-back arguments">
            {JSON.stringify(result.readback.call.args, null, 2)}
          </pre>
          {result.readback.executed ? (
            <p className="text-ui">
              {result.readback.ok === false
                ? "The read-back query failed."
                : `${result.readback.items_found ?? 0} ${result.readback.items_found === 1 ? "record" : "records"} found${result.readback.matched === true ? "; one already matches this change." : result.readback.matched === false ? "; none matches this change yet." : "."}`}
              {result.readback.detail ? <span className="text-fg-muted"> {result.readback.detail}</span> : null}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** Admin dry-run (`POST …/mappings/action/dry-run`): renders the call for a sample action without executing it. */
function DryRunPanel({ slug, ops, savedVersion }: { slug: string; ops: readonly string[]; savedVersion: number | null }) {
  const admin = useAdminApi();
  const [op, setOp] = useState(ops[0] ?? "");
  const [payload, setPayload] = useState('{\n  "from_dc": "DC-1",\n  "to_dc": "DC-2",\n  "lines": [{ "sku": "SKU-1", "qty": 10 }]\n}');
  const [targetRef, setTargetRef] = useState("");
  const [expected, setExpected] = useState("");
  const [readback, setReadback] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DryRunResult | null>(null);
  const parsed = parsePayload(payload);
  const expectedNum = expected.trim() === "" ? null : Number(expected);
  const expectedBad = expectedNum !== null && (!Number.isInteger(expectedNum) || expectedNum < 1);
  const opOk = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$/.test(op) && op.length <= 64;
  const options = [...new Set([...ops, ...(op && !ops.includes(op) ? [op] : [])])];

  const run = async () => {
    if (!("value" in parsed) || expectedBad || !opOk) return;
    setBusy(true);
    setError(null);
    try {
      setResult(
        await admin.dryRun(slug, {
          op,
          payload: parsed.value,
          target_ref: targetRef.trim() || null,
          expected_version: expectedNum,
          run_readback: readback,
        }),
      );
    } catch (err) {
      setResult(null);
      setError(adminError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AdminSection
      title="Dry run"
      description={`Renders the vendor call for a sample change and runs only the read-back query. Nothing is changed in the vendor system. Uses the saved action mapping${savedVersion ? ` (v${savedVersion})` : ""}: save your edits first.`}
    >
      <div className="grid gap-3 md:grid-cols-2">
        <Field label="Change" description="A mapped op, or type any canonical op to see that it is guided.">
          <TextInput value={op} list="dry-run-ops" mono onChange={(e) => setOp(e.target.value.trim())} aria-invalid={!opOk || undefined} />
        </Field>
        <datalist id="dry-run-ops">
          {options.map((o) => (
            <option key={o} value={o}>
              {opLabel(o)}
            </option>
          ))}
        </datalist>
        <Field label="Target record (optional)" description="{{target_ref}} in the template.">
          <TextInput value={targetRef} mono maxLength={200} onChange={(e) => setTargetRef(e.target.value)} />
        </Field>
        <Field label="Expected version (optional)" error={expectedBad ? "A whole number, 1 or more." : undefined} description="{{expected_version}} in the template.">
          <TextInput value={expected} inputMode="numeric" onChange={(e) => setExpected(e.target.value)} />
        </Field>
        <div className="flex items-end">
          <Checkbox checked={readback} onCheckedChange={setReadback} label="Run the read-back query" description="Read-only: shows whether a matching record already exists." />
        </div>
      </div>
      <Field label="Sample payload (JSON)" error={"error" in parsed ? parsed.error : undefined} description="{{payload.*}} in the template.">
        <Textarea value={payload} rows={6} className="font-mono text-caption" onChange={setPayload} />
      </Field>
      <div>
        <Button size="sm" variant="primary" icon={<FlaskConical className="size-4" />} loading={busy} disabled={!opOk || "error" in parsed || expectedBad} onClick={() => void run()}>
          Run dry run
        </Button>
      </div>
      {error ? <InlineError message={error} /> : null}
      {result ? <DryRunResultView result={result} /> : null}
    </AdminSection>
  );
}

/**
 * **Mappings** of a tier-A connector (`docs/contracts/110` §4): read and action mapping editors
 * (CodeMirror 6, YAML or JSON, validated against the contract as you type), the capability summary
 * and the action dry-run.
 */
export function MappingsPage({ slug }: { slug: string }) {
  const admin = useAdminApi();
  const [tab, setTab] = useState<MappingKind>("read");
  const connector = useResource(() => admin.connector(slug), `connector:${slug}`);
  const tools = useResource(() => admin.connectorTools(slug), `tools:${slug}`);
  const read = useResource(() => admin.mapping(slug, "read"), `mapping:${slug}:read`);
  const action = useResource(() => admin.mapping(slug, "action"), `mapping:${slug}:action`);
  const res = tab === "read" ? read : action;
  const savedAction = action.data ? mappingActionSchema.safeParse(action.data.mapping) : null;
  const actionOps = mappedOps(savedAction?.success ? savedAction.data : null);

  return (
    <>
      <AdminPageHeader
        title={`Mappings · ${connector.data?.name ?? slug}`}
        description="How Keelson reads this system's data into scn and how canonical changes become vendor calls."
        actions={
          <Link href={`/admin/connectors/${encodeURIComponent(slug)}`} className={cn(linkClass, "text-ui")}>
            Back to the connector
          </Link>
        }
      />
      <CapabilitiesSection slug={slug} />
      <AdminSection title={KIND_INFO[tab].title} description={KIND_INFO[tab].description}>
        <Tabs
          label="Mapping"
          value={tab}
          onValueChange={(v) => setTab(v === "action" ? "action" : "read")}
          tabs={[
            { id: "read", label: "Read mapping", badge: read.data ? `v${read.data.version ?? "?"}` : undefined },
            { id: "action", label: "Action mapping", badge: action.data ? `v${action.data.version ?? "?"}` : undefined },
          ]}
          panelClassName="pt-3"
        >
          <ResourceView resource={res} label={`the ${tab} mapping`}>
            {(saved) => (
              <MappingEditor
                key={`${tab}:${saved?.version ?? "new"}`}
                slug={slug}
                kind={tab}
                saved={saved}
                tools={tools.data ?? null}
                onSaved={() => res.reload()}
              />
            )}
          </ResourceView>
        </Tabs>
      </AdminSection>
      {tab === "action" ? <DryRunPanel slug={slug} ops={actionOps} savedVersion={action.data?.version ?? null} /> : null}
    </>
  );
}
