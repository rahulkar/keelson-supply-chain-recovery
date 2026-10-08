import { describe, expect, it } from "vitest";
import { GENERIC_RUN_ERROR, GENERIC_STEP_ERROR, publicErrorId, publicErrorMessage } from "./errors";

const ID = "6f1e7c8a-9d55-4b1c-8d3e-4f500b1c2d3e";
const ENGINE_MESSAGE = `Something went wrong while working on this case. Please try again; if it keeps happening, contact support with error ID ${ID}.`;

describe("publicErrorMessage", () => {
  it("keeps the engine's public messages", () => {
    for (const m of [
      ENGINE_MESSAGE,
      "Run cancelled.",
      "This step could not be completed. Please try again; if it keeps happening, contact support with error ID x.",
      "The run used up its budget.",
    ]) {
      expect(publicErrorMessage(m)).toBe(m);
    }
  });

  it.each([
    ["a Python traceback", 'Traceback (most recent call last):\n  File "/app/run.py", line 4, in main\nKeyError: "x"'],
    ["a one-line traceback frame", 'File "/app/run.py", line 4, in main'],
    ["an exception class prefix", 'ProgrammingError: relation "run" does not exist'],
    ["a JS stack frame", "TypeError at fold (reducer.ts:12:3)"],
    ["a source path", "failed in keelson_engine/runtime/translator.py:310"],
    ["an internal URL", "GET http://engine-api:8000/internal failed"],
    ["a DSN", "could not connect to postgresql://keelson@pg-core/keelson"],
    ["SQL", "SELECT * FROM run WHERE tenant = 'b'"],
    ["a driver name", "asyncpg pool exhausted"],
    ["too long", "x".repeat(401)],
    ["empty", "   "],
  ])("replaces %s with a generic sentence", (_name, message) => {
    expect(publicErrorMessage(message)).toBe(GENERIC_RUN_ERROR);
    expect(publicErrorMessage(message, GENERIC_STEP_ERROR)).toBe(GENERIC_STEP_ERROR);
  });

  it("handles non-strings", () => {
    expect(publicErrorMessage(undefined)).toBe(GENERIC_RUN_ERROR);
    expect(publicErrorMessage({ message: "x" })).toBe(GENERIC_RUN_ERROR);
  });
});

describe("publicErrorId", () => {
  it("prefers metadata.errorId, else the UUID after 'error ID' in the message", () => {
    expect(publicErrorId("abc-123-def", ENGINE_MESSAGE)).toBe("abc-123-def");
    expect(publicErrorId(null, ENGINE_MESSAGE)).toBe(ID);
    expect(publicErrorId(undefined, "error ID not-a-uuid")).toBeNull();
    expect(publicErrorId(null, 42)).toBeNull();
  });
});
