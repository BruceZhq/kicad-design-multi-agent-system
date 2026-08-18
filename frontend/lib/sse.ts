export interface SseEvent {
  data: string;
  event?: string;
  id?: string;
  retry?: number;
}

/** Incremental SSE parser. It deliberately accepts arbitrary chunk boundaries. */
export class SseParser {
  private buffer = "";
  private pendingCarriageReturn = false;
  private lastEventId: string | undefined;

  feed(chunk: string): SseEvent[] {
    let input = `${this.pendingCarriageReturn ? "\r" : ""}${chunk}`;
    this.pendingCarriageReturn = input.endsWith("\r");
    if (this.pendingCarriageReturn) input = input.slice(0, -1);

    this.buffer += input.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    return this.drain(false);
  }

  finish(): SseEvent[] {
    if (this.pendingCarriageReturn) {
      this.buffer += "\n";
      this.pendingCarriageReturn = false;
    }
    return this.drain(true);
  }

  private drain(flush: boolean): SseEvent[] {
    const events: SseEvent[] = [];
    let boundary = this.buffer.indexOf("\n\n");

    while (boundary >= 0) {
      const block = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const event = this.parseBlock(block);
      if (event) events.push(event);
      boundary = this.buffer.indexOf("\n\n");
    }

    if (flush && this.buffer.length > 0) {
      const event = this.parseBlock(this.buffer);
      this.buffer = "";
      if (event) events.push(event);
    }
    return events;
  }

  private parseBlock(block: string): SseEvent | null {
    const data: string[] = [];
    let event: string | undefined;
    let retry: number | undefined;
    let sawData = false;

    for (const line of block.split("\n")) {
      if (line === "" || line.startsWith(":")) continue;
      const separator = line.indexOf(":");
      const field = separator < 0 ? line : line.slice(0, separator);
      let value = separator < 0 ? "" : line.slice(separator + 1);
      if (value.startsWith(" ")) value = value.slice(1);

      if (field === "data") {
        sawData = true;
        data.push(value);
      } else if (field === "event") {
        event = value;
      } else if (field === "id" && !value.includes("\0")) {
        this.lastEventId = value;
      } else if (field === "retry" && /^\d+$/.test(value)) {
        retry = Number(value);
      }
    }

    if (!sawData && retry === undefined) return null;
    return { data: data.join("\n"), event, id: this.lastEventId, retry };
  }
}

export async function readSseStream(
  stream: ReadableStream<Uint8Array>,
  onEvent: (event: SseEvent) => void | Promise<void>,
): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const event of parser.feed(decoder.decode(value, { stream: true }))) {
        await onEvent(event);
      }
    }
    const tail = decoder.decode();
    for (const event of [...parser.feed(tail), ...parser.finish()]) {
      await onEvent(event);
    }
  } finally {
    reader.releaseLock();
  }
}
