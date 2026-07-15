import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { requireBrokerContext, resolveBrokerContext } from "../broker/paths.ts";
import { acquireBrokerSpawnLock, getBrokerLaunchSpec, getBrokerSpawnOptions, getTsxCliPath, isBrokerSpawnLockStale, releaseBrokerSpawnLock, spawnBrokerIfNeeded } from "../broker/spawn.ts";

test("broker spawn receives the exact canonical owning roots", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-spawn-context-"));
	try {
		const project = join(parent, "owner");
		mkdirSync(join(project, ".pi", "agency"), { recursive: true });
		const context = requireBrokerContext(resolveBrokerContext({ projectRoot: project, env: {} }));
		const options = getBrokerSpawnOptions(context, join(parent, "extension"), { PATH: "/bin" });
		assert.equal(options.env.AGENCY_PROJECT_ROOT, context.projectRoot);
		assert.equal(options.env.AGENCY_ROOT, context.agencyRoot);
		assert.equal(options.cwd, join(parent, "extension"));
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("broker spawn normalizes immutable Windows TCP transport flags", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-spawn-tcp-"));
	try {
		const project = join(parent, "owner");
		mkdirSync(join(project, ".pi", "agency"), { recursive: true });
		const enabled = requireBrokerContext(resolveBrokerContext({
			projectRoot: project,
			env: { AGENCY_BROKER_TCP: "true" },
			platform: "win32",
		}));
		const enabledEnv = getBrokerSpawnOptions(enabled, parent, {
			AGENCY_BROKER_TRANSPORT: "pipe",
			AGENCY_BROKER_TCP: "false",
		}).env;
		assert.equal(enabledEnv.AGENCY_BROKER_TRANSPORT, "tcp");
		assert.equal(enabledEnv.AGENCY_BROKER_TCP, "1");

		const disabled = requireBrokerContext(resolveBrokerContext({ projectRoot: project, env: {}, platform: "win32" }));
		const disabledEnv = getBrokerSpawnOptions(disabled, parent, {
			AGENCY_BROKER_TRANSPORT: "tcp",
			AGENCY_BROKER_TCP: "1",
		}).env;
		assert.equal(disabledEnv.AGENCY_BROKER_TRANSPORT, undefined);
		assert.equal(disabledEnv.AGENCY_BROKER_TCP, undefined);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("stale-lock inspection treats a disappeared lock as stale without a precheck", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-spawn-lock-"));
	try {
		const project = join(parent, "owner");
		mkdirSync(join(project, ".pi", "agency"), { recursive: true });
		const context = requireBrokerContext(resolveBrokerContext({ projectRoot: project, env: {} }));
		assert.equal(isBrokerSpawnLockStale(context), true);
		mkdirSync(context.brokerDir, { recursive: true });
		writeFileSync(context.spawnLockFile, `${JSON.stringify({ pid: process.pid, createdAt: Date.now(), token: "fresh" })}\n`);
		assert.equal(isBrokerSpawnLockStale(context), false);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("live lock owners never age-expire and only matching ownership tokens release", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-spawn-live-lock-"));
	try {
		const project = join(parent, "owner");
		mkdirSync(join(project, ".pi", "agency", "runtime", "broker"), { recursive: true });
		const context = requireBrokerContext(resolveBrokerContext({ projectRoot: project, env: {} }));
		writeFileSync(context.spawnLockFile, `${JSON.stringify({ pid: process.pid, createdAt: 1, token: "old-live" })}\n`);
		assert.equal(isBrokerSpawnLockStale(context), false);
		assert.equal(releaseBrokerSpawnLock(context, "wrong"), false);
		assert.equal(existsSync(context.spawnLockFile), true);
		assert.equal(releaseBrokerSpawnLock(context, "old-live"), true);
		assert.equal(existsSync(context.spawnLockFile), false);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("concurrent spawn-lock acquisition cannot steal a live owner's lock", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-spawn-concurrent-lock-"));
	try {
		const project = join(parent, "owner");
		mkdirSync(join(project, ".pi", "agency", "runtime", "broker"), { recursive: true });
		const context = requireBrokerContext(resolveBrokerContext({ projectRoot: project, env: {} }));
		const first = acquireBrokerSpawnLock(context);
		assert.ok(first);
		assert.equal(acquireBrokerSpawnLock(context), null);
		assert.equal(releaseBrokerSpawnLock(context, first!), true);
		const second = acquireBrokerSpawnLock(context);
		assert.ok(second);
		assert.notEqual(second, first);
		assert.equal(releaseBrokerSpawnLock(context, second!), true);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("hanging direct launcher is terminated and reaped before spawn ownership releases", async () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-spawn-hanging-"));
	try {
		const project = join(parent, "owner");
		mkdirSync(join(project, ".pi", "agency"), { recursive: true });
		const context = requireBrokerContext(resolveBrokerContext({ projectRoot: project, env: {} }));
		const childPidFile = join(parent, "child.pid");
		const script = `require('node:fs').writeFileSync(${JSON.stringify(childPidFile)}, String(process.pid)); setInterval(() => {}, 1000)`;
		await assert.rejects(
			spawnBrokerIfNeeded(context, process.execPath, ["-e", script], { startupTimeoutMs: 750 }),
			/failed to start within timeout/i,
		);
		const childPid = Number.parseInt(readFileSync(childPidFile, "utf8"), 10);
		assert.throws(() => process.kill(childPid, 0), (error: unknown) => (error as NodeJS.ErrnoException).code === "ESRCH");
		assert.equal(existsSync(context.spawnLockFile), false);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("Windows launch generation uses the required project-owned broker directory", () => {
	const brokerDir = join("C:\\owner", ".pi", "agency", "runtime", "broker");
	const spec = getBrokerLaunchSpec({
		brokerPath: "C:\\extension\\broker.ts",
		brokerCommand: "node",
		brokerArgs: [],
		brokerDir,
		extensionDir: "C:\\extension",
		platform: "win32",
		nodePath: "C:\\node.exe",
	});
	assert.equal(spec.kind, "windows-launcher");
	if (spec.kind === "windows-launcher") assert.equal(spec.launcherPath, join(brokerDir, "broker-launch.vbs"));
});

test("broker launch uses installed tsx or the network fallback in a linked checkout", () => {
	const extensionDir = mkdtempSync(join(tmpdir(), "agency-extension-no-tsx-"));
	try {
		const tsxCli = getTsxCliPath(extensionDir);
		const spec = getBrokerLaunchSpec({
			brokerPath: join(extensionDir, "broker.ts"),
			brokerCommand: "npx",
			brokerArgs: ["--no-install", "tsx"],
			brokerDir: join(extensionDir, "runtime"),
			extensionDir,
			platform: "darwin",
			nodePath: "/usr/local/bin/node",
		});
		assert.deepEqual(spec, tsxCli
			? { kind: "direct", command: "/usr/local/bin/node", args: [tsxCli, join(extensionDir, "broker.ts")] }
			: { kind: "direct", command: "npx", args: ["--yes", "tsx", join(extensionDir, "broker.ts")] });
	} finally {
		rmSync(extensionDir, { recursive: true, force: true });
	}
});
