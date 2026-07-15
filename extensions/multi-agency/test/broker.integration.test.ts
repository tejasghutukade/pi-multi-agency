import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { AgencyBrokerRuntime, buildAgencyTransportId } from "../broker-runtime.ts";
import { AgencyBrokerClient } from "../broker/client.ts";
import { requireBrokerContext, resolveBrokerContext, type AvailableBrokerContext } from "../broker/paths.ts";
import { checkSocketConnectable, getTsxCliPath, spawnBrokerIfNeeded } from "../broker/spawn.ts";
import { makeAgencyMessage } from "../messages.ts";

const sandbox = mkdtempSync(join(existsSync("/tmp") ? "/tmp" : tmpdir(), "ab-"));

function projectContext(name: string): AvailableBrokerContext {
	const project = join(sandbox, name);
	mkdirSync(join(project, ".pi", "agency"), { recursive: true });
	return requireBrokerContext(resolveBrokerContext({ projectRoot: project, env: {} }));
}

const projectA = projectContext(join("deep", "项目".repeat(40), "project-a"));
const projectB = projectContext(join("deep", "é".repeat(80), "project-b"));

function registration(context: AvailableBrokerContext, name: string, role: string, isHub = false) {
	return {
		name,
		role,
		isHub,
		cwd: context.projectRoot,
		model: "test",
		pid: process.pid,
		startedAt: Date.now(),
		lastActivity: Date.now(),
		status: "idle",
	};
}

async function connect(context: AvailableBrokerContext, name: string, role: string, isHub = false): Promise<AgencyBrokerClient> {
	const client = new AgencyBrokerClient(context);
	await client.connect(registration(context, name, role, isHub), buildAgencyTransportId(context, name));
	return client;
}

async function safeDisconnect(...clients: Array<AgencyBrokerClient | undefined>) {
	await Promise.all(clients.filter(Boolean).map((client) => client!.disconnect().catch(() => undefined)));
}

function receiveMessages(client: AgencyBrokerClient, count: number, timeoutMs = 2000): { messages: unknown[]; done: Promise<void> } {
	const messages: unknown[] = [];
	let timer: ReturnType<typeof setTimeout>;
	let handler: (_from: unknown, message: unknown) => void;
	const done = new Promise<void>((resolve, reject) => {
		handler = (_from, message) => {
			messages.push(message);
			if (messages.length >= count) { clearTimeout(timer); client.off("message", handler); resolve(); }
		};
		client.on("message", handler);
		timer = setTimeout(() => { client.off("message", handler); reject(new Error(`Timed out waiting for ${count} broker message(s)`)); }, timeoutMs);
	});
	return { messages, done };
}

async function stopTestBroker(context: AvailableBrokerContext): Promise<void> {
	let pid: number;
	try { pid = Number.parseInt(readFileSync(context.pidFile, "utf8").trim(), 10); } catch { return; }
	if (!Number.isSafeInteger(pid) || pid <= 0) throw new Error(`Invalid test broker pid at ${context.pidFile}`);
	try { process.kill(pid, "SIGTERM"); } catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ESRCH") return;
		throw error;
	}
	const deadline = Date.now() + 5000;
	while (Date.now() < deadline) {
		try { process.kill(pid, 0); } catch (error) {
			if ((error as NodeJS.ErrnoException).code === "ESRCH") return;
			throw error;
		}
		await new Promise((resolve) => setTimeout(resolve, 25));
	}
	throw new Error(`Test broker ${pid} did not terminate`);
}

before(async () => {
	const results = await Promise.allSettled([
		spawnBrokerIfNeeded(projectA, "npx", ["--yes", "tsx"]),
		spawnBrokerIfNeeded(projectB, "npx", ["--yes", "tsx"]),
	]);
	const failures = results.filter((result) => result.status === "rejected") as PromiseRejectedResult[];
	if (failures.length > 0) {
		await Promise.allSettled(results.map((result, index) => result.status === "fulfilled" ? stopTestBroker(index === 0 ? projectA : projectB) : Promise.resolve()));
		throw new AggregateError(failures.map((failure) => failure.reason), "Test broker startup failed");
	}
});

test("bounded Unix endpoints listen from deep multibyte roots while state remains project-local", async () => {
	assert.ok(projectA.endpoint.length < 100);
	assert.ok(projectB.endpoint.length < 100);
	assert.notEqual(projectA.endpoint, projectB.endpoint);
	assert.ok(projectA.pidFile.startsWith(projectA.agencyRoot));
	assert.ok(projectB.spawnLockFile.startsWith(projectB.agencyRoot));
	assert.equal(await checkSocketConnectable(projectA), true);
	assert.equal(await checkSocketConnectable(projectB), true);
});

