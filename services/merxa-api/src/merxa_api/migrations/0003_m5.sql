-- M5 (docs/contracts/95): four-eyes co-approvers in attribution and audit.
-- Additive, nullable columns only (no table rewrite; the append-only audit_log triggers
-- guard row UPDATE/DELETE, not DDL).

-- [{ref: "user:<username>", name: "<display>"}] from the approval receipt's co_approved_by
-- claim (+ optional co_approver_names); NULL when the approval had no co-approvers.
ALTER TABLE merxa.change_set ADD COLUMN IF NOT EXISTS co_approvers jsonb NULL;

-- The receipt's co_approved_by refs on every audit row an apply/rollback wrote.
ALTER TABLE merxa.audit_log ADD COLUMN IF NOT EXISTS co_approved_by text[] NULL;
