/**
 * Multi-Agency — lean spawn / list / delegate / wait / release / init tools.
 *
 * Package scripts live next to this extension; project state is always
 * `<cwd>/.pi/agency` after `agency_init`.
 * Lifecycle bridge (v0.3): agent_* hooks → sessions + silent-settle + hub push/queue.
 */

import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { StringEnum } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { installLifecycleBridge } from "./lifecycle.ts";

const EXT_DIR = path.dirname(fileURLToPath(import.meta.url));

function findPackageRoot(): string {
	let cur = EXT_DIR;
	for (let i = 0; i < 8; i++) {
		const ctl = path.join(cur, "agency", "scripts", "agency_ctl.py");
		const pkg = path.join(cur, "package.json");
		if (fs.existsSync(ctl) && fs.existsSync(pkg)) return cur;
		const parent = path.dirname(cur);
		if (parent === cur) break;
		cur = parent;
	}
	return path.resolve(EXT_DIR, "../..");
}

function findProjectRoot(start: string): string {
	let cur = path.resolve(start);
	for (let i = 0; i < 12; i++) {
		if (fs.existsSync(path.join(cur, ".pi", "agency")) || fs.existsSync(path.join(cur, "package.json"))) {
			return cur;
		}
		const parent = path.dirname(cur);
		if (parent === cur) break;
		cur = parent;
	}
	return path.resolve(start);
}

function agencyCtlPath(packageRoot: string): string {
	return path.join(packageRoot, "agency", "scripts", "agency_ctl.py");
}

function runCtl(
	packageRoot: string,
	projectRoot: string,
	args: string[],
	signal?: AbortSignal,
): Promise<{ code: number; stdout: string; stderr: string }> {
	return new Promise((resolve, reject) => {
		const child = spawn("python3", [agencyCtlPath(packageRoot), ...args], {
			cwd: projectRoot,
			env: {
				...process.env,
				AGENCY_ROOT: path.join(projectRoot, ".pi", "agency"),
				AGENCY_PROJECT_ROOT: projectRoot,
				PATH: `${process.env.HOME}/bin:${process.env.PATH || ""}`,
			},
		});
		let stdout = "";
		let stderr = "";
		child.stdout.on("data", (d) => {
			stdout += String(d);
		});
		child.stderr.on("data", (d) => {
			stderr += String(d);
		});
		const onAbort = () => {
			child.kill("SIGTERM");
		};
		signal?.addEventListener("abort", onAbort);
		child.on("error", (err) => {
			signal?.removeEventListener("abort", onAbort);
			reject(err);
		});
		child.on("close", (code) => {
			signal?.removeEventListener("abort", onAbort);
			resolve({ code: code ?? 1, stdout, stderr });
		});
	});
}

function textResult(obj: unknown) {
	return {
		content: [{ type: "text" as const, text: typeof obj === "string" ? obj : JSON.stringify(obj, null, 2) }],
		details: typeof obj === "object" && obj ? obj : { text: obj },
	};
}

