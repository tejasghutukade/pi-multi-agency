import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { registerAgencyBrokerCommands } from "../broker-commands.ts";
import { AgencyBrokerRuntime } from "../broker-runtime.ts";
import { requireBrokerContext, resolveBrokerContext } from "../broker/paths.ts";

test("broker commands register and claim refreshes identity before immediate connected status", async () => {
	const parent = mkdtempSync(join(tmpdir(), "agency-index-claim-"));
	try {
		const project = join(parent, "project");
		mkdirSync(join(project, ".pi", "agency"), { recursive: true });
		const context = requireBrokerContext(resolveBrokerContext({ projectRoot: project, env: {} }));
		const runtime = new AgencyBrokerRuntime(context);
		let connected = false;
		let connectedIdentity: unknown;
		runtime.ensureConnected = async (identity) => { connectedIdentity = identity; connected = true; };
		runtime.getBrokerStatus = () => ({
			projectRoot: context.projectRoot,
			agencyRoot: context.agencyRoot,
			projectKey: context.projectKey,
			endpoint: context.endpoint,
			connectionState: connected ? "connected" : "disconnected",
			diagnostic: context.diagnostic,
		});

		const commands = new Map<string, { handler: (args: string, ctx: any) => Promise<void> }>();
		const pi = { registerCommand: (name: string, command: any) => { commands.set(name, command); } } as Pick<ExtensionAPI, "registerCommand">;
		const calls: string[][] = [];
		const identity = { ok: true, isHub: true, instance: { intercomName: "orchestrator", role: "orchestrator", cwd: project } };
		registerAgencyBrokerCommands(pi, async (args) => {
			calls.push(args);
			return args[0] === "claim-orchestrator"
				? { code: 0, stdout: "{}", stderr: "" }
				: { code: 0, stdout: JSON.stringify(identity), stderr: "" };
		}, runtime);
		assert.deepEqual([...commands.keys()].sort(), ["agency-broker-status", "agency-claim"]);

		const notifications: Array<{ text: string; level?: string }> = [];
		const ctx = { ui: { notify: (text: string, level?: string) => notifications.push({ text, level }) } };
		await commands.get("agency-broker-status")!.handler("", ctx);
		assert.match(notifications.at(-1)!.text, /Agency broker: disconnected/);
		await commands.get("agency-claim")!.handler("", ctx);
		assert.deepEqual(calls, [["claim-orchestrator"], ["lifecycle", "whoami"]]);
		assert.deepEqual(connectedIdentity, identity);
		await commands.get("agency-broker-status")!.handler("", ctx);
		assert.match(notifications.at(-1)!.text, /Agency broker: connected/);
	} finally { rmSync(parent, { recursive: true, force: true }); }
});
