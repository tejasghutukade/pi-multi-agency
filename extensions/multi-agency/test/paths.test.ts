import assert from "node:assert/strict";
import { chmodSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import {
	AGENCY_BROKER_DIR_MODE,
	AGENCY_BROKER_PROTOCOL_VERSION,
	AGENCY_BROKER_RUNTIME_FILE_MODE,
	discoverProjectRoot,
	ensureAgencyBrokerRuntimeDir,
	ensureAgencyBrokerSocketDir,
	getBrokerConnectTarget,
	getBrokerListenTarget,
	requireBrokerContext as available,
	resolveBrokerContext,
	restrictAgencyBrokerRuntimeFile,
	writeAgencyBrokerRuntimeFile,
} from "../broker/paths.ts";

function initializedProject(parent: string, name: string): string {
	const project = join(parent, name);
	mkdirSync(join(project, ".pi", "agency"), { recursive: true });
	return project;
}

test("explicit roots establish one canonical project-owned broker context", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-context-explicit-"));
	try {
		const project = initializedProject(parent, "project");
		const context = available(resolveBrokerContext({
			projectRoot: project,
			agencyRoot: join(project, ".pi", "agency"),
			env: {},
			cwd: parent,
			platform: "darwin",
		}));
		const canonicalProject = realpathSync.native(project);
		assert.equal(context.projectRoot, canonicalProject);
		assert.equal(context.agencyRoot, join(canonicalProject, ".pi", "agency"));
		assert.equal(context.brokerDir, join(canonicalProject, ".pi", "agency", "runtime", "broker"));
		assert.equal(context.endpoint, join("/tmp", `pi-agency-${process.getuid?.() ?? "user"}`, `${context.projectKey}.sock`));
		assert.ok(context.endpoint.length < 100);
		assert.match(context.projectKey, /^[a-f0-9]{16}$/);
		assert.equal(AGENCY_BROKER_PROTOCOL_VERSION, 1);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("relative roots resolve against the owning project and conflicting roots fail closed", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-context-relative-"));
	try {
		const project = initializedProject(parent, "project");
		const context = available(resolveBrokerContext({ projectRoot: "project", agencyRoot: ".pi/agency", env: {}, cwd: parent }));
		assert.equal(context.projectRoot, realpathSync.native(project));
		assert.equal(context.agencyRoot, join(realpathSync.native(project), ".pi", "agency"));

		const conflict = resolveBrokerContext({ projectRoot: project, agencyRoot: join(parent, "elsewhere", ".pi", "agency"), env: {} });
		assert.equal(conflict.available, false);
		assert.match(conflict.diagnostic, /does not match/i);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("project environment derives agency root and initialized discovery outranks nested package boundaries", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-context-discovery-"));
	try {
		const project = initializedProject(parent, "project");
		const nested = join(project, "packages", "child", "src");
		mkdirSync(nested, { recursive: true });
		writeFileSync(join(project, "packages", "child", "package.json"), "{}\n");

		const explicit = available(resolveBrokerContext({ env: { AGENCY_PROJECT_ROOT: project }, cwd: nested }));
		assert.equal(explicit.agencyRoot, join(realpathSync.native(project), ".pi", "agency"));
		const discovered = available(resolveBrokerContext({ env: {}, cwd: nested }));
		assert.equal(discovered.projectRoot, realpathSync.native(project));
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("project discovery preserves initialized canonical roots and package fallback", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-project-discovery-"));
	try {
		const initialized = initializedProject(parent, "initialized");
		const initializedNested = join(initialized, "nested");
		mkdirSync(initializedNested);
		assert.deepEqual(discoverProjectRoot(initializedNested), {
			projectRoot: realpathSync.native(initialized),
			initialized: true,
		});

		const packageRoot = join(parent, "package");
		const packageNested = join(packageRoot, "nested");
		mkdirSync(packageNested, { recursive: true });
		writeFileSync(join(packageRoot, "package.json"), "{}\n");
		assert.deepEqual(discoverProjectRoot(packageNested), {
			projectRoot: packageRoot,
			initialized: false,
		});
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("agency-only environment derives its conventional owning project", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-context-agency-only-"));
	try {
		const project = initializedProject(parent, "project");
		const context = available(resolveBrokerContext({ env: { AGENCY_ROOT: join(project, ".pi", "agency") }, cwd: parent }));
		assert.equal(context.projectRoot, realpathSync.native(project));
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("uninitialized cwd is unavailable and never selects the user-global broker", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-context-unavailable-"));
	try {
		writeFileSync(join(parent, "package.json"), "{}\n");
		const context = resolveBrokerContext({ env: {}, cwd: parent });
		assert.equal(context.available, false);
		assert.match(context.diagnostic, /agency-init|AGENCY_PROJECT_ROOT/i);
		assert.equal(context.endpoint, null);
		assert.doesNotMatch(context.diagnostic, /agency-broker$/);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("filesystem aliases produce one key while identical basenames remain distinct", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-context-key-"));
	try {
		const projectA = initializedProject(join(parent, "one"), "same");
		const projectB = initializedProject(join(parent, "two"), "same");
		const alias = join(parent, "alias");
		symlinkSync(projectA, alias, "dir");
		const a = available(resolveBrokerContext({ projectRoot: projectA, env: {} }));
		const viaAlias = available(resolveBrokerContext({ projectRoot: alias, env: {} }));
		const viaAgencyAlias = available(resolveBrokerContext({ env: { AGENCY_ROOT: join(alias, ".pi", "agency") } }));
		const b = available(resolveBrokerContext({ projectRoot: projectB, env: {} }));
		assert.equal(a.projectKey, viaAlias.projectKey);
		assert.equal(a.projectKey, viaAgencyAlias.projectKey);
		assert.equal(a.endpoint, viaAlias.endpoint);
		assert.notEqual(a.projectKey, b.projectKey);
		assert.notEqual(a.endpoint, b.endpoint);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("Unix, Windows pipe, and Windows TCP derivation is bounded and project-distinct", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-context-platform-"));
	try {
		const aRoot = initializedProject(join(parent, "one"), "same");
		const bRoot = initializedProject(join(parent, "two"), "same");
		const unixA = available(resolveBrokerContext({ projectRoot: aRoot, env: {}, platform: "linux" }));
		const unixB = available(resolveBrokerContext({ projectRoot: bRoot, env: {}, platform: "linux" }));
		assert.notEqual(unixA.endpoint, unixB.endpoint);

		const pipeA = available(resolveBrokerContext({ projectRoot: aRoot, env: {}, platform: "win32" }));
		const pipeB = available(resolveBrokerContext({ projectRoot: bRoot, env: {}, platform: "win32" }));
		assert.match(String(pipeA.endpoint), /^\\\\\.\\pipe\\multi-agency-[a-f0-9]{16}$/);
		assert.notEqual(pipeA.endpoint, pipeB.endpoint);
		assert.ok(String(pipeA.endpoint).length < 100);

		const tcpA = available(resolveBrokerContext({ projectRoot: aRoot, env: { AGENCY_BROKER_TRANSPORT: "tcp" }, platform: "win32" }));
		const tcpB = available(resolveBrokerContext({ projectRoot: bRoot, env: { AGENCY_BROKER_TRANSPORT: "tcp" }, platform: "win32" }));
		assert.notEqual(tcpA.portFile, tcpB.portFile);
		assert.deepEqual(getBrokerListenTarget(tcpA), { transport: "tcp", host: "127.0.0.1", port: 0 });
		mkdirSync(dirname(tcpA.portFile), { recursive: true });
		writeFileSync(tcpA.portFile, JSON.stringify({ transport: "tcp", host: "127.0.0.1", port: 43123, stateId: "secret" }));
		assert.deepEqual(getBrokerConnectTarget(tcpA), { transport: "tcp", host: "127.0.0.1", port: 43123, stateId: "secret" });
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("canonical agency roots cannot escape the project while project-root aliases remain valid", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-context-escape-"));
	try {
		const project = join(parent, "project");
		const external = join(parent, "external-agency");
		mkdirSync(join(project, ".pi"), { recursive: true });
		mkdirSync(external);
		symlinkSync(external, join(project, ".pi", "agency"), "dir");
		const escaped = resolveBrokerContext({ projectRoot: project, env: {} });
		assert.equal(escaped.available, false);
		assert.match(escaped.diagnostic, /escapes owning project/i);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("atomic runtime-state replacement does not follow pre-existing file symlinks", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-context-state-link-"));
	try {
		const context = available(resolveBrokerContext({ projectRoot: initializedProject(parent, "project"), env: {}, platform: "linux" }));
		ensureAgencyBrokerRuntimeDir(context.brokerDir, "linux", context.agencyRoot);
		const sentinel = join(parent, "sentinel");
		writeFileSync(sentinel, "external\n");
		symlinkSync(sentinel, context.pidFile);
		writeAgencyBrokerRuntimeFile(context.pidFile, "broker\n", "linux");
		assert.equal(readFileSync(sentinel, "utf8"), "external\n");
		assert.equal(readFileSync(context.pidFile, "utf8"), "broker\n");
		assert.equal(lstatSync(context.pidFile).isSymbolicLink(), false);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("runtime directories and files retain owner-only Unix modes", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-context-mode-"));
	try {
		const context = available(resolveBrokerContext({ projectRoot: initializedProject(parent, "project"), env: {}, platform: "linux" }));
		ensureAgencyBrokerRuntimeDir(context.brokerDir, "linux", context.agencyRoot);
		ensureAgencyBrokerSocketDir(context);
		assert.equal(statSync(context.brokerDir).mode & 0o777, AGENCY_BROKER_DIR_MODE);
		assert.equal(statSync(context.socketDir!).mode & 0o777, AGENCY_BROKER_DIR_MODE);
		writeFileSync(context.pidFile, "123\n");
		chmodSync(context.pidFile, 0o644);
		restrictAgencyBrokerRuntimeFile(context.pidFile, "linux");
		assert.equal(statSync(context.pidFile).mode & 0o777, AGENCY_BROKER_RUNTIME_FILE_MODE);
		assert.equal(readFileSync(context.pidFile, "utf8"), "123\n");
	} finally { rmSync(parent, { recursive: true, force: true }); }
});
