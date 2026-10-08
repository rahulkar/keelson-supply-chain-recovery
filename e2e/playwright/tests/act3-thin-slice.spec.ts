/**
 * Golden journey "act3-thin-slice": the M1 thin slice in a real browser against the real stack
 * (Keycloak, engine + Gemini via LiteLLM, optimizer, Merxa). Mirrors `e2e/api_thin_slice.py`,
 * but every step is driven through the UI the way a presenter would.
 *
 * M4 (docs/contracts/90 §2–§6): the approach card lists the planner's checks and the agents it will
 * consult; the thinking timeline shows several specialists working in parallel plus the critic; a
 * follow-up ("What if the delay doubles?") re-simulates without re-investigating and shows a
 * "What changed" line; values left unconfirmed are confirmed before Apply.
 */
import { disconnectMerxa, merxaChangeSet, resetMerxa } from "./support/api";
import { KEELSON, MERXA, RUN_TIMEOUT_MS, TRIM_DELAY_PROMPT } from "./support/env";
import { elementScreenshot, expect, keyScreenshot, test } from "./support/fixtures";
import {
  RAW_TOOL_NAME,
  block,
  canvas,
  canvasVisuals,
  caseIdFromUrl,
  expectCalmTurns,
  keycloakLogin,
  switchPersona,
  timeline,
  turns,
  waitForBlock,
} from "./support/keelson";

test.describe.configure({ mode: "serial" });

/** M4 specialist agent rows (AGENT_DISPLAY_NAMES; tenant display-name overrides end in "analyst"). */
const SPECIALIST_ROW = /^[\w &]+ analyst\b/i;

