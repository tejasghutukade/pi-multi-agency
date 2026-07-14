/**
 * Pi lifecycle bridge (v0.3) — runs in every agency pane.
 * Process truth: agent_start / agent_settled → sessions.json
 * Task truth: bus report/ask
 * Hub: push full message when idle; queue banner when busy
 * Specialist: auto-pull delegate/reply from bus + silent settle guard (nudge → abandon)
 * Temporary: agent_settled starts 5m idle timer; agent_start cancels; idle → auto-teardown
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { AgencyBrokerRuntime } from "./broker-runtime.ts";
import type { AgencyMessage, AgencySessionInfo } from "./broker/types.ts";
import { formatInboundAgencyMessage } from "./messages.ts";

export type CtlRunner = (
	args: string[],
	signal?: AbortSignal,
) => Promise<{ code: number; stdout: string; stderr: string }>;

const SILENT_TICK_MS = 5_000;
const HUB_POLL_MS = 2_000;
const HUB_DELIVER_GRACE_MS = 30_000;
const TEMP_IDLE_TEARDOWN_MS = 5 * 60 * 1000;

type Whoami = {
	ok?: boolean;
	isHub?: boolean;
	isTemporary?: boolean;
	instance?: {
		intercomName?: string;
		role?: string;
		taskId?: string | null;
		lifecycle?: string;
	} | null;
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
	blocked?: string;
	replay?: boolean;
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

export function installLifecycleBridge(pi: ExtensionAPI, runCtl: CtlRunner, broker?: AgencyBrokerRuntime): void {
	let identity: Whoami | null = null;
	let processBusy = false;
	let settleTimer: ReturnType<typeof setTimeout> | null = null;
	let tempIdleTimer: ReturnType<typeof setTimeout> | null = null;
	let hubPollTimer: ReturnType<typeof setInterval> | null = null;
	let specialistTickTimer: ReturnType<typeof setInterval> | null = null;
	let delivering = false;
	let tearingDown = false;
	let lastSpecialistDeliveryPath: string | null = null;
	let lastUi: ExtensionContext["ui"] | null = null;
	const brokerQueue: Array<{ from: AgencySessionInfo; message: AgencyMessage }> = [];

	const clearSettleTimer = () => {
		if (settleTimer) {
			clearTimeout(settleTimer);
			settleTimer = null;
		}
	};

	const clearTempIdleTimer = () => {
		if (tempIdleTimer) {
			clearTimeout(tempIdleTimer);
			tempIdleTimer = null;
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

	const isTemporary = () =>
		Boolean(
			identity?.isTemporary ||
				identity?.instance?.lifecycle === "temporary",
		);

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

	const setTempIdleBanner = (active: boolean) => {
		if (!lastUi?.setStatus) return;
		if (!active || !isTemporary()) {
			lastUi.setStatus("agency-temp-idle", undefined);
			return;
		}
		const name = identity?.instance?.intercomName || "specialist";
		lastUi.setStatus(
			"agency-temp-idle",
			`Temporary ${name}: idle — auto-close in 5m if no agent_start`,
		);
	};

	const markStatus = async (status: "working" | "idle" | "interrupted") => {
		const name = identity?.instance?.intercomName;
		const args = ["status", "--status", status];
		if (name) args.push("--name", name);
		try {
			const out = (await lifecycle(args)) as Whoami;
			if (out?.instance) identity = { ...(identity || {}), instance: out.instance };
			broker?.updatePresence(identity, status);
		} catch {
			/* pane may not be claimed yet */
			broker?.updatePresence(identity, status);
		}
	};

	const runTempIdleTeardown = async () => {
		if (tearingDown || identity?.isHub || !isTemporary()) return;
		tearingDown = true;
		clearTempIdleTimer();
		setTempIdleBanner(false);
		const name = identity?.instance?.intercomName;
		try {
			const args = ["idle-teardown", "--reason", "temp-idle-timeout"];
			if (name) args.push("--name", name);
			await lifecycle(args);
			lastUi?.notify?.(
				`Temporary ${name || "specialist"} closed after 5m idle`,
				"info",
			);
		} catch (e) {
			tearingDown = false;
			lastUi?.notify?.(`temp idle-teardown failed: ${String(e)}`, "error");
		}
	};

	const armTempIdleTimer = () => {
		if (identity?.isHub || !isTemporary() || tearingDown) {
			clearTempIdleTimer();
			setTempIdleBanner(false);
			return;
		}
		clearTempIdleTimer();
		setTempIdleBanner(true);
		tempIdleTimer = setTimeout(() => {
			tempIdleTimer = null;
			void runTempIdleTeardown();
		}, TEMP_IDLE_TEARDOWN_MS);
	};

	const setBrokerBanner = () => {
		if (!lastUi?.setStatus) return;
		if (!brokerQueue.length) {
			lastUi.setStatus("agency-broker-queue", undefined);
			return;
		}
		const from = brokerQueue.map((m) => m.message.from || m.from.name || "?").slice(0, 3).join(", ");
		lastUi.setStatus(
			"agency-broker-queue",
			processBusy
				? `Queued ${brokerQueue.length} broker message(s) from ${from || "agency"} — delivers when idle`
				: `Delivering ${brokerQueue.length} broker message(s)…`,
		);
	};

	const ackBrokerInbound = async (message: AgencyMessage) => {
		if (!identity?.isHub) return;
		if (message.kind !== "report" && message.kind !== "ask") return;
		try {
			const args = ["broker-ack", "--from", message.from, "--type", message.kind];
			if (message.taskId) args.push("--task-id", message.taskId);
			await lifecycle(args);
		} catch {
			/* ack is best-effort; live delivery already reached the hub */
		}
	};

	const deliverBrokerMessage = async (entry: { from: AgencySessionInfo; message: AgencyMessage }) => {
		const text = formatInboundAgencyMessage(entry.message);
		try {
			pi.sendUserMessage(text, { deliverAs: "followUp" });
			await ackBrokerInbound(entry.message);
		} catch (e) {
			lastUi?.notify?.(`agency broker delivery failed: ${String(e)}`, "error");
		}
	};

	const drainBrokerQueue = async () => {
		if (processBusy || delivering) return;
		while (!processBusy && brokerQueue.length) {
			const entry = brokerQueue.shift();
			if (entry) await deliverBrokerMessage(entry);
		}
		setBrokerBanner();
	};

	const handleBrokerInbound = async (from: AgencySessionInfo, message: AgencyMessage) => {
		if (processBusy) {
			brokerQueue.push({ from, message });
			setBrokerBanner();
			return;
		}
		await deliverBrokerMessage({ from, message });
	};

	broker?.onMessage((from, message) => void handleBrokerInbound(from, message));

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
		if (processBusy || tearingDown) return;
		const name = identity?.instance?.intercomName;
		if (!name) return;

		try {
			const tick = (await lifecycle(["tick", "--name", name])) as TickResult;
			if (tick.status === "abandon") {
				clearTempIdleTimer();
				setTempIdleBanner(false);
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
				return;
			}
			if (tick.status === "nudged") {
				lastUi?.notify?.(`Silent-settle nudge sent to ${name}`, "info");
			}
		} catch {
			/* ignore */
		}

		try {
			const claim = (await lifecycle(["claim-specialist", "--name", name])) as ClaimResult;
			if (!claim || claim.empty || !claim.text || !claim.path) {
				if (!claim?.path) lastSpecialistDeliveryPath = null;
				return;
			}
			if (claim.path === lastSpecialistDeliveryPath) return;
			lastSpecialistDeliveryPath = claim.path;
			pi.sendUserMessage(claim.text, { deliverAs: "followUp" });
		} catch {
			/* ignore transient */
		}
	};

	const startLoops = async () => {
		await refreshIdentity();
		void broker?.ensureConnected(identity, lastUi).catch((e) => {
			lastUi?.setStatus?.("agency-broker", `agency broker unavailable — file fallback active: ${String(e)}`);
		});
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
		clearTempIdleTimer();
		if (hubPollTimer) clearInterval(hubPollTimer);
		if (specialistTickTimer) clearInterval(specialistTickTimer);
		hubPollTimer = null;
		specialistTickTimer = null;
		lastSpecialistDeliveryPath = null;
		brokerQueue.splice(0, brokerQueue.length);
		void broker?.disconnect();
		lastUi?.setStatus?.("agency-queue", undefined);
		lastUi?.setStatus?.("agency-broker", undefined);
		lastUi?.setStatus?.("agency-broker-queue", undefined);
		lastUi?.setStatus?.("agency-temp-idle", undefined);
	});

	pi.on("agent_start", async (_event, ctx) => {
		lastUi = ctx.ui;
		processBusy = true;
		clearSettleTimer();
		clearTempIdleTimer();
		setTempIdleBanner(false);
		if (!identity?.instance) await refreshIdentity();
		await markStatus("working");
		void broker?.ensureConnected(identity, lastUi).catch(() => undefined);
		if (identity?.isHub) void pollHubPending();
	});

	pi.on("agent_settled", async (_event, ctx) => {
		lastUi = ctx.ui;
		processBusy = false;
		if (!identity?.instance) await refreshIdentity();
		await markStatus("idle");
		void broker?.ensureConnected(identity, lastUi).catch(() => undefined);
		if (brokerQueue.length) void drainBrokerQueue();
		if (identity?.isHub) {
			clearSettleTimer();
			settleTimer = setTimeout(() => {
				settleTimer = null;
				void drainHubQueue();
			}, HUB_DELIVER_GRACE_MS);
			void pollHubPending();
		} else {
			armTempIdleTimer();
			void specialistTick();
		}
	});
}