test("identically named cohorts list and deliver only inside their owning project", async () => {
	const hubA = await connect(projectA, "orchestrator", "orchestrator", true);
	const scoutA = await connect(projectA, "scout", "scout");
	const hubB = await connect(projectB, "orchestrator", "orchestrator", true);
	const scoutB = await connect(projectB, "scout", "scout");
	try {
		assert.notEqual(hubA.sessionId, hubB.sessionId);
		assert.deepEqual((await hubA.listSessions()).map((session) => session.name).sort(), ["orchestrator", "scout"]);
		assert.deepEqual((await hubB.listSessions()).map((session) => session.name).sort(), ["orchestrator", "scout"]);

		const inboundA = receiveMessages(scoutA, 1);
		const inboundB = receiveMessages(scoutB, 1);
		const delegateA = makeAgencyMessage({ kind: "delegate", from: "orchestrator", to: "scout", taskId: "a", payload: { goal: "A" } });
		const delegateB = makeAgencyMessage({ kind: "delegate", from: "orchestrator", to: "scout", taskId: "b", payload: { goal: "B" } });
		assert.equal((await hubA.send("scout", delegateA)).delivered, true);
		assert.equal((await hubB.send("scout", delegateB)).delivered, true);
		await Promise.all([inboundA.done, inboundB.done]);
		assert.deepEqual(inboundA.messages, [delegateA]);
		assert.deepEqual(inboundB.messages, [delegateB]);
		assert.equal((inboundA.messages[0] as { from: string }).from, "orchestrator");
	} finally { await safeDisconnect(hubA, scoutA, hubB, scoutB); }
});

test("progress, report, and release envelopes retain logical identities without crossing projects", async () => {
	const hubA = await connect(projectA, "orchestrator", "orchestrator", true);
	const scoutA = await connect(projectA, "scout", "scout");
	const hubB = await connect(projectB, "orchestrator", "orchestrator", true);
	const scoutB = await connect(projectB, "scout", "scout");
	try {
		const receivedByHubA = receiveMessages(hubA, 2);
		const receivedByScoutA = receiveMessages(scoutA, 1);
		const receivedByHubB: unknown[] = [];
		const receivedByScoutB: unknown[] = [];
		hubB.on("message", (_from, message) => receivedByHubB.push(message));
		scoutB.on("message", (_from, message) => receivedByScoutB.push(message));
		const progress = makeAgencyMessage({ kind: "progress", from: "scout", to: "orchestrator", taskId: "a", payload: { message: "working" } });
		const report = makeAgencyMessage({ kind: "report", from: "scout", to: "orchestrator", taskId: "a", payload: { summary: "done" } });
		const release = makeAgencyMessage({ kind: "release", from: "orchestrator", to: "scout", taskId: "a" });
		assert.equal((await scoutA.send("orchestrator", progress)).delivered, true);
		assert.equal((await scoutA.send("orchestrator", report)).delivered, true);
		assert.equal((await hubA.send("scout", release)).delivered, true);
		await Promise.all([receivedByHubA.done, receivedByScoutA.done]);
		assert.deepEqual(receivedByHubA.messages, [progress, report]);
		assert.deepEqual(receivedByScoutA.messages, [release]);
		assert.deepEqual(receivedByHubB, []);
		assert.deepEqual(receivedByScoutB, []);
	} finally { await safeDisconnect(hubA, scoutA, hubB, scoutB); }
});

test("ACL, missing-target, and reply-correlation behavior remains unchanged", async () => {
	const hub = await connect(projectA, "orchestrator", "orchestrator", true);
	const scout = await connect(projectA, "scout", "scout");
	const planner = await connect(projectA, "planner", "planner");
	try {
		const missing = makeAgencyMessage({ kind: "delegate", from: "orchestrator", to: "missing", taskId: "missing" });
		assert.deepEqual(await hub.send("missing", missing), { id: missing.id, delivered: false, reason: "Session not found" });
		const peer = makeAgencyMessage({ kind: "progress", from: "scout", to: "planner", taskId: "peer" });
		assert.match((await scout.send("planner", peer)).reason || "", /ACL denied/);
		const badReply = makeAgencyMessage({ kind: "reply", from: "orchestrator", to: "scout", replyTo: "not-pending" });
		assert.match((await hub.send("scout", badReply)).reason || "", /pending ask/);
	} finally { await safeDisconnect(hub, scout, planner); }
});

test("same-project replacement disconnects only the stale local socket", async () => {
	const oldA = await connect(projectA, "orchestrator", "orchestrator", true);
	const hubB = await connect(projectB, "orchestrator", "orchestrator", true);
	let replacementA: AgencyBrokerClient | undefined;
	try {
		const oldDisconnected = new Promise<void>((resolve) => oldA.once("disconnected", () => resolve()));
		replacementA = await connect(projectA, "orchestrator", "orchestrator", true);
		let replacementTimeout: ReturnType<typeof setTimeout> | undefined;
		const replacementTimedOut = new Promise<never>((_, reject) => {
			replacementTimeout = setTimeout(() => reject(new Error("local replacement did not disconnect stale socket")), 2000);
		});
		try {
			await Promise.race([oldDisconnected, replacementTimedOut]);
		} finally {
			if (replacementTimeout) clearTimeout(replacementTimeout);
		}
		assert.equal(oldA.isConnected(), false);
		assert.equal(replacementA.isConnected(), true);
		assert.equal(hubB.isConnected(), true);
		assert.deepEqual((await hubB.listSessions()).map((session) => session.name), ["orchestrator"]);
	} finally { await safeDisconnect(oldA, replacementA, hubB); }
});

