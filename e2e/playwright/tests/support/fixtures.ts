import fs from "node:fs";
import path from "node:path";
import { test as base, expect, type BrowserContext, type Locator, type Page, type TestInfo } from "@playwright/test";
import { SCREENS, TRACES } from "./env";

function slug(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 80);
}

/**
 * Trace recorder with "holes": everything is recorded (screenshots, DOM snapshots, sources,
 * network) into `artifacts/traces/<test>-r<retry>-<n>.zip`, except what runs inside `secret()`
 * (password entry on the Keycloak forms). A paused *chunk* is not enough — the network log keeps
 * recording between chunks and the next chunk would carry the login POST body — so `secret()`
 * stops the whole tracing session and starts a new one afterwards. Traces still hold session
 * cookies in request headers (as every Playwright trace does); they never hold a password.
 */
export class Tracer {
  private part = 0;
  private recording = false;
  readonly files: string[] = [];

  constructor(
    private readonly context: BrowserContext,
    private readonly info: TestInfo,
  ) {}

  private nextPath(): string {
    this.part += 1;
    const name = `${slug(this.info.titlePath.slice(1).join(" "))}-r${this.info.retry}-${this.part}.zip`;
    const file = path.join(TRACES, name);
    this.files.push(file);
    return file;
  }

  private async begin(title: string): Promise<void> {
    await this.context.tracing.start({ screenshots: true, snapshots: true, sources: true, title });
    this.recording = true;
  }

  private async end(): Promise<void> {
    if (!this.recording) return;
    this.recording = false;
    await this.context.tracing.stop({ path: this.nextPath() });
  }

  async start(): Promise<void> {
    fs.mkdirSync(TRACES, { recursive: true });
    await this.begin(this.info.title);
  }

  /** Run `fn` with no tracing session at all (the part before it is saved). */
  async secret<T>(fn: () => Promise<T>): Promise<T> {
    await this.end();
    try {
      return await fn();
    } finally {
      await this.begin(`${this.info.title} (cont.)`);
    }
  }

  async finish(): Promise<void> {
    await this.end();
    for (const file of this.files) {
      if (fs.existsSync(file)) await this.info.attach(path.basename(file), { path: file, contentType: "application/zip" });
    }
  }
}

/**
 * Write `artifacts/screens/<name>.png`. Overwriting a file on the Windows bind mount sometimes fails
 * with EINVAL (the host still holds the previous file, e.g. an indexer or a viewer): write a fresh
 * temporary file, then replace the target, retrying a few times.
 */
export async function saveScreen(name: string, png: Buffer): Promise<string> {
  fs.mkdirSync(SCREENS, { recursive: true });
  const file = path.join(SCREENS, `${name}.png`);
  let last: unknown;
  for (let attempt = 0; attempt < 5; attempt++) {
    const tmp = path.join(SCREENS, `.${name}.${process.pid}.${attempt}.tmp`);
    try {
      fs.writeFileSync(tmp, png);
      try {
        fs.renameSync(tmp, file);
      } catch {
        fs.rmSync(file, { force: true });
        fs.renameSync(tmp, file);
      }
      return file;
    } catch (err) {
      last = err;
      fs.rmSync(tmp, { force: true });
      await new Promise((r) => setTimeout(r, 300 * (attempt + 1)));
    }
  }
  throw last;
}

/** Key screenshots for the report (`artifacts/screens/<name>.png`, overwritten each run). */
export async function keyScreenshot(page: Page, name: string, options: { fullPage?: boolean } = {}): Promise<string> {
  return saveScreen(name, await page.screenshot({ fullPage: options.fullPage ?? false, animations: "disabled" }));
}

/** Screenshot of one element (a canvas visual) as `artifacts/screens/<name>.png`. */
export async function elementScreenshot(target: Locator, name: string): Promise<string> {
  await target.scrollIntoViewIfNeeded();
  return saveScreen(name, await target.screenshot({ animations: "disabled" }));
}

export const test = base.extend<{ tracer: Tracer }>({
  tracer: [
    async ({ context }, use, testInfo) => {
      const tracer = new Tracer(context, testInfo);
      await tracer.start();
      await use(tracer);
      await tracer.finish();
    },
    { auto: true },
  ],
});

export { expect };
