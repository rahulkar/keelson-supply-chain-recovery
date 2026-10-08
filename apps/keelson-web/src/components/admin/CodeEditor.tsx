"use client";

/**
 * CodeMirror 6 editor for YAML / JSON (admin mapping editors, `docs/contracts/110` §4). Client
 * only: load it with `next/dynamic(…, { ssr: false })`. Diagnostics come from the caller (parsed
 * and validated against the contract schemas in `lib/admin/mappings.ts`) and are shown in the
 * gutter and underlined; Ctrl/Cmd-Shift-M opens the list. Tab is **not** captured (keyboard users
 * can leave the editor); indentation uses the editor's own commands.
 */
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { json } from "@codemirror/lang-json";
import { yaml } from "@codemirror/lang-yaml";
import { bracketMatching, defaultHighlightStyle, indentOnInput, syntaxHighlighting } from "@codemirror/language";
import { lintGutter, lintKeymap, setDiagnostics, type Diagnostic as CmDiagnostic } from "@codemirror/lint";
import { Compartment, EditorState } from "@codemirror/state";
import { EditorView, highlightActiveLine, highlightActiveLineGutter, keymap, lineNumbers } from "@codemirror/view";
import { useEffect, useRef } from "react";
import type { Diagnostic } from "@/lib/admin/mappings";

export type CodeEditorProps = {
  value: string;
  onChange: (value: string) => void;
  language: "yaml" | "json";
  diagnostics: readonly Diagnostic[];
  /** Accessible name of the editing area. */
  label: string;
  /** Id of the element describing the editor (e.g. the problem summary). */
  describedBy?: string | undefined;
  readOnly?: boolean | undefined;
  /** CSS height of the scroller (e.g. "24rem"). */
  height?: string | undefined;
};

const theme = EditorView.theme({
  "&": { fontSize: "0.8125rem", backgroundColor: "var(--color-surface)", color: "var(--color-fg)", borderRadius: "var(--radius-sm, 6px)" },
  "&.cm-focused": { outline: "2px solid var(--color-focus)", outlineOffset: "1px" },
  ".cm-content": { fontFamily: "var(--font-mono, ui-monospace, monospace)", caretColor: "var(--color-fg)" },
  ".cm-gutters": { backgroundColor: "var(--color-surface-2)", color: "var(--color-fg-muted)", borderRight: "1px solid var(--color-border)" },
  ".cm-activeLine": { backgroundColor: "color-mix(in oklab, var(--color-accent) 6%, transparent)" },
  ".cm-activeLineGutter": { backgroundColor: "color-mix(in oklab, var(--color-accent) 10%, transparent)" },
  ".cm-scroller": { overflow: "auto" },
});

function toCm(diagnostics: readonly Diagnostic[], length: number): CmDiagnostic[] {
  return diagnostics.map((d) => {
    const from = Math.max(0, Math.min(d.from, length));
    const to = Math.max(from, Math.min(d.to, length));
    return { from, to, severity: d.severity, message: d.message };
  });
}

/**
 * The content element carries the label and an explicit `tabindex` so the scroller always has a
 * keyboard-focusable child (axe `scrollable-region-focusable`; a read-only editor has no
 * `contenteditable` to make it focusable otherwise).
 */
function contentAttrs(label: string, describedBy: string | undefined): Record<string, string> {
  return { "aria-label": label, tabindex: "0", ...(describedBy ? { "aria-describedby": describedBy } : {}) };
}

export default function CodeEditor({ value, onChange, language, diagnostics, label, describedBy, readOnly = false, height = "24rem" }: CodeEditorProps) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const onChangeRef = useRef(onChange);
  const lang = useRef(new Compartment());
  const attrs = useRef(new Compartment());
  const editable = useRef(new Compartment());
  useEffect(() => {
    onChangeRef.current = onChange;
  });

  // Create once; later prop changes are dispatched into the same view.
  useEffect(() => {
    if (!host.current) return;
    const v = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          lineNumbers(),
          highlightActiveLineGutter(),
          highlightActiveLine(),
          history(),
          indentOnInput(),
          bracketMatching(),
          syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
          lintGutter(),
          keymap.of([...defaultKeymap, ...historyKeymap, ...lintKeymap]),
          lang.current.of(language === "json" ? json() : yaml()),
          attrs.current.of(EditorView.contentAttributes.of(contentAttrs(label, describedBy))),
          editable.current.of([EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)]),
          EditorView.updateListener.of((u) => {
            if (u.docChanged) onChangeRef.current(u.state.doc.toString());
          }),
          EditorView.lineWrapping,
          theme,
          EditorView.theme({ ".cm-scroller": { height, minHeight: "8rem" } }),
        ],
      }),
    });
    view.current = v;
    return () => {
      v.destroy();
      view.current = null;
    };
    // The view is created once per mount; the effects below keep it in sync with the props.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // External value changes (load, YAML ⇄ JSON conversion, discard).
  useEffect(() => {
    const v = view.current;
    if (!v) return;
    const current = v.state.doc.toString();
    if (current !== value) v.dispatch({ changes: { from: 0, to: current.length, insert: value } });
  }, [value]);

  useEffect(() => {
    view.current?.dispatch({ effects: lang.current.reconfigure(language === "json" ? json() : yaml()) });
  }, [language]);

  useEffect(() => {
    view.current?.dispatch({
      effects: [
        attrs.current.reconfigure(EditorView.contentAttributes.of(contentAttrs(label, describedBy))),
        editable.current.reconfigure([EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)]),
      ],
    });
  }, [label, describedBy, readOnly]);

  useEffect(() => {
    const v = view.current;
    if (!v) return;
    v.dispatch(setDiagnostics(v.state, toCm(diagnostics, v.state.doc.length)));
  }, [diagnostics, value]);

  return <div ref={host} className="overflow-hidden rounded-sm border border-border-strong" data-code-editor={language} />;
}
