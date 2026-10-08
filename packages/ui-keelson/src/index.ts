/**
 * @keelson/ui — presentational, accessible primitives (Base UI under the hood). No data fetching.
 * Styles: import "@keelson/ui/styles.css" after "@keelson/tokens/theme.css" in the app stylesheet.
 */
export { cn } from "./lib/cn";
export { formatElapsed, initials } from "./lib/format";
export { CANVAS_SIDE_QUERY, useMediaQuery, usePrefersReducedMotion } from "./lib/hooks";

export { Button, IconButton, type ButtonProps, type ButtonSize, type ButtonVariant, type IconButtonProps } from "./components/Button";
export { Card, CardFooter, CardHeader, EmptyState, type CardHeaderProps, type CardProps, type EmptyStateProps } from "./components/Card";
export { Badge, Chip, type BadgeProps, type BadgeTone, type ChipProps } from "./components/Badge";
export { ProvenanceChip, type Prov, type ProvenanceChipProps } from "./components/ProvenanceChip";
export {
  Avatar,
  AvatarStack,
  type AvatarAgent,
  type AvatarPerson,
  type AvatarProps,
  type AvatarSize,
  type AvatarStackProps,
} from "./components/Avatar";
export { Tabs, TabList, type TabItem, type TabsProps } from "./components/Tabs";
export { Dialog, Sheet, type SheetProps } from "./components/Dialog";
export { Tooltip, TooltipProvider, type TooltipProps } from "./components/Tooltip";
export { Shimmer, Skeleton, type ShimmerProps, type SkeletonProps } from "./components/Shimmer";
export { Spinner, type SpinnerProps } from "./components/Spinner";
export {
  Composer,
  SuggestionChips,
  type ComposerHandle,
  type ComposerProps,
  type Suggestion,
  type SuggestionChipsProps,
} from "./components/Composer";
export { StageChips, type StageChipsProps, type StageItem, type StageState } from "./components/StageChips";
export {
  ACTIVITY_PREVIEW,
  MANY_AGENTS,
  ThinkingTimeline,
  agentsSummary,
  groupActivities,
  type ThinkingActivity,
  type ThinkingActivityGroup,
  type ThinkingAgent,
  type ThinkingStatus,
  type ThinkingTimelineProps,
} from "./components/ThinkingTimeline";
export { MessageBubble, type MessageBubbleProps, type MessageRole } from "./components/MessageBubble";
export { Toaster, createBufferedToastManager, toast, toastManager, type ToastOptions, type ToastType } from "./components/Toast";
export { CanvasPanel, type CanvasPanelProps, type CanvasTab } from "./components/CanvasPanel";
export { UiProvider, type UiProviderProps } from "./components/Provider";

// Admin / back-office primitives (M2): dense forms, tables, step logs, diffs.
export type { BadgeSize, BadgeVariant } from "./components/Badge";
export {
  Checkbox,
  Field,
  Select,
  Switch,
  Textarea,
  TextInput,
  counterAnnouncement,
  type CheckboxProps,
  type FieldProps,
  type SelectOption,
  type SelectProps,
  type SwitchProps,
  type TextareaProps,
  type TextInputProps,
} from "./components/Field";
export { Table, type TableColumn, type TableProps, type TableRowTone } from "./components/Table";
export { StepLog, stepLogAnnouncement, type StepLogItem, type StepLogProps, type StepState } from "./components/StepLog";
export { JsonDiff, type JsonDiffProps } from "./components/JsonDiff";
export { diffJson, formatJsonValue, jsonEqual, type DiffEntry, type DiffKind, type JsonValue } from "./lib/jsonDiff";
export { Meter, type MeterProps } from "./components/Meter";
export { ConfirmDialog, Drawer, type ConfirmDialogProps, type DrawerProps } from "./components/Dialog";
