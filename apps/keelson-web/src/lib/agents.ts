/**
 * What each LLM role does, in one plain sentence (the timeline's agent rows). Display names come
 * from the engine (`AGENT_DISPLAY_NAMES`); unknown/custom specialists get no description.
 */
export const AGENT_ROLE_DESCRIPTIONS: Record<string, string> = {
  intake: "Reads your message and frames the disruption",
  inventory_demand: "Checks stock, demand and open purchase orders",
  sourcing: "Checks suppliers, factories and components",
  logistics: "Checks lanes, ports and transit times",
  playbook: "Checks your playbooks, contracts and policies",
  planner: "Plans which checks to run",
  recommender: "Weighs the options and recommends one",
  critic: "Challenges the findings and the recommendation",
};

export function agentDescription(slug: string): string {
  return AGENT_ROLE_DESCRIPTIONS[slug] ?? "";
}
