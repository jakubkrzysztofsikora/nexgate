export function sanitizeHistory(messages) {
  const dropped = new Set();
  const updated = messages.map(message => {
    if (message.role !== "assistant" || !Array.isArray(message.content)) return message;
    let changed = false;
    const content = message.content.map(block => {
      if (block.type !== "toolCall" ||
          (typeof block.name === "string" && /^[a-zA-Z0-9_-]{1,64}$/.test(block.name))) return block;
      changed = true;
      dropped.add(block.id);
      return { type: "text", text: "[NexGate] A malformed tool call was omitted from history. No command was executed by this repair." };
    });
    if (!changed) return message;
    // Cached provider payloads can otherwise reintroduce the original bad call.
    const { providerPayload, ...rest } = message;
    return { ...rest, content };
  });
  if (!dropped.size) return messages;
  return updated.filter(message => message.role !== "toolResult" || !dropped.has(message.toolCallId));
}

export default function historyGuard(pi) {
  pi.on("context", event => ({ messages: sanitizeHistory(event.messages) }));
}
