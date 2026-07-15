import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { AgencyBrokerRuntime, buildAgencyTransportId } from "../broker-runtime.ts";
import { requireBrokerContext, resolveBrokerContext } from "../broker/paths.ts";

test("broker status is a read-only local diagnostic with no broker round trip", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-runtime-status-"));
	try {
		const project = join(parent, "project");
		mkdirSync(join(project, ".pi", "agency"), { recursive: true });
		const context = requireBrokerContext(resolveBrokerContext({ projectRoot: project, env: {} }));
		const runtime = new AgencyBrokerRuntime(context);
		const status = runtime.getBrokerStatus();
		assert.equal(status.projectRoot, context.projectRoot);
		assert.equal(status.agencyRoot, context.agencyRoot);
		assert.equal(status.projectKey, context.projectKey);
		assert.equal(status.endpoint, context.endpoint);
		assert.equal(status.connectionState, "disconnected");
		assert.equal(runtime.isConnected(), false);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("transport IDs are stable within a project and distinct across projects", () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-runtime-id-"));
	try {
		const projectA = join(parent, "a", "same");
		const projectB = join(parent, "b", "same");
		mkdirSync(join(projectA, ".pi", "agency"), { recursive: true });
		mkdirSync(join(projectB, ".pi", "agency"), { recursive: true });
		const contextA = requireBrokerContext(resolveBrokerContext({ projectRoot: projectA, env: {} }));
		const contextAAgain = requireBrokerContext(resolveBrokerContext({ projectRoot: projectA, env: {} }));
		const contextB = requireBrokerContext(resolveBrokerContext({ projectRoot: projectB, env: {} }));
		const idA = buildAgencyTransportId(contextA, "orchestrator");
		assert.equal(idA, buildAgencyTransportId(contextAAgain, "orchestrator"));
		assert.notEqual(idA, buildAgencyTransportId(contextB, "orchestrator"));
		assert.match(idA, /^agency:[a-f0-9]{16}:orchestrator$/);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});

test("uninitialized runtime remains unavailable with actionable guidance", async () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-runtime-unavailable-"));
	try {
		const context = resolveBrokerContext({ env: {}, cwd: parent });
		const runtime = new AgencyBrokerRuntime(context);
		assert.equal(runtime.getBrokerStatus().connectionState, "unavailable");
		await assert.rejects(
			runtime.ensureConnected({ instance: { intercomName: "scout", role: "scout", cwd: parent } }),
			/agency-init|AGENCY_PROJECT_ROOT/i,
		);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});
