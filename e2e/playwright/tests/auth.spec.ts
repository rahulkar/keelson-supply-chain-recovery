import { AUTH, KEELSON } from "./support/env";
import { expect, test } from "./support/fixtures";
import { keycloakLogin } from "./support/keelson";

test.describe("session gate", () => {
  test("an anonymous visitor is sent to the Keycloak sign-in and comes back to the page", async ({ page, tracer }) => {
    await page.goto(`${KEELSON}/cases`);
    await expect(page).toHaveURL(new RegExp(`^${AUTH.replace(/\./g, "\\.")}/realms/keelson/protocol/openid-connect/auth\\?`));
    const url = new URL(page.url());
    expect(url.searchParams.get("client_id")).toBe("keelson-bff");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    await expect(page.locator("#kc-login")).toBeVisible();

    await keycloakLogin(page, tracer, "keelson", "priya");
    await expect(page).toHaveURL(`${KEELSON}/cases`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  });

  test("API calls without a session answer 401, health stays public", async ({ request }) => {
    expect((await request.get(`${KEELSON}/api/me`)).status()).toBe(401);
    expect((await request.get(`${KEELSON}/healthz`)).status()).toBe(200);
    const page = await request.get(`${KEELSON}/cases/does-not-exist`, { maxRedirects: 0 });
    expect(page.status()).toBe(307);
    expect(page.headers()["location"]).toContain("/api/auth/login?next=%2Fcases%2Fdoes-not-exist");
  });
});
