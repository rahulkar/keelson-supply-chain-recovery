import { toast } from "@keelson/ui";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AdminShell } from "@/components/admin/AdminShell";
import { KnowledgePage, fileSize, uploadError } from "@/components/admin/KnowledgePage";
import { ApiError } from "@/lib/api/client";
import { kbFileProblem, kbListSchema } from "@/lib/admin/types";
import { expectNoA11yViolations } from "./a11y";
import { adminRoutes, makeAdminServer, type AdminServer } from "./adminMock";
import { fakeApi, makeMe, withSession } from "./session";

const nav = vi.hoisted(() => ({ pathname: "/admin/knowledge", push: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.pathname, useRouter: () => ({ push: nav.push, refresh: vi.fn() }) }));

vi.setConfig({ testTimeout: 20_000 });

const SAM = () => makeMe({ username: "sam", name: "Sam Okafor", roles: ["tenant-admin", "planner"] });
const AVA = () => makeMe({ username: "ava", name: "Ava Lindqvist", roles: ["platform-admin"] });

let server: AdminServer;
beforeEach(() => {
  server = makeAdminServer();
});

function renderPage(me = SAM(), ui: ReactNode = <KnowledgePage />) {
  const { api, calls } = fakeApi(adminRoutes(server));
  const Wrapper = withSession(me, api);
  const utils = render(
    <Wrapper>
      <main>
        <AdminShell>{ui}</AdminShell>
      </main>
    </Wrapper>,
  );
  return { ...utils, calls };
}

const md = (name: string, text = "# Policy\n\nSea-air premium caps.") => new File([text], name, { type: "text/markdown" });

