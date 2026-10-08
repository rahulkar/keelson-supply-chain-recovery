import { findSecret } from "./secret-scan";

/**
 * Fails the run if any artifact (traces, report, attachments — zips included) contains the demo
 * password: traces must have holes wherever a password is typed (`Tracer.secret()`).
 */
export default async function globalTeardown(): Promise<void> {
  const secret = process.env.DEMO_USER_PASSWORD;
  if (!secret) return;
  const hits = findSecret(secret);
  if (hits.length > 0) {
    throw new Error(`the demo password was found in ${hits.length} artifact(s), e.g. ${hits.slice(0, 3).join(", ")}`);
  }
  console.log("artifact scan: no credentials in traces, screenshots or reports");
}
