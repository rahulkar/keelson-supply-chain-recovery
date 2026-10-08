"""Keelson engine runtime (E2): the ADK 2.11 workflow, its LLM/tool plumbing and the
deterministic nodes that frame, simulate, compose and commit a case.

Public entry points (``docs/contracts/55-engine-internals.md`` "E2 provides"):

* :func:`keelson_engine.runtime.execute.run_case` — drive one queued run;
* :func:`keelson_engine.runtime.runner_cache.get_runner` — refcounted ``(App, Runner)`` cache;
* :func:`keelson_engine.runtime.workflow.build_workflow` — the M1 graph
  (``docs/contracts/50-engine-workflow.md``).

Submodules are imported explicitly; this package stays import-light.
"""