describe("Admin → Knowledge", () => {
  it("is in the admin nav and lists knowledge bases, documents with status, and the active profile", async () => {
    const { container } = renderPage();
    expect(screen.getByRole("link", { name: "Knowledge" })).toHaveAttribute("aria-current", "page");
    const kbTable = await screen.findByRole("table", { name: "Knowledge bases" });
    expect(within(kbTable).getByText("Playbooks & policies")).toBeInTheDocument();
    expect(within(kbTable).getByText("1 of 2 ready")).toBeInTheDocument();

    const docs = await screen.findByRole("table", { name: "Documents in Playbooks & policies" });
    const ready = within(docs).getByRole("row", { name: /Freight & expedite policy/ });
    expect(within(ready).getByText("Ready")).toBeInTheDocument();
    expect(within(ready).getByText("6 KB")).toBeInTheDocument();
    const failed = within(docs).getByRole("row", { name: /DC-to-DC transfer SOP/ });
    expect(within(failed).getByText("Failed")).toBeInTheDocument();
    expect(within(failed).getByText("embedding unavailable")).toBeInTheDocument();

    const profile = screen.getByRole("region", { name: "Embedding profile" });
    expect(within(profile).getByText("embed-default")).toBeInTheDocument();
    expect(within(profile).getByText("vector(768)")).toBeInTheDocument();
    // Tenant admins don't see the provider model and can't build a new profile.
    expect(within(profile).queryByText("vertex_ai/text-embedding-005")).toBeNull();
    expect(within(profile).queryByRole("button", { name: "New profile" })).toBeNull();
    expect(within(profile).getByText("Changing the model requires a platform admin")).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("uploads with the file chooser (multipart, optional title) and refreshes the list", async () => {
    const user = userEvent.setup();
    const success = vi.spyOn(toast, "success");
    const { calls } = renderPage();
    await screen.findByRole("table", { name: "Documents in Playbooks & policies" });
    await user.type(screen.getByLabelText(/^Title/), "Peak trading playbook");
    await user.upload(screen.getByTestId("kb-file-input"), md("peak.md"));
    const docs = await screen.findByRole("table", { name: "Documents in Playbooks & policies" });
    await within(docs).findByText("Peak trading playbook");
    const upload = calls.find((c) => c.method === "POST" && c.url === "/api/admin/kb/kb-1/documents")!;
    expect(upload.body).toBeInstanceOf(FormData);
    const form = upload.body as FormData;
    expect((form.get("file") as File).name).toBe("peak.md");
    expect(form.get("title")).toBe("Peak trading playbook");
    // Multipart: the browser sets the boundary, so no JSON content type; CSRF still sent.
    expect(upload.headers["Content-Type"]).toBeUndefined();
    expect(upload.headers["X-CSRF-Token"]).toBe("csrf-test");
    expect(success).toHaveBeenCalledWith("Added Peak trading playbook", { description: "3 passages indexed." });
    expect(screen.getByLabelText(/^Title/)).toHaveValue("");
  });

  it("accepts a dropped file, highlights the drop zone, and refuses wrong types / sizes before uploading", async () => {
    const { calls, container } = renderPage();
    await screen.findByRole("table", { name: "Documents in Playbooks & policies" });
    const zone = container.querySelector("[data-dropzone]")!;
    fireEvent.dragOver(zone, { dataTransfer: { files: [] } });
    expect(zone).toHaveAttribute("data-dragging");
    // Regression: passing over the zone's own children is not leaving it.
    // (jsdom has no DragEvent; a MouseEvent of type dragleave carries relatedTarget.)
    const leave = (to: Element) => fireEvent(zone, new MouseEvent("dragleave", { bubbles: true, relatedTarget: to }));
    leave(within(zone as HTMLElement).getByRole("button", { name: "Choose file" }));
    expect(zone).toHaveAttribute("data-dragging");
    leave(document.body);
    expect(zone).not.toHaveAttribute("data-dragging");
    fireEvent.dragOver(zone, { dataTransfer: { files: [] } });
    expect(zone).toHaveAttribute("data-dragging");
    fireEvent.drop(zone, { dataTransfer: { files: [new File(["x"], "rates.xlsx")] } });
    expect(zone).not.toHaveAttribute("data-dragging");
    expect(await screen.findByRole("alert")).toHaveTextContent("rates.xlsx: only Markdown (.md), text (.txt) and PDF files can be added.");
    fireEvent.drop(zone, { dataTransfer: { files: [md("a.md"), md("b.md")] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("Add one file at a time.");
    expect(calls.some((c) => c.method === "POST")).toBe(false);

    await act(async () => {
      fireEvent.drop(zone, { dataTransfer: { files: [md("sop.md")] } });
    });
    const docs = await screen.findByRole("table", { name: "Documents in Playbooks & policies" });
    await within(docs).findByText("sop");
    expect(calls.filter((c) => c.method === "POST" && c.url.endsWith("/documents"))).toHaveLength(1);
  });

  it("says when the same file is already there and shows server refusals", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("table", { name: "Documents in Playbooks & policies" });
    await user.upload(screen.getByTestId("kb-file-input"), md("peak.md"));
    await screen.findByText("peak");
    await user.upload(screen.getByTestId("kb-file-input"), md("peak.md"));
    await waitFor(() => expect(screen.getAllByText("peak")).toHaveLength(1));

    server.failures["POST /api/admin/kb/kb-1/documents"] = [503, { code: "embedding_unavailable", message: "the embedding service is unavailable" }];
    await user.upload(screen.getByTestId("kb-file-input"), md("other.md"));
    expect(await screen.findByRole("alert")).toHaveTextContent("The document was stored but couldn't be indexed yet. Use Re-embed documents to retry.");
  });

  it("deletes a document after confirmation", async () => {
    const user = userEvent.setup();
    const { calls } = renderPage();
    const docs = await screen.findByRole("table", { name: "Documents in Playbooks & policies" });
    await user.click(within(docs).getByRole("button", { name: "Delete DC-to-DC transfer SOP" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Delete DC-to-DC transfer SOP?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete document" }));
    await waitFor(() => expect(within(docs).queryByText("DC-to-DC transfer SOP")).toBeNull());
    expect(calls.some((c) => c.method === "DELETE" && c.url === "/api/admin/kb/kb-1/documents/doc-2")).toBe(true);
  });

  it("creates a knowledge base (name taken → says so) and shows its documents", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("table", { name: "Knowledge bases" });
    await user.click(screen.getByRole("button", { name: "New knowledge base" }));
    const dialog = await screen.findByRole("dialog", { name: "New knowledge base" });
    await user.type(within(dialog).getByRole("textbox", { name: /Name/ }), "playbooks & POLICIES");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("A knowledge base with this name already exists.");
    await user.clear(within(dialog).getByRole("textbox", { name: /Name/ }));
    await user.type(within(dialog).getByRole("textbox", { name: /Name/ }), "Supplier contracts");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "New knowledge base" })).toBeNull());
    expect(await screen.findByRole("table", { name: "Documents in Supplier contracts" })).toBeInTheDocument();
    expect(screen.getByText("No documents yet. Add a playbook, contract or policy above.")).toBeInTheDocument();
    // Switch back to the first knowledge base.
    await user.click(screen.getByRole("button", { name: "Show documents in Playbooks & policies" }));
    expect(await screen.findByRole("table", { name: "Documents in Playbooks & policies" })).toBeInTheDocument();
  });

  it("re-embeds after confirmation, shows progress, and is off while a job runs", async () => {
    const user = userEvent.setup();
    const { calls } = renderPage();
    const profile = await screen.findByRole("region", { name: "Embedding profile" });
    await user.click(within(profile).getByRole("button", { name: "Re-embed documents" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Re-embed documents?" });
    await user.click(within(dialog).getByRole("button", { name: "Re-embed" }));
    const meter = await within(profile).findByRole("meter", { name: "Passages re-embedded" });
    expect(meter).toHaveAttribute("aria-valuetext", "3 of 14 passages");
    expect(calls.find((c) => c.method === "POST" && c.url === "/api/admin/kb/reembed")!.body).toEqual({ scope: "tenant" });
    // While a job runs the button is off (the engine would answer 409 reembed_running).
    expect(within(profile).getByRole("button", { name: "Re-embed documents" })).toHaveAttribute("aria-disabled", "true");
  });

  it("platform admin: provider model and a new profile (alias + dims) at platform scope", async () => {
    const user = userEvent.setup();
    const { calls } = renderPage(AVA());
    const profile = await screen.findByRole("region", { name: "Embedding profile" });
    expect(within(profile).getByText("vertex_ai/text-embedding-005")).toBeInTheDocument();
    await user.click(within(profile).getByRole("button", { name: "New profile" }));
    const dialog = await screen.findByRole("dialog", { name: "New embedding profile" });
    const dims = within(dialog).getByRole("textbox", { name: /Dimensions/ });
    await user.clear(dims);
    await user.type(dims, "5000");
    await user.click(within(dialog).getByRole("button", { name: "Build profile" }));
    expect(within(dialog).getByText("A whole number from 1 to 4,000.")).toBeInTheDocument();
    await user.clear(dims);
    await user.type(dims, "3072");
    await user.click(within(dialog).getByRole("button", { name: "Build profile" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/admin/kb/reembed")).toBe(true));
    expect(calls.find((c) => c.url === "/api/admin/kb/reembed" && c.method === "POST")!.body).toEqual({ scope: "platform", alias: "embed-default", dims: 3072 });
  });

  it("no profile yet: says search is unavailable and Re-embed is off", async () => {
    server.kbProfile = null;
    renderPage();
    const profile = await screen.findByRole("region", { name: "Embedding profile" });
    expect(within(profile).getByText(/No embedding profile is active yet/)).toBeInTheDocument();
    expect(within(profile).getByRole("button", { name: "Re-embed documents" })).toHaveAttribute("aria-disabled", "true");
  });

  it("helpers", () => {
    expect(fileSize(820)).toBe("820 B");
    expect(fileSize(12_700)).toBe("12.4 KB");
    expect(fileSize(3_250_000)).toBe("3.1 MB");
    expect(kbFileProblem({ name: "Policy.PDF", size: 10 })).toBeNull();
    expect(kbFileProblem({ name: "big.md", size: 5 * 1024 * 1024 + 1 })).toBe("big.md: files can be at most 5 MB.");
    expect(kbFileProblem({ name: "empty.txt", size: 0 })).toBe("empty.txt: the file is empty.");
    expect(uploadError(new ApiError(413, "file_too_large", "x"))).toBe("The file is larger than 5 MB.");
    expect(uploadError(new ApiError(415, "unsupported_media_type", "x"))).toMatch(/Only Markdown/);
    // Regression: the engine's real refusal codes (kb_text.py / kb.py) get a plain sentence.
    expect(uploadError(new ApiError(422, "invalid_text", "x"))).toMatch(/isn't readable text/);
    expect(uploadError(new ApiError(422, "invalid_pdf", "x"))).toMatch(/PDF couldn't be read/);
    expect(uploadError(new ApiError(422, "encrypted_pdf", "x"))).toMatch(/Password-protected/);
    expect(uploadError(new ApiError(422, "no_text", "x"))).toMatch(/no text/);
    expect(uploadError(new ApiError(422, "too_many_pages", "x"))).toMatch(/too long/);
    expect(uploadError(new ApiError(422, "too_many_chunks", "x"))).toMatch(/too long/);
    expect(uploadError(new ApiError(422, "empty_file", "x"))).toBe("The file is empty.");
    expect(uploadError(new ApiError(503, "no_embedding_profile", "x"))).toMatch(/stored but couldn't be indexed/);
    // A bare list (older engine) is tolerated; the profile is then unknown.
    expect(kbListSchema.parse([{ id: "k", name: "n" }])).toMatchObject({ items: [{ id: "k", documents: 0 }], profile: null });
  });
});