test("act3-thin-slice", async ({ page, context, tracer }) => {
  test.setTimeout(15 * 60_000);

  await test.step("demo reset: Merxa golden seed, Marco's Merxa link disconnected", async () => {
    await resetMerxa();
    await disconnectMerxa("marco");
  });

  await test.step("sign in (Keycloak, keelson realm) and switch persona to Priya", async () => {
    await page.goto(`${KEELSON}/`);
    await keycloakLogin(page, tracer, "keelson", "sam");
    await expect(page).toHaveURL(`${KEELSON}/`);
    await switchPersona(page, "Priya Shah");
    await expect(page.getByRole("heading", { level: 1, name: "Hello, Priya" })).toBeVisible();
    await keyScreenshot(page, "01-home");
  });

  let caseId = "";
  await test.step("describe the trim delay on Home", async () => {
    const box = page.getByRole("textbox", { name: "Describe a disruption or ask a question" });
    await expect(box).toBeFocused();
    await box.fill(TRIM_DELAY_PROMPT);
    await box.press("Enter");
    await page.waitForURL(/\/cases\/[^/]+$/, { timeout: 30_000 });
    caseId = caseIdFromUrl(page);
    test.info().annotations.push({ type: "case", description: caseId });
    await expect(page.getByText(TRIM_DELAY_PROMPT)).toBeVisible();
  });

  await test.step("calm thinking timeline: stage chips progress", async () => {
    const first = timeline(page, 1);
    await expect(first).toBeVisible({ timeout: 30_000 });
    // Collapsed by default: one status line, no agent list.
    await expect(first.getByRole("list", { name: "Agents consulted" })).toHaveCount(0);
    await first.getByRole("button", { name: /Show thinking/ }).click();
    const stages = first.getByRole("list", { name: "Progress" });
    await expect(stages.getByRole("listitem").first()).toBeVisible();
    await expect(stages).toContainText("Understanding");
  });

  // The investigation runs in the resumed run (turn 2), or in turn 1 when the approach gate
  // auto-confirms (all facts, few matches).
  let analysisTurn = 2;
  await test.step("approach card → Looks right", async () => {
    const approach = block(turns(page), "approach_card").first();
    const outcome = await Promise.race([
      approach
        .getByRole("button", { name: "Looks right" })
        .waitFor({ state: "visible", timeout: RUN_TIMEOUT_MS })
        .then(() => "card" as const)
        .catch(() => "timeout" as const),
      block(turns(page), "recommended_option")
        .first()
        .waitFor({ state: "visible", timeout: RUN_TIMEOUT_MS })
        .then(() => "auto" as const)
        .catch(() => "timeout" as const),
    ]);
    expect(outcome, "approach card or results").not.toBe("timeout");
    if (outcome === "card") {
      const confirm = approach.getByRole("button", { name: "Looks right" });
      await expect(confirm).toBeEnabled({ timeout: 30_000 });
      // Paused for the approach: the first timeline says so, calmly (no shimmer).
      await expect(timeline(page, 1)).toHaveAttribute("data-status", "paused");
      await approach.scrollIntoViewIfNeeded();
      await keyScreenshot(page, "03-approach-card");
      // M4: the plan as the user reads it — one check per task and the agents to be consulted.
      const consult = approach.getByRole("region", { name: "Who I'll consult" });
      await expect(consult).toBeVisible();
      const checks = approach.getByRole("region", { name: "What I'll check" }).getByRole("listitem");
      expect(await checks.count(), "planned checks on the approach card").toBeGreaterThanOrEqual(3);
      for (const text of await checks.allInnerTexts()) expect(text, "check is a plain sentence").not.toMatch(/\{\{|_[a-z]+_/);
      test.info().annotations.push({ type: "agents", description: (await consult.innerText()).replace(/\s+/g, " ") });
      await elementScreenshot(approach, "17-approach-plan");
      await confirm.click();
      await expect(confirm).toBeDisabled({ timeout: 30_000 });
    } else {
      analysisTurn = 1;
      test.info().annotations.push({ type: "note", description: "approach auto-confirmed by the engine" });
    }
  });

  await test.step("thinking expanded: stages progress, agents with humanised activities", async () => {
    const tl = timeline(page, analysisTurn);
    await expect(tl).toBeVisible({ timeout: 60_000 });
    const toggle = tl.getByRole("button", { name: /Show thinking/ });
    if (await toggle.isVisible()) await toggle.click();
    const stages = tl.getByRole("list", { name: "Progress" });
    // Stage chips move: an active stage while running, then later stages done.
    const sawActive = await stages
      .locator('[aria-current="step"]')
      .first()
      .waitFor({ state: "visible", timeout: 30_000 })
      .then(() => true)
      .catch(() => false);
    const analyst = tl.getByRole("button", { name: /^Inventory & demand analyst/ });
    await expect(analyst).toBeVisible({ timeout: RUN_TIMEOUT_MS });
    await analyst.click();
    const activities = tl.getByRole("list", { name: "Inventory & demand analyst activity" });
    await expect(activities.getByRole("listitem").first()).toBeVisible({ timeout: RUN_TIMEOUT_MS });
    await expect(stages.locator('[data-state="done"]').first()).toBeVisible({ timeout: RUN_TIMEOUT_MS });
    test.info().annotations.push({ type: "stages", description: (await stages.innerText()).replace(/\s+/g, " ") });
    expect(sawActive || (await tl.getAttribute("data-status")) === "done", "an active stage chip while running").toBe(true);
    await keyScreenshot(page, "02-thinking-expanded");
    const labels = await activities.getByRole("listitem").allInnerTexts();
    expect(labels.length).toBeGreaterThan(0);
    for (const label of labels) {
      expect(label, "activity is a sentence").toMatch(/^[A-Z].*\s/);
      expect(label, "no raw tool names").not.toMatch(RAW_TOOL_NAME);
    }
    expect(await tl.innerText()).not.toMatch(RAW_TOOL_NAME);

    // M4: several specialists in parallel, then the critic (each its own agent row).
    const agentList = tl.getByRole("list", { name: "Agents consulted" });
    await expect(tl.getByRole("button", { name: /^Critic/ })).toBeVisible({ timeout: RUN_TIMEOUT_MS });
    // Row buttons are named by the agent's display name (the avatar initials are aria-hidden).
    const specialists = agentList.getByRole("button", { name: SPECIALIST_ROW });
    const names = await agentList
      .locator(":scope > li[data-agent]")
      .evaluateAll((els) => els.map((e) => e.getAttribute("data-agent") ?? ""));
    test.info().annotations.push({ type: "agent rows", description: names.join(" | ") });
    expect(await specialists.count(), `specialist agent rows (${names.join(", ")})`).toBeGreaterThanOrEqual(3);
    const sourcing = tl.getByRole("button", { name: /^(Sourcing|Logistics|Playbook) analyst/ }).first();
    if ((await sourcing.getAttribute("aria-expanded")) === "false") await sourcing.click();
    await tl.scrollIntoViewIfNeeded();
    await keyScreenshot(page, "18-thinking-multi-agent");
  });

  await test.step("results: narrative, KPI strip, recommended option, canvas with the plan diff", async () => {
    const option = await waitForBlock(page, "recommended_option");
    // `has` takes a locator relative to each turn.
    const resultTurn = turns(page).filter({ has: page.locator('[data-block-kind="recommended_option"]') }).last();
    // Wait for the run to finish (narrative composed, timeline done).
    await expect(resultTurn.locator("section[data-status]").first()).toHaveAttribute("data-status", "done", {
      timeout: RUN_TIMEOUT_MS,
    });
    await expect(block(resultTurn, "kpi_strip")).toBeVisible();
    const apply = option.getByRole("button", { name: "Apply changes for me" });
    await expect(apply).toBeVisible();
    const thread = await turns(page).allInnerTexts();
    for (const text of thread) expect(text, "no unresolved placeholders").not.toMatch(/\{\{|\}\}/);
    const narrative = resultTurn.locator(".k-prose").first();
    await expect(narrative).toBeVisible();
    expect((await narrative.innerText()).trim().length, "composed narrative").toBeGreaterThan(40);
    await expect(narrative).not.toContainText("{{");
    await expectCalmTurns(page);

    // Canvas: auto-opens on the first heavy result on wide screens; open it otherwise.
    const panel = canvas(page);
    if (!(await panel.isVisible())) await page.getByRole("button", { name: "Open canvas" }).click();
    await expect(panel).toBeVisible();
    await expect(block(panel, "plan_diff")).toBeVisible({ timeout: 30_000 });
    await expect(apply).toBeEnabled();
    await keyScreenshot(page, "04-results-canvas");
  });

  await test.step("canvas visuals: option comparison, network map, impact graph, stock-out timeline", async () => {
    const panel = canvas(page);
    const visuals = await canvasVisuals(page, panel);
    const order = await panel.locator("[data-block-kind]").evaluateAll((els) => els.map((e) => e.getAttribute("data-block-kind")));
    // Result tab: the option comparison first, then map, graph, timeline, plan diff (80 §6).
    const want = ["scenario_compare", "network_map", "impact_graph", "stockout_timeline", "plan_diff"];
    expect(order.filter((k) => k && want.includes(k)), "canvas order").toEqual(want);
    // The comparison marks the recommended option and lists Do nothing.
    await expect(visuals.scenario_compare).toContainText(/Recommended/i);
    await expect(visuals.scenario_compare).toContainText("Do nothing");
    // A tall viewport so every visual fits on screen whole (an element screenshot of a block
    // taller than the viewport inside the scrolling canvas would be cut off).
    const viewport = page.viewportSize();
    await page.setViewportSize({ width: 1280, height: 1600 });
    try {
      await canvasVisuals(page, panel);
      for (const [kind, name] of [
        ["scenario_compare", "13-scenario-compare"],
        ["network_map", "14-network-map"],
        ["impact_graph", "15-impact-graph"],
        ["stockout_timeline", "16-stockout-timeline"],
      ] as const) {
        await elementScreenshot(visuals[kind], name);
      }
      // The impact graph opens on actual nodes (not an empty corner of a wide layout).
      const graphBox = await visuals.impact_graph.locator("[data-graph-canvas]").boundingBox();
      const onScreen = await visuals.impact_graph.locator(".react-flow__node").evaluateAll(
        (els, box) =>
          box
            ? els.filter((el) => {
                const r = el.getBoundingClientRect();
                return r.bottom > box.y && r.top < box.y + box.height && r.right > box.x && r.left < box.x + box.width;
              }).length
            : 0,
        graphBox,
      );
      expect(onScreen, "impact graph nodes in the opening view").toBeGreaterThan(0);
    } finally {
      if (viewport) await page.setViewportSize(viewport);
    }
    if (await visuals.network_map.getByText(/background map couldn.t be loaded/).isVisible()) {
      test.info().annotations.push({ type: "note", description: "OpenFreeMap base map unavailable; overlay drawn on a plain background" });
    }
  });

  await test.step("follow-up: 'What if the delay doubles?' re-simulates and says what changed", async () => {
    const before = await turns(page).count();
    const box = page.getByRole("textbox", { name: "Message" });
    await box.fill("What if the delay doubles?");
    await box.press("Enter");
    await expect(turns(page)).toHaveCount(before + 1, { timeout: 30_000 });
    const turn = turns(page).last();
    await expect(turn.locator("section[data-status]").first()).toHaveAttribute("data-status", "done", { timeout: RUN_TIMEOUT_MS });
    // No approach card (the investigation is reused), a new recommendation and the diff line.
    await expect(block(turn, "approach_card")).toHaveCount(0);
    await expect(block(turn, "recommended_option")).toBeVisible();
    const changed = turn.locator("[data-what-changed]");
    await expect(changed).toContainText(/What changed:\s*Plan v\d+ → v\d+/);
    test.info().annotations.push({ type: "what changed", description: (await changed.innerText()).replace(/\s+/g, " ") });
    expect(await turn.innerText()).not.toMatch(/\{\{|\}\}/);
    await changed.scrollIntoViewIfNeeded();
    await keyScreenshot(page, "21-followup-what-changed");
    await expectCalmTurns(page);
  });

  await test.step("Priya applies → approval card she cannot approve", async () => {
    const option = block(turns(page), "recommended_option").last();
    // Values the investigation could only infer must be confirmed first (90 §5).
    const confirm = option.getByRole("button", { name: "Confirm these values" });
    if (await confirm.isVisible()) {
      await expect(option.getByRole("button", { name: "Apply changes for me" })).toBeDisabled();
      const count = await block(turns(page), "recommended_option").count();
      await confirm.click();
      await expect(block(turns(page), "recommended_option")).toHaveCount(count + 1, { timeout: RUN_TIMEOUT_MS });
      test.info().annotations.push({ type: "note", description: "confirmed unconfirmed values before Apply" });
    }
    await block(turns(page), "recommended_option").last().getByRole("button", { name: "Apply changes for me" }).click();
    const approval = await waitForBlock(page, "approval_card");
    await expect(approval.getByRole("button", { name: "Approve" })).toBeDisabled();
    await expect(page.getByText(/Waiting for someone with the approver role to decide/)).toBeVisible();
    await expect(page.getByText("Awaiting approval").first()).toBeVisible();
    await expectCalmTurns(page);
  });

  await test.step("Marco opens the case: approval card asks to Connect Merxa", async () => {
    await switchPersona(page, "Marco Rossi");
    await expect(page).toHaveURL(`${KEELSON}/cases/${encodeURIComponent(caseId)}`);
    const approval = await waitForBlock(page, "approval_card", 60_000);
    const connect = approval.getByRole("button", { name: "Connect Merxa" });
    await expect(connect).toBeVisible({ timeout: 30_000 });
    await expect(approval.getByRole("button", { name: "Approve" })).toBeDisabled();
    await approval.scrollIntoViewIfNeeded();
    await keyScreenshot(page, "05-approval-connect");
    await connect.click();
  });

  await test.step("Keycloak (merxa realm) login as marco.rossi → back on the case", async () => {
    await keycloakLogin(page, tracer, "merxa", "marco.rossi");
    await page.waitForURL(new RegExp(`/cases/${caseId}`), { timeout: 30_000 });
    await expect(page.getByText("Merxa connected")).toBeVisible();
    // ?connected=merxa is removed from the address bar.
    await expect.poll(() => new URL(page.url()).search).toBe("");
  });

  let extUrl = "";
  await test.step("Marco approves → commit tracker reaches Verified", async () => {
    const approval = await waitForBlock(page, "approval_card", 60_000);
    const approve = approval.getByRole("button", { name: "Approve" });
    await expect(approve).toBeEnabled({ timeout: 30_000 });
    await approve.click();
    const tracker = await waitForBlock(page, "commit_tracker");
    await expect(tracker.locator('[data-status="verified"]')).toBeVisible({ timeout: RUN_TIMEOUT_MS });
    await expect(tracker.getByText("Changes applied and verified")).toBeVisible();
    await tracker.scrollIntoViewIfNeeded();
    await keyScreenshot(page, "06-commit-verified");
    await expectCalmTurns(page);
    extUrl = (await tracker.getByRole("link", { name: /Open in Merxa/ }).getAttribute("href")) ?? "";
    expect(extUrl).toMatch(new RegExp(`^${MERXA.replace(/\./g, "\\.")}/change-batches/CS-\\d+`));
  });

  await test.step("follow the link to Merxa: Change Batch applied and attributed to Marco", async () => {
    const tracker = block(turns(page), "commit_tracker").last();
    const [merxa] = await Promise.all([context.waitForEvent("page"), tracker.getByRole("link", { name: /Open in Merxa/ }).click()]);
    await merxa.waitForLoadState();
    await expect(merxa).toHaveURL(/\/login\?next=/);
    await merxa.getByRole("button", { name: /^Continue as Marco Rossi/ }).click();
    await merxa.waitForURL(/\/change-batches\/CS-\d+/, { timeout: 30_000 });
    const banner = merxa.getByTestId("attribution-banner");
    await expect(banner).toContainText(/Changed by Marco Rossi via Keelson \(APR-[\w-]+\)/);
    await expect(merxa.locator('.mx-badge[data-status="applied"]').first()).toBeVisible();
    const firstItem = merxa.getByRole("region", { name: /^Item 1:/ }).or(merxa.locator("section.mx-item").first());
    await expect(firstItem.getByRole("columnheader", { name: "Before" })).toBeVisible();
    await expect(firstItem.getByRole("columnheader", { name: "After" })).toBeVisible();
    await expect(firstItem.locator("tbody tr").first()).toBeVisible();
    await keyScreenshot(merxa, "07-merxa-change-batch");

    const code = new URL(merxa.url()).pathname.split("/").pop() ?? "";
    const cs = await merxaChangeSet(code);
    expect(cs.status).toBe("applied");
    expect(cs.items.every((i) => i.status === "applied")).toBe(true);
    await merxa.close();
  });
});
