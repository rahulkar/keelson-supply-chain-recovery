import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fakeApi, jsonResponse, makeMe, withSession } from "../../../test/session";
import { HOME_SUGGESTIONS, HomeComposer } from "./HomeComposer";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

beforeEach(() => push.mockReset());

describe("HomeComposer", () => {
  it("creates a case, posts the message with an Idempotency-Key, then opens the thread", async () => {
    const user = userEvent.setup();
    const { api, calls } = fakeApi([
      (url, init) => (url === "/api/cases" && init.method === "POST" ? jsonResponse(201, { id: "case-42" }) : undefined),
      (url) => (url === "/api/cases/case-42/messages" ? jsonResponse(202, { run_id: "run-1" }) : undefined),
    ]);
    const Wrapper = withSession(makeMe(), api);
    render(
      <Wrapper>
        <HomeComposer />
      </Wrapper>,
    );
    await user.type(screen.getByRole("textbox"), "Port strike in Felixstowe from Monday{Enter}");
    await waitFor(() => expect(push).toHaveBeenCalledWith("/cases/case-42"));
    expect(calls.map((c) => [c.method, c.url])).toEqual([
      ["POST", "/api/cases"],
      ["POST", "/api/cases/case-42/messages"],
    ]);
    expect(calls[1]!.body).toEqual({ text: "Port strike in Felixstowe from Monday" });
    expect(calls[1]!.headers["Idempotency-Key"]).toMatch(/^[0-9a-f-]{36}$/);
    expect(calls.every((c) => c.headers["X-CSRF-Token"] === "csrf-test")).toBe(true);
  });

  it("creates one case when submitted twice before re-rendering (regression)", async () => {
    const { api, calls } = fakeApi([
      (url, init) => (url === "/api/cases" && init.method === "POST" ? jsonResponse(201, { id: "case-7" }) : undefined),
      (url) => (url === "/api/cases/case-7/messages" ? jsonResponse(202, { run_id: "run-7" }) : undefined),
    ]);
    const Wrapper = withSession(makeMe(), api);
    render(
      <Wrapper>
        <HomeComposer />
      </Wrapper>,
    );
    const box = screen.getByRole("textbox");
    await act(async () => {
      fireEvent.change(box, { target: { value: "Felixstowe strike" } });
    });
    await act(async () => {
      fireEvent.submit(box.closest("form")!);
      fireEvent.submit(box.closest("form")!);
    });
    await waitFor(() => expect(push).toHaveBeenCalledWith("/cases/case-7"));
    expect(calls.filter((c) => c.url === "/api/cases").length).toBe(1);
    expect(calls.filter((c) => c.url.endsWith("/messages")).length).toBe(1);
  });

  it("starts a case straight from a suggestion chip", async () => {
    const user = userEvent.setup();
    const { api, calls } = fakeApi([
      (url, init) => (url === "/api/cases" && init.method === "POST" ? jsonResponse(201, { id: "c9" }) : undefined),
      (url) => (url === "/api/cases/c9/messages" ? jsonResponse(202, { run_id: "r9" }) : undefined),
    ]);
    const Wrapper = withSession(makeMe(), api);
    render(
      <Wrapper>
        <HomeComposer />
      </Wrapper>,
    );
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(expect.arrayContaining(HOME_SUGGESTIONS));
    await user.click(screen.getByRole("button", { name: "What if the Suez Canal closes for 3 weeks?" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/cases/c9"));
    expect(calls[1]!.body).toEqual({ text: "What if the Suez Canal closes for 3 weeks?" });
  });

  it("retries a failed send on the same case with the same Idempotency-Key", async () => {
    const user = userEvent.setup();
    let attempts = 0;
    const { api, calls } = fakeApi([
      (url, init) => (url === "/api/cases" && init.method === "POST" ? jsonResponse(201, { id: "c1" }) : undefined),
      (url) => {
        if (url !== "/api/cases/c1/messages") return undefined;
        attempts += 1;
        return attempts === 1 ? jsonResponse(503, { code: "unavailable", message: "try later" }) : jsonResponse(202, { run_id: "r1" });
      },
    ]);
    const Wrapper = withSession(makeMe(), api);
    render(
      <Wrapper>
        <HomeComposer />
      </Wrapper>,
    );
    await user.type(screen.getByRole("textbox"), "Trims are late{Enter}");
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("Trims are late"));
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/cases/c1"));
    const creates = calls.filter((c) => c.url === "/api/cases");
    const posts = calls.filter((c) => c.url === "/api/cases/c1/messages");
    expect(creates).toHaveLength(1);
    expect(posts).toHaveLength(2);
    expect(posts[0]!.headers["Idempotency-Key"]).toBe(posts[1]!.headers["Idempotency-Key"]);
  });

  it("is disabled for read-only roles", () => {
    const { api } = fakeApi([]);
    const Wrapper = withSession(makeMe({ roles: ["compliance"] }), api);
    render(
      <Wrapper>
        <HomeComposer disabled />
      </Wrapper>,
    );
    expect(screen.getByRole("textbox")).toBeDisabled();
  });
});
