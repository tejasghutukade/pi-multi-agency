import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { before, test } from "node:test";

const agencyRoot = mkdtempSync(join(tmpdir(), "agency-broker-test-"));
process.env.AGENCY_ROOT = agencyRoot;
process.env.AGENCY_PROJECT_ROOT = join(agencyRoot, "project");

let spawnBrokerIfNeeded: (...args: any[]) => Promise<void>;
let AgencyBrokerClient: any;
let makeAgencyMessage: any;

before(async () => {
	({ spawnBrokerIfNeeded } = await import("../broker/spawn.ts"));
	({ AgencyBrokerClient } = await import("../broker/client.ts"));
	({ makeAgencyMessage } = await import("../messages.ts"));
});

function registration(name: string, role: string, isHub = false) {
	return {
		name,
		role,
		isHub,
		cwd: process.env.AGENCY_PROJECT_ROOT || process.cwd(),
		model: "test",
		pid: process.pid,
		startedAt: Date.now(),
		lastActivity: Date.now(),
		status: "idle",
	};
}

async function connect(name: string, role: string, isHub = false): Promise<any> {
	const client = new AgencyBrokerClient();
	await client.connect(registration(name, role, isHub), `agency:${name}`);
	return client;
}

async function ensureBroker() {
	await spawnBrokerIfNeeded("npx", ["--yes", "tsx"]);
}

async function safeDisconnect(...clients: any[]) {
	await Promise.all(clients.filter(Boolean).map((client) => client.disconnect().catch(() => undefined)));
}

test("broker delivers delegate and report messages with explicit status", async () => {
	await ensureBroker();
	const hub = await connect("orchestrator", "orchestrator", true);
	const scout = await connect("scout-1", "scout");
	try {
		const inbound: unknown[] = [];
		scout.on("message", (_from: unknown, message: unknown) => inbound.push(message));

		const delegate = makeAgencyMessage({ kind: "delegate", from: "orchestrator", to: "scout-1", taskId: "t-1", payload: { goal: "map" } });
		const result = await hub.send("scout-1", delegate);
		assert.equal(result.delivered, true);
		await new Promise((resolve) => setTimeout(resolve, 20));
		assert.equal(inbound.length, 1);
		assert.deepEqual(inbound[0], delegate);
	} finally {
		await safeDisconnect(hub, scout);
	}
});

test("broker rejects missing target and peer-to-peer sends by default", async () => {
	await ensureBroker();
	const hub = await connect("orchestrator", "orchestrator", true);
	const scout = await connect("scout-1", "scout");
	const plan = await connect("plan-1", "plan");
	try {
		const missing = makeAgencyMessage({ kind: "delegate", from: "orchestrator", to: "missing", taskId: "t-missing" });
		assert.deepEqual(await hub.send("missing", missing), { id: missing.id, delivered: false, reason: "Session not found" });

		const peer = makeAgencyMessage({ kind: "progress", from: "scout-1", to: "plan-1", taskId: "t-peer", payload: { message: "hi" } });
		const peerResult = await scout.send("plan-1", peer);
		assert.equal(peerResult.delivered, false);
		assert.match(peerResult.reason || "", /ACL denied/);
	} finally {
		await safeDisconnect(hub, scout, plan);
	}
});

test("broker enforces ask/reply correlation", async () => {
	await ensureBroker();
	const hub = await connect("orchestrator", "orchestrator", true);
	const scout = await connect("scout-1", "scout");
	try {
		hub.on("message", async (_from: unknown, message: any) => {
			const reply = makeAgencyMessage({ kind: "reply", from: "orchestrator", to: "scout-1", taskId: message.taskId, replyTo: message.id, payload: { message: "approved" } });
			await hub.send("scout-1", reply);
		});
		const ask = makeAgencyMessage({ kind: "ask", from: "scout-1", to: "orchestrator", taskId: "t-ask", expectsReply: true, payload: { message: "continue?" } });
		const reply = await scout.ask("orchestrator", ask, 5000);
		assert.equal(reply.kind, "reply");
		assert.equal(reply.replyTo, ask.id);

		const badReply = makeAgencyMessage({ kind: "reply", from: "orchestrator", to: "scout-1", replyTo: "not-pending", payload: { message: "bad" } });
		const bad = await hub.send("scout-1", badReply);
		assert.equal(bad.delivered, false);
		assert.match(bad.reason || "", /pending ask/);
	} finally {
		await safeDisconnect(hub, scout);
	}
});

test.after(() => {
	rmSync(agencyRoot, { recursive: true, force: true });
});
