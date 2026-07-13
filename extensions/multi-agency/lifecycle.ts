/**
 * Pi lifecycle bridge (v0.3) — runs in every agency pane.
 * Process truth: agent_start / agent_settled → sessions.json
 * Task truth: bus report/ask
 * Hub: push full message when idle; queue banner when busy
 * Specialist: silent settle → one nudge → abandon if no agent_start
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

export type CtlRunner = (
	args: string[],
	signal?: AbortSignal,
) => Promise<{ code: number; stdout: string; stderr: string }>;

const SILENT_TICK_MS = 5_000;
const HUB_POLL_MS = 2_000;
const HUB_DELIVER_GRACE_MS = 30_000;

type Whoami = {
	ok?: boolean;
	isHub?: boolean;
	instance?: { intercomName?: string; role?: string; taskId?: string | null } | null;
};

type PendingHub = {
	ok?: boolean;
	count?: number;
	messages?: Array<{ from?: string; taskId?: string; type?: string; path?: string }>;
};

type TickResult = {
	ok?: boolean;
	status?: string;
	taskId?: string;
	instance?: string;
};

type ClaimResult = {
	ok?: boolean;
	empty?: boolean;
	path?: string;
	text?: string;
	envelope?: { from?: string; taskId?: string; type?: string };
};

function parseJson<T>(raw: string): T | null {
	try {
		return JSON.parse(raw) as T;
	} catch {
		return null;
	}
}

export function installLifecycleBridge(pi: ExtensionAPI, runCtl: CtlRunner): void {
	let identity: Whoami | null = null;
	let processBusy = false;
	let settleTimer: ReturnType<typeof setTimeout> | null = null;
	let hubPollTimer: ReturnType<typeof setInterval> | null = null;
	let specialistTickTimer: ReturnType<typeof setInterval> | null = null;
	let delivering = false;
	let lastUi: ExtensionContext["ui"] | null = null;

	const clearSettleTimer = () => {
		if (settleTimer) {
			clearTimeout(settleTimer);
			settleTimer = null;
		}
	};

	const lifecycle = async (args: string[]) => {
		const r = await runCtl(["lifecycle", ...args]);
		if (r.code !== 0) {
			const err = parseJson<{ error?: string }>(r.stderr) || parseJson<{ error?: string }>(r.stdout);
			throw new Error(err?.error || r.stderr.trim() || r.stdout.trim() || "lifecycle failed");
		}
		return parseJson<Record<string, unknown>>(r.stdout) || { ok: true, raw: r.stdout };
	};

	const refreshIdentity = async () => {
		try {
			identity = (await lifecycle(["whoami"])) as Whoami;
		} catch {
			identity = null;
		}
		return identity;
	};

	const setQueueBanner = (ui: ExtensionContext["ui"] | null, pending: PendingHub | null) => {
		if (!ui?.setStatus) return;
		const n = pending?.count || 0;
		if (n <= 0) {
			ui.setStatus("agency-queue", undefined);
			return;
		}
		const from = (pending?.messages || []).map((m) => m.from || "?").slice(0, 3).join(", ");
		ui.setStatus(
			"agency-queue",
			processBusy
				? `Queued ${n} agency message(s) from ${from || "specialist"} — delivers when idle`
				: `Delivering ${n} agency message(s)…`,
		);
	};

	const markStatus = async (status: "working" | "idle" | "interrupted") => {
		const name = identity?.instance?.intercomName;
		const args = ["status", "--status", status];
		if (name) args.push("--name", name);
		try {
			await lifecycle(args);
		} catch {
			/* pane may not be claimed yet */
		}
	};

	const drainHubQueue = async () => {
		if (delivering || processBusy) return;
		delivering = true;
		try {
			const claim = (await lifecycle(["claim-delivery"])) as ClaimResult;
			if (!claim || claim.empty || !claim.text || !claim.path) {
				const pending = (await lifecycle(["pending-hub"])) as PendingHub;
				setQueueBanner(lastUi, pending);
				return;
			}
			try {
				pi.sendUserMessage(claim.text, { deliverAs: "followUp" });
				await lifecycle(["ack-delivery", "--path", claim.path]);
			} catch (e) {
				lastUi?.notify?.(`agency delivery failed: ${String(e)}`, "error");
			}
			const pending = (await lifecycle(["pending-hub"])) as PendingHub;
			setQueueBanner(lastUi, pending);
		} catch {
			/* ignore transient */
		} finally {
			delivering = false;
		}
	};

	const pollHubPending = async () => {
		if (!identity?.isHub) return;
		try {
			const pending = (await lifecycle(["pending-hub"])) as PendingHub;
			setQueueBanner(lastUi, pending);
			if (!processBusy && (pending.count || 0) > 0 && !settleTimer) {
				settleTimer = setTimeout(() => {
					settleTimer = null;
					void drainHubQueue();
				}, HUB_DELIVER_GRACE_MS);
			}
		} catch {
			/* ignore */
		}
	};

	const specialistTick = async () => {
		if (identity?.isHub) return;
		if (processBusy) return;
		const name = identity?.instance?.intercomName;
		if (!name) return;
		try {
			const tick = (await lifecycle(["tick", "--name", name])) as TickResult;
			if (tick.status === "abandon") {
				await lifecycle([
					"abandon",
					"--name",
					name,
					"--reason",
					"no-agent_start-after-nudge",
				]);
				lastUi?.notify?.(
					`Abandoned ${name} after silent settle; respawned + re-delegated`,
					"warning",
				);
			} else if (tick.status === "nudged") {
				lastUi?.notify?.(`Silent-settle nudge sent to ${name}`, "info");
			}
		} catch {
			/* ignore */
		}
	};

	const startLoops = async () => {
		await refreshIdentity();
		if (identity?.isHub) {
			if (!hubPollTimer) {
				hubPollTimer = setInterval(() => void pollHubPending(), HUB_POLL_MS);
			}
		} else if (identity?.instance) {
			if (!specialistTickTimer) {
				specialistTickTimer = setInterval(() => void specialistTick(), SILENT_TICK_MS);
			}
		}
	};

	pi.on("session_start", async (_event, ctx) => {
		lastUi = ctx.ui;
		await startLoops();
	});

	pi.on("session_shutdown", async () => {
		clearSettleTimer();
		if (hubPollTimer) clearInterval(hubPollTimer);
		if (specialistTickTimer) clearInterval(specialistTickTimer);
		hubPollTimer = null;
		specialistTickTimer = null;
		lastUi?.setStatus?.("agency-queue", undefined);
	});

	pi.on("agent_start", async (_event, ctx) => {
		lastUi = ctx.ui;
		processBusy = true;
		clearSettleTimer();
		if (!identity?.instance) await refreshIdentity();
		await markStatus("working");
		if (identity?.isHub) void pollHubPending();
	});

	pi.on("agent_settled", async (_event, ctx) => {
		lastUi = ctx.ui;
		processBusy = false;
		if (!identity?.instance) await refreshIdentity();
		await markStatus("idle");
		if (identity?.isHub) {
			clearSettleTimer();
			settleTimer = setTimeout(() => {
				settleTimer = null;
				void drainHubQueue();
			}, HUB_DELIVER_GRACE_MS);
			void pollHubPending();
		} else {
			void specialistTick();
		}
	});
}
