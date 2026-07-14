import assert from "node:assert/strict";
import { test } from "node:test";
import { EventEmitter } from "node:events";
import { createMessageReader, writeMessage } from "../broker/framing.ts";

class FakeSocket extends EventEmitter {
	chunks: Buffer[] = [];
	write(chunk: Buffer): boolean {
		this.chunks.push(chunk);
		return true;
	}
}

test("framing reads fragmented length-prefixed JSON messages", () => {
	const socket = new FakeSocket();
	writeMessage(socket as any, { hello: "agency" });
	const frame = socket.chunks[0];
	const received: unknown[] = [];
	const errors: Error[] = [];
	const reader = createMessageReader((msg) => received.push(msg), (err) => errors.push(err));

	for (const byte of frame) reader(Buffer.from([byte]));

	assert.deepEqual(received, [{ hello: "agency" }]);
	assert.deepEqual(errors, []);
});

test("framing rejects oversized frames", () => {
	const received: unknown[] = [];
	const errors: Error[] = [];
	const reader = createMessageReader((msg) => received.push(msg), (err) => errors.push(err), 8);
	const header = Buffer.alloc(4);
	header.writeUInt32BE(9, 0);
	reader(header);
	assert.equal(received.length, 0);
	assert.match(errors[0]?.message || "", /exceeds maximum/);
});
