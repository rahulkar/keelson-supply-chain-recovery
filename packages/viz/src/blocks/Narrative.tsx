"use client";

import type { NarrativeProps } from "@keelson/contracts";
import { Badge } from "@keelson/ui";
import { ExternalLink, History } from "lucide-react";
import type { AnchorHTMLAttributes } from "react";
import { defaultRehypePlugins, Streamdown, type Components } from "streamdown";
import { ALLOWED_LINK_PREFIXES, isAllowedHref } from "../links";
import type { BlockComponentProps } from "../types";
import { planStatusLabel, ReplanNotice } from "./PlanVersions";
import { AdvisoryNote } from "./Advisory";

type Pluggable = NonNullable<Parameters<typeof Streamdown>[0]["rehypePlugins"]>[number];

const harden = (defaultRehypePlugins["harden"] as [unknown, unknown])[0];
const sanitize = defaultRehypePlugins["sanitize"];

/**
 * Hardened pipeline: no raw HTML (skipHtml + no rehype-raw), sanitised hast, links only to the
 * allowlisted hosts (others become plain text), and no images at all.
 */
export const NARRATIVE_REHYPE_PLUGINS = [
  sanitize,
  [
    harden,
    {
      // Relative links resolve against the app origin (still subject to the allowlist).
      defaultOrigin: "http://keelson.localhost",
      allowedLinkPrefixes: ALLOWED_LINK_PREFIXES,
      allowedImagePrefixes: [],
      allowDataImages: false,
      allowedProtocols: ["http", "https"],
      linkBlockPolicy: "text-only",
      imageBlockPolicy: "remove",
    },
  ],
] as Pluggable[];

function SafeLink({ href, children, node: _node, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { node?: unknown }) {
  if (!isAllowedHref(href)) return <span>{children}</span>;
  void rest;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="font-medium text-accent underline underline-offset-4 hover:text-accent-hover"
    >
      {children}
      <ExternalLink aria-hidden="true" className="ml-0.5 inline size-3 align-baseline" />
      <span className="sr-only"> (opens in a new tab)</span>
    </a>
  );
}

const components = {
  a: SafeLink,
  img: () => null,
} as Components;

export type NarrativeMarkdownProps = { markdown: string; className?: string | undefined };

/** Static, hardened markdown (Streamdown 2.7) with Keelson prose styles. */
export function NarrativeMarkdown({ markdown, className }: NarrativeMarkdownProps) {
  return (
    <div className={["k-prose", className].filter(Boolean).join(" ")}>
      <Streamdown
        mode="static"
        skipHtml
        controls={false}
        linkSafety={{ enabled: false }}
        disallowedElements={["img", "iframe", "script", "style", "video", "audio", "object", "embed"]}
        rehypePlugins={NARRATIVE_REHYPE_PLUGINS}
        components={components}
      >
        {markdown}
      </Streamdown>
    </div>
  );
}

/**
 * The compact "What changed" line of a follow-up (M4, `narrative@1` `what_changed`): one line of
 * plain text (never markdown) against the previous plan version.
 */
export function WhatChanged({ text, className }: { text: string | null | undefined; className?: string | undefined }) {
  const line = (text ?? "").replace(/\s+/g, " ").trim();
  if (!line) return null;
  return (
    <p
      className={["flex items-baseline gap-1.5 rounded-sm border-l-2 border-accent bg-surface-2 px-2.5 py-1 text-ui text-fg", className]
        .filter(Boolean)
        .join(" ")}
      data-what-changed=""
    >
      <History aria-hidden="true" className="size-3.5 shrink-0 self-center text-accent" />
      <span>
        <span className="font-medium">What changed:</span> {line}
      </span>
    </p>
  );
}

/**
 * `narrative@1` — the composed assistant narrative (no card chrome; reads as prose). M5: a
 * re-plan `banner` ("New information: … — plan updated (v2)") and the case's `plan_versions`
 * (the app's thread shows the banner with a Compare button; see `ReplanNotice`).
 */
export function Narrative({ props, envelope }: BlockComponentProps<NarrativeProps>) {
  return (
    <section
      aria-label={envelope.title || "Summary"}
      data-block-kind={envelope.kind}
      data-block-id={envelope.id}
      className="flex flex-col gap-2"
    >
      {props.banner ? <ReplanNotice banner={props.banner} /> : null}
      <AdvisoryNote advisory={props.advisory} />
      {props.plan_versions.length > 1 ? (
        <ul aria-label="Plan versions" className="flex flex-wrap gap-1.5">
          {props.plan_versions.map((v) => (
            <li key={v.version}>
              <Badge tone={v.current ? "accent" : "neutral"}>
                {v.label} · {planStatusLabel(v.status)}
                {v.current ? " (current)" : ""}
              </Badge>
            </li>
          ))}
        </ul>
      ) : null}
      <WhatChanged text={props.what_changed} />
      <NarrativeMarkdown markdown={props.markdown} />
    </section>
  );
}
