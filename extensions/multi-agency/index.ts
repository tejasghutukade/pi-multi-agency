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
import type { ExtensionAPI, ExtensionCommandContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { registerAgencyBrokerCommands } from "./broker-commands.ts";
import { AgencyBrokerRuntime } from "./broker-runtime.ts";
import { discoverProjectRoot, resolveBrokerContext } from "./broker/paths.ts";
import { installLifecycleBridge } from "./lifecycle.ts";
import { makeAgencyMessage } from "./messages.ts";
import { buildAgencyReportPayload, isPipelineRunnerTarget } from "./pipeline-routing.ts";

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

type AgencyInstance = {
	intercomName?: string;
	role?: string;
	status?: string;
	lifecycle?: string;
	taskId?: string | null;
	cmuxSurface?: string | null;
	cmuxPane?: string | null;
};

type AgencyListPayload = {
	specialistCount?: number;
	instances?: AgencyInstance[];
	reconcile?: {
		ok?: boolean;
		before?: number;
		after?: number;
		at?: string;
		error?: string;
		cleared?: Array<{ intercomName?: string; status?: string }>;
	};
};

function formatAgencyList(payload: AgencyListPayload): string {
	const instances = payload.instances || [];
	const specialistCount = payload.specialistCount ?? Math.max(0, instances.length - 1);
	const rec = payload.reconcile || {};
	const cleared = rec.cleared || [];

	const lines: string[] = [];
	lines.push("Agency roster");
	lines.push(`- Instances: ${instances.length} (specialists: ${specialistCount})`);
	if (rec.error) {
		lines.push(`- Reconcile: error (${rec.error})`);
	} else if (rec.ok) {
		lines.push(
			`- Reconcile: before ${rec.before ?? "?"} → after ${rec.after ?? "?"}; cleared ${cleared.length}${rec.at ? ` at ${rec.at}` : ""}`,
		);
	}
	lines.push("");

	if (!instances.length) {
		lines.push("No active instances.");
	} else {
		lines.push("| Instance | Role | Status | Lifecycle | Task | Surface | Pane |");
		lines.push("|---|---|---|---|---|---|---|");
		for (const i of instances) {
			lines.push(
				`| ${i.intercomName || "-"} | ${i.role || "-"} | ${i.status || "-"} | ${i.lifecycle || "-"} | ${i.taskId || "-"} | ${i.cmuxSurface || "-"} | ${i.cmuxPane || "-"} |`,
			);
		}
	}

	if (cleared.length) {
		lines.push("");
		lines.push("Cleared stale sessions:");
		for (const c of cleared) {
			lines.push(`- ${c.intercomName || "unknown"}${c.status ? ` (${c.status})` : ""}`);
		}
	}

	return lines.join("\n");
}

type AgencyDelegatePayload = {
	ok?: boolean;
	action?: string;
	to?: string;
	taskId?: string;
	bus?: {
		ok?: boolean;
		id?: string;
		path?: string;
		notified?: boolean;
		transport?: "broker" | string;
		delivered?: boolean;
		reason?: string;
	};
	wake?: {
		attempted?: boolean;
		ok?: boolean;
		surface?: string;
		error?: string;
	} | null;
	instance?: AgencyInstance & {
		lastDelegate?: {
			workflowId?: string;
			payload?: {
				goal?: string;
				outputShape?: string;
			};
		};
	};
};

function clip(text: string | undefined, max = 140): string {
	if (!text) return "-";
	const t = text.replace(/\s+/g, " ").trim();
	if (t.length <= max) return t;
	return `${t.slice(0, max - 1)}…`;
}

function formatAgencyDelegate(payload: AgencyDelegatePayload): string {
	const b = payload.bus || {};
	const inst = payload.instance || {};
	const d = inst.lastDelegate || {};
	const p = d.payload || {};
	const w = payload.wake;

	const lines: string[] = [];
	lines.push("Delegate delivered");
	lines.push(`- To: ${payload.to || "-"}`);
	lines.push(`- taskId: ${payload.taskId || "-"}`);
	if (d.workflowId) lines.push(`- workflowId: ${d.workflowId}`);
	lines.push(`- Transport: ${b.transport || "broker"}${b.delivered != null ? ` (${b.delivered ? "delivered" : "not delivered"})` : ""}`);
	if (b.id) lines.push(`- Message id: ${b.id}`);
	if (b.reason) lines.push(`- Transport reason: ${b.reason}`);
	if (w?.attempted) {
		lines.push(`- Wake ping: ${w.ok ? "sent" : "failed"}${w.surface ? ` (${w.surface})` : ""}`);
		if (w.error) lines.push(`- Wake error: ${w.error}`);
	}
	lines.push("");
	lines.push("Instance state");
	lines.push(`- ${inst.intercomName || "-"} · ${inst.role || "-"} · ${inst.status || "-"}`);
	lines.push(`- Surface: ${inst.cmuxSurface || "-"}  Pane: ${inst.cmuxPane || "-"}`);
	lines.push("");
	lines.push("Delegate payload");
	lines.push(`- Goal: ${clip(p.goal, 180)}`);
	lines.push(`- Output: ${clip(p.outputShape, 140)}`);

	return lines.join("\n");
}

type AgencyReleasePayload = {
	action?: "idle" | "teardown" | string;
	instance?: AgencyInstance;
	cleared?: string;
	closed?: { ok?: boolean } | null;
};

type AgencySpawnPayload = {
	action?: "spawn" | "reuse" | "spawn-dry-run" | string;
	instance?: AgencyInstance & {
		cwd?: string;
		agentPath?: string | null;
	};
	bootWaitSec?: number;
	bootPromptPath?: string;
	piCommand?: string;
};

type AgencyInitPayload = {
	action?: "init" | string;
	skipped?: boolean;
	reason?: string;
	projectRoot?: string;
	agencyRoot?: string;
	packageRoot?: string;
	copied?: string[];
	next?: string[];
};

function formatAgencyInit(payload: AgencyInitPayload): string {
	const lines: string[] = [];
	if (payload.skipped) {
		lines.push("Agency already initialized");
		if (payload.reason) lines.push(`- Reason: ${payload.reason}`);
	} else {
		lines.push("Agency initialized");
		lines.push(`- Copied entries: ${(payload.copied || []).length}`);
	}
	if (payload.projectRoot) lines.push(`- Project: ${payload.projectRoot}`);
	if (payload.agencyRoot) lines.push(`- Agency root: ${payload.agencyRoot}`);
	if (!payload.skipped && (payload.next || []).length) {
		lines.push("");
		lines.push("Next:");
		for (const step of payload.next || []) lines.push(`- ${step}`);
	}
	return lines.join("\n");
}

function formatAgencySpawn(payload: AgencySpawnPayload): string {
	const i = payload.instance || {};
	if (payload.action === "reuse") {
		return [
			"Reused existing instance",
			`- Name: ${i.intercomName || "-"}`,
			`- Role: ${i.role || "-"}`,
			`- Status: ${i.status || "-"}`,
			`- Surface: ${i.cmuxSurface || "-"}  Pane: ${i.cmuxPane || "-"}`,
		].join("\n");
	}

	const title = payload.action === "spawn-dry-run" ? "Spawn dry-run prepared" : "Specialist spawned";
	const lines: string[] = [title];
	lines.push(`- Name: ${i.intercomName || "-"}`);
	lines.push(`- Role: ${i.role || "-"} (${i.lifecycle || "-"})`);
	lines.push(`- Status: ${i.status || "-"}`);
	lines.push(`- Surface: ${i.cmuxSurface || "-"}  Pane: ${i.cmuxPane || "-"}`);
	if (i.cwd) lines.push(`- CWD: ${i.cwd}`);
	if (payload.action !== "spawn-dry-run") {
		lines.push(`- Boot wait: ${payload.bootWaitSec ?? "-"}s`);
		if (payload.bootPromptPath) lines.push(`- Boot prompt: ${payload.bootPromptPath}`);
	}
	if (payload.piCommand) lines.push(`- Pi command: ${clip(payload.piCommand, 160)}`);
	return lines.join("\n");
}

function formatAgencyRelease(payload: AgencyReleasePayload): string {
	if (payload.action === "idle") {
		const i = payload.instance || {};
		return [
			"Instance set to idle (pane retained)",
			`- Name: ${i.intercomName || "-"}`,
			`- Role: ${i.role || "-"}`,
			`- Status: ${i.status || "idle"}`,
			`- Task cleared: ${i.taskId ? "no" : "yes"}`,
			"- To close pane + remove from roster: use --mode teardown",
		].join("\n");
	}
	if (payload.action === "teardown") {
		const closeState = payload.closed == null ? "not requested" : payload.closed.ok ? "ok" : "failed";
		return ["Instance released", `- Cleared: ${payload.cleared || "-"}`, `- Pane close: ${closeState}`].join("\n");
	}
	return `Release result: ${payload.action || "ok"}`;
}

export default function multiAgencyExtension(pi: ExtensionAPI) {
	const packageRoot = findPackageRoot();
	const brokerContext = resolveBrokerContext({ env: process.env, cwd: process.cwd() });
	const projectRoot = brokerContext.projectRoot || discoverProjectRoot(process.cwd()).projectRoot;
	const ctl = (args: string[], signal?: AbortSignal) => runCtl(packageRoot, projectRoot, args, signal);
	const broker = new AgencyBrokerRuntime(brokerContext);

	installLifecycleBridge(pi, ctl, broker);
	registerAgencyBrokerCommands(pi, ctl, broker);

	const commandAgencyList = async (_args: string, ctx: ExtensionCommandContext) => {
		const r = await ctl(["list"]);
		if (r.code !== 0) {
			ctx.ui.notify(r.stderr.trim() || r.stdout.trim() || "agency_list failed", "error");
			return;
		}
		const payload = JSON.parse(r.stdout) as AgencyListPayload;
		ctx.ui.notify(formatAgencyList(payload), "info");
	};

	const commandAgencyRelease = async (
		rawArgs: string,
		ctx: ExtensionCommandContext,
	) => {
		const tokens = (rawArgs || "").trim().split(/\s+/).filter(Boolean);
		let name: string | undefined;
		let mode: "auto" | "idle" | "teardown" = "auto";
		let keepPane = false;
		let force = false;

		for (let i = 0; i < tokens.length; i++) {
			const t = tokens[i];
			if (t === "--name" && tokens[i + 1]) {
				name = tokens[++i];
				continue;
			}
			if (t === "--mode" && tokens[i + 1]) {
				const m = tokens[++i] as "auto" | "idle" | "teardown";
				if (m !== "auto" && m !== "idle" && m !== "teardown") {
					ctx.ui.notify(`invalid --mode: ${m}`, "error");
					return;
				}
				mode = m;
				continue;
			}
			if (t === "--keep-pane") {
				keepPane = true;
				continue;
			}
			if (t === "--force") {
				force = true;
				continue;
			}
			if (t.startsWith("--")) {
				ctx.ui.notify(`unknown flag: ${t}`, "error");
				ctx.ui.notify("usage: /agency_release <name> [--mode auto|idle|teardown] [--keep-pane] [--force]", "info");
				return;
			}
			if (!name) {
				name = t;
				continue;
			}
			ctx.ui.notify(`unexpected arg: ${t}`, "error");
			ctx.ui.notify("usage: /agency_release <name> [--mode auto|idle|teardown] [--keep-pane] [--force]", "info");
			return;
		}

		if (!name) {
			ctx.ui.notify("usage: /agency_release <name> [--mode auto|idle|teardown] [--keep-pane] [--force]", "info");
			return;
		}

		const args = ["release", "--name", name, "--mode", mode];
		if (keepPane) args.push("--keep-pane");
		if (force) args.push("--force");
		const r = await ctl(args);
		if (r.code !== 0) {
			ctx.ui.notify(r.stderr.trim() || r.stdout.trim() || "agency_release failed", "error");
			return;
		}
		const payload = JSON.parse(r.stdout) as AgencyReleasePayload;
		ctx.ui.notify(formatAgencyRelease(payload), "info");
	};

	pi.registerCommand("agency_list", {
		description: "List agency instances (readable summary)",
		handler: commandAgencyList,
	});

	pi.registerCommand("agency_release", {
		description: "Release an instance: /agency_release <name> [--mode auto|idle|teardown]",
		handler: commandAgencyRelease,
	});

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

	pi.registerCommand("agency-ops", {
		description: "Ops observer manager: /agency-ops start|stop|status [--port N]",
		handler: async (rawArgs, ctx) => {
			const args = (rawArgs || "").trim().split(/\s+/).filter(Boolean);
			const action = (args[0] || "start").toLowerCase();
			let port = 8765;
			for (let i = 1; i < args.length; i++) {
				if (args[i] === "--port" && args[i + 1]) {
					const n = Number(args[i + 1]);
					if (!Number.isFinite(n) || n < 1 || n > 65535) {
						ctx.ui.notify(`invalid --port: ${args[i + 1]}`, "error");
						return;
					}
					port = Math.trunc(n);
					i++;
				}
			}

			const agencyRoot = path.join(projectRoot, ".pi", "agency");
			const pidPath = path.join(agencyRoot, "observe.pid.json");
			const logPath = path.join(agencyRoot, "observe.log");
			const pidAlive = (pid: number) => {
				try {
					process.kill(pid, 0);
					return true;
				} catch {
					return false;
				}
			};
			const readPid = () => {
				if (!fs.existsSync(pidPath)) return null;
				try {
					return JSON.parse(fs.readFileSync(pidPath, "utf8")) as {
						pid: number;
						port?: number;
						startedAt?: string;
					};
				} catch {
					return null;
				}
			};

			if (action === "status") {
				const info = readPid();
				if (!info?.pid || !pidAlive(info.pid)) {
					ctx.ui.notify("ops observer: not running", "info");
					return;
				}
				ctx.ui.notify(`ops observer running: http://127.0.0.1:${info.port || 8765}/`, "info");
				return;
			}

			if (action === "stop") {
				const info = readPid();
				if (!info?.pid) {
					ctx.ui.notify("ops observer: not running", "info");
					return;
				}
				if (pidAlive(info.pid)) {
					try {
						process.kill(info.pid, "SIGTERM");
					} catch {
						/* already gone */
					}
				}
				try {
					fs.unlinkSync(pidPath);
				} catch {
					/* ignore */
				}
				ctx.ui.notify("ops observer stopped", "info");
				return;
			}

			if (action !== "start") {
				ctx.ui.notify("usage: /agency-ops start|stop|status [--port 8765]", "info");
				return;
			}

			if (!fs.existsSync(agencyRoot)) {
				const r = await ctl(["init", "--project", projectRoot]);
				if (r.code !== 0) {
					ctx.ui.notify(r.stderr.trim() || r.stdout.trim() || "agency init failed", "error");
					return;
				}
			}

			const running = readPid();
			if (running?.pid && pidAlive(running.pid)) {
				ctx.ui.notify(`ops observer already running: http://127.0.0.1:${running.port || 8765}/`, "info");
				return;
			}

			fs.mkdirSync(agencyRoot, { recursive: true });
			const outFd = fs.openSync(logPath, "a");
			const child = spawn("python3", [agencyCtlPath(packageRoot), "observe", "--root", agencyRoot, "--host", "127.0.0.1", "--port", String(port)], {
				cwd: projectRoot,
				env: {
					...process.env,
					AGENCY_ROOT: agencyRoot,
					AGENCY_PROJECT_ROOT: projectRoot,
					PATH: `${process.env.HOME}/bin:${process.env.PATH || ""}`,
				},
				detached: true,
				stdio: ["ignore", outFd, outFd],
			});
			child.unref();
			fs.closeSync(outFd);
			if (!child.pid) {
				ctx.ui.notify("failed to start ops observer", "error");
				return;
			}
			fs.writeFileSync(
				pidPath,
				JSON.stringify({ pid: child.pid, port, startedAt: new Date().toISOString() }, null, 2) + "\n",
			);
			ctx.ui.notify(`ops observer started: http://127.0.0.1:${port}/`, "info");
			ctx.ui.notify(`log: ${path.join(".pi", "agency", "observe.log")}`, "info");
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
			const payload = JSON.parse(r.stdout) as AgencyInitPayload;
			return {
				content: [{ type: "text" as const, text: formatAgencyInit(payload) }],
				details: payload,
			};
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
			const payload = JSON.parse(r.stdout) as AgencyListPayload;
			return {
				content: [{ type: "text" as const, text: formatAgencyList(payload) }],
				details: payload,
			};
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
			"After agency_delegate, stay free — lifecycle bridge delivers reports",
		],
		parameters: Type.Object({
			role: Type.String({ description: "Agent role id from agents.yaml (e.g. scout, brainstorm, plan)" }),
			lifecycle: Type.Optional(StringEnum(["temporary", "persistent"] as const)),
			name: Type.Optional(Type.String({ description: "Override specialist instance name" })),
			direction: Type.Optional(StringEnum(["left", "right", "up", "down"] as const)),
			reuse: Type.Optional(Type.Boolean({ description: "Reuse idle instance of role if present" })),
			dryRun: Type.Optional(Type.Boolean()),
			bootWaitSec: Type.Optional(Type.Number()),
			cwd: Type.Optional(Type.String({ description: "Pane cwd (Scout reference-repo)" })),
			pipeline: Type.Optional(Type.String({ description: "Pipeline name from pipelines.yaml (pipeline-runner init)" })),
			topic: Type.Optional(Type.String({ description: "Pipeline topic (pipeline-runner init)" })),
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
			if (params.pipeline) args.push("--pipeline-name", params.pipeline);
			if (params.topic) args.push("--topic", params.topic);
			const r = await ctl(args, signal);
			if (r.code !== 0) throw new Error(r.stderr.trim() || r.stdout.trim() || "agency_spawn failed");
			const payload = JSON.parse(r.stdout) as AgencySpawnPayload;
			return {
				content: [{ type: "text" as const, text: formatAgencySpawn(payload) }],
				details: payload,
			};
		},
	});

	pi.registerTool({
		name: "agency_delegate",
		label: "Agency delegate",
		description:
			"Orchestrator-only: send a broker delegate message and mark the instance working.",
		promptSnippet: "Delegate a task to an agency specialist via the agency broker",
		promptGuidelines: [
			"After delegate, continue other work or wait for the pushed report",
		],
		parameters: Type.Object({
			to: Type.String({ description: "Specialist instance name" }),
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

			const preflight = await ctl([...args, "--prepare-only"], signal);
			if (preflight.code !== 0) throw new Error(preflight.stderr.trim() || preflight.stdout.trim() || "agency_delegate preflight failed");
			const preflightPayload = JSON.parse(preflight.stdout) as { payload?: Record<string, unknown>; instance?: AgencyInstance };
			const livePayload = preflightPayload.payload || {};

			if (isPipelineRunnerTarget(preflightPayload)) {
				const r = await ctl(args, signal);
				if (r.code !== 0) throw new Error(r.stderr.trim() || r.stdout.trim() || "agency_delegate failed");
				const payload = JSON.parse(r.stdout) as AgencyDelegatePayload;
				return {
					content: [{ type: "text" as const, text: formatAgencyDelegate(payload) }],
					details: payload,
				};
			}

			let brokerResult: { delivered: boolean; id?: string; reason?: string } | null = null;
			try {
				const msg = makeAgencyMessage({
					kind: "delegate",
					from: broker.sessionName || "orchestrator",
					to: params.to,
					taskId: params.taskId,
					workflowId: params.workflowId,
					payload: livePayload,
				});
				const delivered = await broker.send(params.to, msg);
				brokerResult = delivered;
			} catch (e) {
				brokerResult = { delivered: false, reason: e instanceof Error ? e.message : String(e) };
			}

			if (!brokerResult.delivered) {
				throw new Error(brokerResult.reason || `agency_delegate broker delivery failed for ${params.to}`);
			}

			const finalArgs = [...args, "--no-bus"];
			const r = await ctl(finalArgs, signal);
			if (r.code !== 0) throw new Error(r.stderr.trim() || r.stdout.trim() || "agency_delegate failed");
			const payload = JSON.parse(r.stdout) as AgencyDelegatePayload;
			payload.bus = {
				...(payload.bus || {}),
				transport: "broker",
				delivered: brokerResult.delivered,
				reason: brokerResult.reason,
				id: brokerResult.id || payload.bus?.id,
			};
			return {
				content: [{ type: "text" as const, text: formatAgencyDelegate(payload) }],
				details: payload,
			};
		},
	});

	pi.registerTool({
		name: "agency_report",
		label: "Agency report",
		description: "Specialist-only: report task completion to the Orchestrator (pipeline stages use authenticated durable delivery)",
		promptSnippet: "Report completed agency work to the Orchestrator",
		parameters: Type.Object({
			taskId: Type.Optional(Type.String()),
			status: Type.Optional(StringEnum(["succeeded", "failed", "needs_attention"] as const)),
			summary: Type.Optional(Type.String()),
			output: Type.Optional(Type.String()),
			artifacts: Type.Optional(Type.Record(Type.String(), Type.String())),
			error: Type.Optional(Type.String()),
			question: Type.Optional(Type.String()),
			options: Type.Optional(Type.Array(Type.String())),
			payloadJson: Type.Optional(Type.String()),
		}),
		async execute(_id, params, signal) {
			const payload = buildAgencyReportPayload(params);
			const from = broker.sessionName || "specialist";
			if (params.taskId) {
				const args = [
					"pipeline-report",
					"--from",
					from,
					"--task-id",
					params.taskId,
					"--payload-json",
					JSON.stringify(payload),
				];
				const preflight = await ctl([...args, "--prepare-only"], signal);
				if (preflight.code !== 0) {
					throw new Error(preflight.stderr.trim() || preflight.stdout.trim() || "agency_report pipeline preflight failed");
				}
				const prepared = JSON.parse(preflight.stdout) as { pipelineOwned?: boolean };
				if (prepared.pipelineOwned === true) {
					const sent = await ctl(args, signal);
					if (sent.code !== 0) {
						throw new Error(sent.stderr.trim() || sent.stdout.trim() || "agency_report pipeline delivery failed");
					}
					const result = JSON.parse(sent.stdout) as { bus?: { id?: string } };
					return {
						content: [{ type: "text" as const, text: `Agency pipeline report delivered to orchestrator (${result.bus?.id || "file bus"})` }],
						details: result,
					};
				}
			}
			const message = makeAgencyMessage({
				kind: "report",
				from,
				to: "orchestrator",
				taskId: params.taskId,
				payload,
			});
			const result = await broker.send("orchestrator", message);
			if (!result.delivered) throw new Error(result.reason || "agency_report not delivered");
			return { content: [{ type: "text" as const, text: `Agency report delivered to orchestrator (${result.id})` }], details: result };
		},
	});

	pi.registerTool({
		name: "agency_progress",
		label: "Agency progress",
		description: "Specialist-only: send non-terminal progress to the Orchestrator over the agency broker",
		promptSnippet: "Send agency progress to the Orchestrator",
		parameters: Type.Object({
			taskId: Type.Optional(Type.String()),
			message: Type.String(),
			payloadJson: Type.Optional(Type.String()),
		}),
		async execute(_id, params) {
			const payload = params.payloadJson ? JSON.parse(params.payloadJson) as Record<string, unknown> : { message: params.message };
			const message = makeAgencyMessage({
				kind: "progress",
				from: broker.sessionName || "specialist",
				to: "orchestrator",
				taskId: params.taskId,
				payload,
			});
			const result = await broker.send("orchestrator", message);
			if (!result.delivered) throw new Error(result.reason || "agency_progress not delivered");
			return { content: [{ type: "text" as const, text: `Agency progress delivered to orchestrator (${result.id})` }], details: result };
		},
	});

	pi.registerTool({
		name: "agency_ask",
		label: "Agency ask",
		description: "Specialist-only: ask the Orchestrator a blocking question over the agency broker and wait for a correlated reply",
		promptSnippet: "Ask the Orchestrator a blocking agency question",
		parameters: Type.Object({
			taskId: Type.Optional(Type.String()),
			question: Type.String(),
			timeoutMs: Type.Optional(Type.Number()),
		}),
		async execute(_id, params) {
			const message = makeAgencyMessage({
				kind: "ask",
				from: broker.sessionName || "specialist",
				to: "orchestrator",
				taskId: params.taskId,
				expectsReply: true,
				payload: { message: params.question },
			});
			const reply = await broker.ask("orchestrator", message, params.timeoutMs);
			const body = typeof reply.payload === "object" && reply.payload !== null && "message" in reply.payload
				? String((reply.payload as { message?: unknown }).message)
				: JSON.stringify(reply.payload);
			return { content: [{ type: "text" as const, text: `**Agency reply:**\n${body}` }], details: reply };
		},
	});

	pi.registerTool({
		name: "agency_reply",
		label: "Agency reply",
		description: "Orchestrator-only: reply to a specialist agency_ask using the ask's Reply-To id",
		promptSnippet: "Reply to a specialist agency ask",
		parameters: Type.Object({
			to: Type.String({ description: "Specialist instance name" }),
			replyTo: Type.String({ description: "Ask message id from the inbound agency ask" }),
			message: Type.String(),
			taskId: Type.Optional(Type.String()),
		}),
		async execute(_id, params) {
			const reply = makeAgencyMessage({
				kind: "reply",
				from: broker.sessionName || "orchestrator",
				to: params.to,
				taskId: params.taskId,
				replyTo: params.replyTo,
				payload: { message: params.message },
			});
			const result = await broker.send(params.to, reply);
			if (!result.delivered) throw new Error(result.reason || "agency_reply not delivered");
			return { content: [{ type: "text" as const, text: `Agency reply delivered to ${params.to}` }], details: result };
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
			const payload = JSON.parse(r.stdout) as AgencyReleasePayload;
			return {
				content: [{ type: "text" as const, text: formatAgencyRelease(payload) }],
				details: payload,
			};
		},
	});
}
