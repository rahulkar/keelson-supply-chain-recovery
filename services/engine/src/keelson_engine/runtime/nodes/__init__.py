"""Workflow nodes, one module per node group (``docs/contracts/50-engine-workflow.md``,
``90-m4-investigation.md``; ``M4_NODES.md`` and ``M4_INVESTIGATION.md`` in this package):

* ``routing`` — pre_route, route_intake, reject_run, compose_smalltalk, close_case (+ the M1
  promote_case stand-in)
* ``framing`` — frame_case, quick_exposure, approach_gate, approach_router, promote_case,
  confirm_assumptions (``taxonomy``: the event-type templates)
* ``investigate`` — planner, investigate (dependency waves) + the specialist wrappers
* ``review`` — verify_numbers, critic (shared REVISE budget)
* ``refine`` — refine_scenario
* ``followup`` — followup_delta, explain, prepare_resimulate, prepare_reinvestigate
* ``planning`` — impact, simulate, compose
* ``case_state`` — restores the planning state in later runs of a case
* ``m4_defaults`` — conservative stand-ins for framing-owned nodes a build does not provide
* ``answer`` — compose_answer
* ``commit_nodes`` — compile_changeset, dry_run, approval_gate, approval_check, execute, reconcile
"""
