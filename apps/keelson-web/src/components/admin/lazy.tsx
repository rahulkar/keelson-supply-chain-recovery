"use client";

/**
 * Heavy admin widgets loaded on demand and never rendered on the server: the CodeMirror editor
 * (mapping editors) and the React Flow / elkjs workflow graph (Agents → Workflow).
 */
import { Skeleton } from "@keelson/ui";
import dynamic from "next/dynamic";

export const LazyCodeEditor = dynamic(() => import("./CodeEditor"), {
  ssr: false,
  loading: () => <Skeleton className="h-64 w-full" />,
});

export const LazyWorkflowGraph = dynamic(() => import("@keelson/viz/workflow-graph"), {
  ssr: false,
  loading: () => <Skeleton className="h-96 w-full" />,
});