export default function multiAgencyExtension(pi: ExtensionAPI) {
	const packageRoot = findPackageRoot();
	const projectRoot = findProjectRoot(process.cwd());
	const ctl = (args: string[], signal?: AbortSignal) => runCtl(packageRoot, projectRoot, args, signal);

	installLifecycleBridge(pi, ctl);

	pi.registerCommand("agency-init", {
		description: "Scaffold .pi/agency + .pi/agents in this project from the multi-agency package",
		handler: async (args, ctx) => {
			const force = /\b--force\b/.test(args || "");
			const argv = ["init", "--project", projectRoot];
			if (force) argv.push("--force");
			const r = await ctl(argv);
			if (r.code !== 0) {
				ctx.ui.notify(r.stderr.trim() || r.stdout.trim() || "agency_init failed", "error");
				return;
			}
			ctx.ui.notify("Agency project initialized", "info");
			ctx.ui.notify(r.stdout.trim().slice(0, 500), "info");
		},
	});

	pi.registerCommand("agency-claim", {
		description: "Claim this cmux surface as the Orchestrator hub",
		handler: async (_args, ctx) => {
			const r = await ctl(["claim-orchestrator"]);
			if (r.code !== 0) {
				ctx.ui.notify(r.stderr.trim() || r.stdout.trim() || "claim failed", "error");
				return;
			}
			ctx.ui.notify("Orchestrator claimed", "info");
		},
	});

	pi.registerCommand("agency-hub", {
		description: "Show the canonical Orchestrator hub start command (tools lock + persona)",
		handler: async (_args, ctx) => {
			const r = await ctl(["hub-start", "--project", projectRoot]);
			if (r.code !== 0) {
				ctx.ui.notify(r.stderr.trim() || r.stdout.trim() || "hub-start failed", "error");
				return;
			}
			try {
				const parsed = JSON.parse(r.stdout) as { command?: string; notes?: string[] };
				ctx.ui.notify(parsed.command || r.stdout.trim(), "info");
				for (const n of parsed.notes || []) ctx.ui.notify(n, "info");
			} catch {
				ctx.ui.notify(r.stdout.trim().slice(0, 800), "info");
			}
		},
	});

	pi.registerTool({
		name: "agency_init",
		label: "Agency init",
		description: "Scaffold project-local .pi/agency + .pi/agents from the installed multi-agency package",
		promptSnippet: "Initialize multi-agency files in this project",
		parameters: Type.Object({
			force: Type.Optional(Type.Boolean({ description: "Refresh templates even if already initialized" })),
		}),
		async execute(_id, params, signal) {
			const args = ["init", "--project", projectRoot];
			if (params.force) args.push("--force");
			const r = await ctl(args, signal);
			if (r.code !== 0) throw new Error(r.stderr.trim() || r.stdout.trim() || "agency_init failed");
			return textResult(JSON.parse(r.stdout));
		},
	});

	pi.registerTool({
		name: "agency_list",
		label: "Agency list",
		description: "List multi-agency specialist instances (sessions.json) after cmux stale reconcile",
		promptSnippet: "List agency specialist panes and status",
		parameters: Type.Object({}),
		async execute(_id, _params, signal) {
			const r = await ctl(["list"], signal);
			if (r.code !== 0) throw new Error(r.stderr.trim() || r.stdout.trim() || "agency_list failed");
			return textResult(JSON.parse(r.stdout));
		},
	});

	pi.registerTool({
		name: "agency_spawn",
		label: "Agency spawn",
		description:
			"Orchestrator-only: spawn or reuse a specialist cmux pane, register sessions.json, init bus inbox, bootstrap pi",
		promptSnippet: "Spawn or reuse an agency specialist pane",
		promptGuidelines: [
			"Only the Orchestrator may call agency_spawn",
			"Prefer --reuse semantics via reuse=true when an idle instance of the role exists",
			"Do not spawn a second Work while one is working",
			"After agency_delegate, do not block in agency_wait — lifecycle bridge delivers reports",
		],
		parameters: Type.Object({
			role: Type.String({ description: "Agent role id from agents.yaml (e.g. scout, brainstorm, plan)" }),
			lifecycle: Type.Optional(StringEnum(["temporary", "persistent"] as const)),
			name: Type.Optional(Type.String({ description: "Override instance/bus name" })),
			direction: Type.Optional(StringEnum(["left", "right", "up", "down"] as const)),
			reuse: Type.Optional(Type.Boolean({ description: "Reuse idle instance of role if present" })),
			dryRun: Type.Optional(Type.Boolean()),
			bootWaitSec: Type.Optional(Type.Number()),
			cwd: Type.Optional(Type.String({ description: "Pane cwd (Scout reference-repo)" })),
			nudge: Type.Optional(Type.Boolean()),
		}),
		async execute(_id, params, signal) {
			const args = ["spawn", "--role", params.role];
			if (params.lifecycle) args.push("--lifecycle", params.lifecycle);
			if (params.name) args.push("--name", params.name);
			if (params.direction) args.push("--direction", params.direction);
			if (params.reuse) args.push("--reuse");
			if (params.dryRun) args.push("--dry-run");
			if (params.bootWaitSec != null) args.push("--boot-wait", String(params.bootWaitSec));
			if (params.cwd) args.push("--cwd", params.cwd);
			if (params.nudge === false) args.push("--no-nudge");
			if (params.nudge === true) args.push("--nudge");
			const r = await ctl(args, signal);
			if (r.code !== 0) throw new Error(r.stderr.trim() || r.stdout.trim() || "agency_spawn failed");
			return textResult(JSON.parse(r.stdout));
		},
	});

	pi.registerTool({
		name: "agency_delegate",
		label: "Agency delegate",
		description:
			"Orchestrator-only: send a hybrid-bus delegate envelope and mark the instance working. Stay free after delegate — lifecycle bridge pushes/queues the report.",
		promptSnippet: "Delegate a task to an agency specialist via the file bus",
		promptGuidelines: [
			"After delegate, continue other work or wait for the pushed report — do not call agency_wait unless debugging",
		],
		parameters: Type.Object({
			to: Type.String({ description: "Instance / bus name" }),
			taskId: Type.String(),
			goal: Type.Optional(Type.String()),
			workflowId: Type.Optional(Type.String()),
			contextPaths: Type.Optional(Type.Array(Type.String())),
			successCriteria: Type.Optional(Type.String()),
			constraints: Type.Optional(Type.String()),
			charterPath: Type.Optional(Type.String()),
			skillPath: Type.Optional(Type.String()),
			outputShape: Type.Optional(Type.String()),
			stopRules: Type.Optional(Type.String()),
			payloadJson: Type.Optional(Type.String()),
		}),
		async execute(_id, params, signal) {
			const args = ["delegate", "--to", params.to, "--task-id", params.taskId];
			if (params.goal) args.push("--goal", params.goal);
			if (params.workflowId) args.push("--workflow-id", params.workflowId);
			if (params.contextPaths) args.push("--context-paths", JSON.stringify(params.contextPaths));
			if (params.successCriteria) args.push("--success-criteria", params.successCriteria);
			if (params.constraints) args.push("--constraints", params.constraints);
			if (params.charterPath) args.push("--charter-path", params.charterPath);
			if (params.skillPath) args.push("--skill-path", params.skillPath);
			if (params.outputShape) args.push("--output-shape", params.outputShape);
			if (params.stopRules) args.push("--stop-rules", params.stopRules);
			if (params.payloadJson) args.push("--payload-json", params.payloadJson);
			const r = await ctl(args, signal);
			if (r.code !== 0) throw new Error(r.stderr.trim() || r.stdout.trim() || "agency_delegate failed");
			return textResult(JSON.parse(r.stdout));
		},
	});

	pi.registerTool({
		name: "agency_wait",
		label: "Agency wait",
		description:
			"LEGACY: poll hub inbox for a taskId. Prefer lifecycle bridge push/queue. Keep only for debugging or migration.",
		promptSnippet: "Legacy wait for an agency specialist report by taskId",
		promptGuidelines: [
			"Prefer lifecycle bridge delivery over agency_wait",
			"On pane_dead: agency_list → release → spawn + delegate",
		],
		parameters: Type.Object({
			taskId: Type.String({ description: "Same taskId used in agency_delegate" }),
			timeoutSec: Type.Optional(Type.Number({ description: "Seconds to wait (default 120)" })),
			intervalSec: Type.Optional(Type.Number({ description: "Poll interval seconds (default 2)" })),
			autoDoneProgress: Type.Optional(
				Type.Boolean({ description: "Ack matching progress and keep waiting (default true)" }),
			),
		}),
		async execute(_id, params, signal) {
			const args = ["wait", "--task-id", params.taskId];
			if (params.timeoutSec != null) args.push("--timeout", String(params.timeoutSec));
			if (params.intervalSec != null) args.push("--interval", String(params.intervalSec));
			if (params.autoDoneProgress === false) args.push("--no-auto-done-progress");
			if (params.autoDoneProgress === true) args.push("--auto-done-progress");
			const r = await ctl(args, signal);
			if (r.code !== 0) throw new Error(r.stderr.trim() || r.stdout.trim() || "agency_wait failed");
			return textResult(JSON.parse(r.stdout));
		},
	});

	pi.registerTool({
		name: "agency_release",
		label: "Agency release",
		description:
			"Orchestrator-only: mark persistent idle or tear down temporary (close cmux surface + clear sessions row)",
		promptSnippet: "Release an agency specialist (idle or teardown)",
		parameters: Type.Object({
			name: Type.String({ description: "Instance / bus name" }),
			mode: Type.Optional(StringEnum(["auto", "idle", "teardown"] as const)),
			keepPane: Type.Optional(Type.Boolean()),
			force: Type.Optional(Type.Boolean()),
		}),
		async execute(_id, params, signal) {
			const args = ["release", "--name", params.name];
			if (params.mode) args.push("--mode", params.mode);
			if (params.keepPane) args.push("--keep-pane");
			if (params.force) args.push("--force");
			const r = await ctl(args, signal);
			if (r.code !== 0) throw new Error(r.stderr.trim() || r.stdout.trim() || "agency_release failed");
			return textResult(JSON.parse(r.stdout));
		},
	});
}
