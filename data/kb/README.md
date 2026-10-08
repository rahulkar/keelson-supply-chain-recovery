# data/kb — knowledge base seed documents

All documents here are **synthetic and fictional** (contract `docs/contracts/90-m4-investigation.md` §7).

- `<tenant slug>/*.md` — ingested by the engine bootstrap into that tenant's "Playbooks & policies"
  knowledge base, only when the tenant has no knowledge-base documents yet (`KB_SEED_DIR`, default
  `/app/kb`). `demo-retail/` holds the five demo documents (peak trading playbook, Meghna Trims
  supply agreement extract, freight & expedite policy, responsible sourcing code with the approved
  near-shore suppliers F-TR-01 / F-MA-01, DC-to-DC transfer SOP).
- `adversarial/` — eval fixtures with prompt-injection attempts. There is no tenant with this slug,
  so they are never seeded; evals upload them explicitly.
