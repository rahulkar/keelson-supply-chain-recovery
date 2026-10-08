/**
 * Regression for a leak found while building this suite: pausing a trace *chunk* around the
 * Keycloak password still recorded the login POST body in the trace's network log. `Tracer.secret()`
 * now stops the tracing session; this test proves it with a routed form post (no stack needed),
 * and that the artifact scanner used by globalTeardown finds what it should.
 */
import { randomUUID } from "node:crypto";
import { TRACES } from "./support/env";
import { Tracer, expect, test } from "./support/fixtures";
import { findSecret } from "./support/secret-scan";

const PROBE = "http://keelson.localhost/__trace_probe";
const FORM = `<form method="post" action="${PROBE}"><label>Password <input name="p" type="password"></label><button>Sign in</button></form>`;

test("traces never contain what is typed inside Tracer.secret()", async ({ browser }, testInfo) => {
  const visible = `visible-${randomUUID()}`;
  const hidden = `hidden-${randomUUID()}`;
  const context = await browser.newContext();
  await context.route(PROBE, (route) => route.fulfill({ status: 200, contentType: "text/html", body: "<p>ok</p>" }));
  const tracer = new Tracer(context, testInfo);
  await tracer.start();
  const page = await context.newPage();

  await page.setContent(FORM);
  await page.getByLabel("Password").fill(visible);
  await Promise.all([page.waitForURL(PROBE), page.getByRole("button", { name: "Sign in" }).click()]);

  await page.setContent(FORM);
  await tracer.secret(async () => {
    await page.getByLabel("Password").fill(hidden);
    await Promise.all([page.waitForURL(PROBE), page.getByRole("button", { name: "Sign in" }).click()]);
  });
  await page.setContent("<p>after</p>");
  await tracer.finish();
  await context.close();

  expect(tracer.files.length).toBe(2);
  expect(findSecret(visible, TRACES).length, "the scanner finds a value typed while tracing").toBeGreaterThan(0);
  expect(findSecret(hidden, TRACES), "nothing typed inside secret() reaches a trace").toEqual([]);
});
