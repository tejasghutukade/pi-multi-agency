import assert from "node:assert/strict";
import { test } from "node:test";
import { isAgencyMessage, makeAgencyMessage, toBrokerEnvelope } from "../messages.ts";

test("agency message schema validates supported message kinds", () => {
	const message = makeAgencyMessage({
		kind: "delegate",
		from: "orchestrator",
		to: "scout-1",
		taskId: "t-1",
		payload: { goal: "map repo" },
	});
	assert.equal(isAgencyMessage(message), true);
	assert.equal(message.kind, "delegate");
	assert.equal(message.from, "orchestrator");
	assert.equal(typeof message.id, "string");
	assert.equal(typeof message.createdAt, "number");
});

test("agency message schema rejects unsupported kind and malformed task ids", () => {
	assert.equal(isAgencyMessage({ id: "1", kind: "chat", from: "a", to: "b", createdAt: Date.now() }), false);
	assert.equal(isAgencyMessage({ id: "1", kind: "report", from: "a", to: "b", taskId: 42, createdAt: Date.now() }), false);
});

test("broker envelope mirrors ask/reply correlation fields", () => {
	const message = makeAgencyMessage({
		kind: "reply",
		from: "orchestrator",
		to: "scout-1",
		replyTo: "ask-1",
		payload: { message: "continue" },
	});
	const envelope = toBrokerEnvelope(message);
	assert.equal(envelope.id, message.id);
	assert.equal(envelope.replyTo, "ask-1");
	assert.deepEqual(envelope.agency, message);
});
