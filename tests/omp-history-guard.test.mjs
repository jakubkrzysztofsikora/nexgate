import { test, expect } from "bun:test";
import { sanitizeHistory } from "../scripts/omp-history-guard.mjs";

test("malformed tool names cannot poison a model switch or expose orphan results", () => {
  const messages = [
    { role: "user", content: "Inspect the project" },
    { role: "assistant", content: [
      { type: "text", text: "Checking" },
      { type: "toolCall", id: "bad", name: "bash " + "x".repeat(54471), arguments: {} },
      { type: "toolCall", id: "good", name: "read", arguments: { path: "README.md" } },
    ], providerPayload: { stale: true } },
    { role: "toolResult", toolCallId: "bad", toolName: "bad name", content: [{ type: "text", text: "Unknown tool" }] },
    { role: "toolResult", toolCallId: "good", toolName: "read", content: [{ type: "text", text: "Read output" }] },
  ];
  const before = structuredClone(messages);
  const cleaned = sanitizeHistory(messages);
  expect(cleaned.filter(m => m.role === "toolResult").map(m => m.toolCallId)).toEqual(["good"]);
  expect(cleaned[1].content.filter(b => b.type === "toolCall").map(b => b.name)).toEqual(["read"]);
  expect(cleaned[1].providerPayload).toBeUndefined();
  expect(JSON.stringify(cleaned)).not.toContain("x".repeat(201));
  expect(messages).toEqual(before);
});

test("valid history and tool names from previously enabled tools remain intact", () => {
  const messages = [{ role: "assistant", content: [
    { type: "toolCall", id: "one", name: "mcp__server__tool", arguments: { query: "hello" } },
    { type: "toolCall", id: "two", name: "a".repeat(64), arguments: {} },
  ] }];
  expect(sanitizeHistory(messages)).toBe(messages);
});

test("invalid identifier characters are removed without guessing executable arguments", () => {
  const messages = [{ role: "assistant", content: [
    { type: "toolCall", id: "bad", name: "bash echo private-data", arguments: {} },
  ] }, { role: "toolResult", toolCallId: "bad", content: [] }];
  const cleaned = sanitizeHistory(messages);
  expect(cleaned).toHaveLength(1);
  expect(cleaned[0].content[0].type).toBe("text");
  expect(JSON.stringify(cleaned)).not.toContain("private-data");
});