test("ask/reply correlation and logical envelope identities remain project-local", async () => {
	const hubA = await connect(projectA, "orchestrator", "orchestrator", true);
	const scoutA = await connect(projectA, "scout", "scout");
	const hubB = await connect(projectB, "orchestrator", "orchestrator", true);
	try {
		let markReplyDelivered!: () => void;
		const replyDelivered = new Promise<void>((resolve) => { markReplyDelivered = resolve; });
		hubA.on("message", async (_from, message) => {
			const reply = makeAgencyMessage({ kind: "reply", from: "orchestrator", to: "scout", taskId: message.taskId, replyTo: message.id, payload: { message: "A approved" } });
			await hubA.send("scout", reply);
			markReplyDelivered();
		});
		const ask = makeAgencyMessage({ kind: "ask", from: "scout", to: "orchestrator", taskId: "ask-a", payload: { message: "continue?" } });
		const reply = await scoutA.ask("orchestrator", ask, 5000);
		await replyDelivered;
		assert.equal(reply.from, "orchestrator");
		assert.equal(reply.to, "scout");
		assert.equal(reply.replyTo, ask.id);
		assert.equal(hubB.isConnected(), true);
	} finally { await safeDisconnect(hubA, scoutA, hubB); }
});

test("runtime ensureConnected registers a project-qualified transport identity", async () => {
	const unqualified = new AgencyBrokerClient(projectA);
	await unqualified.connect(registration(projectA, "shadow", "scout"), "agency:orchestrator");
	const runtime = new AgencyBrokerRuntime(projectA);
	try {
		await runtime.ensureConnected({ isHub: true, instance: { intercomName: "orchestrator", role: "orchestrator", cwd: projectA.projectRoot } });
		assert.equal(runtime.getBrokerStatus().connectionState, "connected");
		const sessions = await unqualified.listSessions();
		assert.ok(sessions.some((session) => session.id === "agency:orchestrator"));
		assert.ok(sessions.some((session) => session.id === buildAgencyTransportId(projectA, "orchestrator")));
	} finally { await runtime.disconnect(); await safeDisconnect(unqualified); }
});

test("isolated process ignores the real legacy fallback endpoint configured before imports", async () => {
	const fixture = join(import.meta.dirname, "legacy-global.fixture.ts");
	const tsxCli = getTsxCliPath();
	assert.ok(tsxCli);
	const agentDir = join(sandbox, "isolated-agent-dir");
	const isolatedProject = join(sandbox, "isolated-project-without-broker");
	mkdirSync(join(isolatedProject, ".pi", "agency"), { recursive: true });
	const output = await new Promise<string>((resolve, reject) => {
		const child = spawn(process.execPath, [tsxCli!, fixture], {
			env: { ...process.env, AGENCY_ROOT: undefined, AGENCY_PROJECT_ROOT: undefined, PI_CODING_AGENT_DIR: agentDir, TEST_AGENCY_PROJECT: isolatedProject },
			stdio: ["ignore", "pipe", "pipe"],
		});
		let stdout = "";
		let stderr = "";
		child.stdout!.on("data", (chunk) => { stdout += chunk; });
		child.stderr!.on("data", (chunk) => { stderr += chunk; });
		child.once("error", reject);
		child.once("exit", (code) => code === 0 ? resolve(stdout.trim()) : reject(new Error(`legacy fixture exited ${code}: ${stderr}`)));
	});
	const result = JSON.parse(output) as { connected: boolean; connections: number; legacySocket: string; selectedEndpoint: string };
	assert.equal(result.connected, false);
	assert.equal(result.connections, 0);
	assert.equal(result.legacySocket, join(agentDir, "agency-broker", "broker.sock"));
	assert.notEqual(result.selectedEndpoint, result.legacySocket);
});

test("one-sided concurrent startup failure is observable and the successful side is cleaned up", async () => {
	const good = projectContext("one-sided-good");
	const bad = projectContext("one-sided-bad");
	const results = await Promise.allSettled([
		spawnBrokerIfNeeded(good, "npx", ["--yes", "tsx"]),
		spawnBrokerIfNeeded(bad, join(sandbox, "missing-broker-command"), [], { startupTimeoutMs: 250 }),
	]);
	assert.deepEqual(results.map((result) => result.status), ["fulfilled", "rejected"]);
	await stopTestBroker(good);
	assert.equal(existsSync(good.pidFile), false);
	assert.equal(existsSync(bad.spawnLockFile), false);
});

after(async () => {
	await Promise.all([stopTestBroker(projectA), stopTestBroker(projectB)]);
	rmSync(sandbox, { recursive: true, force: true });
});
