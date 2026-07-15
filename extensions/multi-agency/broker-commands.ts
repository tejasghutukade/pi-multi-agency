import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { AgencyBrokerRuntime, AgencyIdentity } from "./broker-runtime.ts";
import type { CtlRunner } from "./lifecycle.ts";

export function formatAgencyBrokerStatus(status: ReturnType<AgencyBrokerRuntime["getBrokerStatus"]>): string {
	return [
		`Agency broker: ${status.connectionState}`,
		`Project root: ${status.projectRoot || "unavailable"}`,
		`Agency root: ${status.agencyRoot || "unavailable"}`,
		`Project key: ${status.projectKey || "unavailable"}`,
		`Endpoint: ${status.endpoint || "unavailable"}`,
		...(!status.projectRoot ? [status.diagnostic] : []),
	].join("\n");
}

export function registerAgencyBrokerCommands(
	pi: Pick<ExtensionAPI, "registerCommand">,
	ctl: CtlRunner,
	broker: AgencyBrokerRuntime,
): void {
	pi.registerCommand("agency-broker-status", {
		description: "Show this pane's read-only project broker context and connection state",
		handler: async (_args, ctx) => {
			const status = broker.getBrokerStatus();
			ctx.ui.notify(formatAgencyBrokerStatus(status), status.connectionState === "unavailable" ? "warning" : "info");
		},
	});
	pi.registerCommand("agency-claim", {
		description: "Claim this cmux surface as the Orchestrator hub",
		handler: async (_args, ctx) => {
			const claimed = await ctl(["claim-orchestrator"]);
			if (claimed.code !== 0) {
				ctx.ui.notify(claimed.stderr.trim() || claimed.stdout.trim() || "claim failed", "error");
				return;
			}
			const refreshed = await ctl(["lifecycle", "whoami"]);
			if (refreshed.code !== 0) {
				ctx.ui.notify(refreshed.stderr.trim() || refreshed.stdout.trim() || "claim identity refresh failed", "error");
				return;
			}
			let identity: AgencyIdentity;
			try { identity = JSON.parse(refreshed.stdout) as AgencyIdentity; } catch {
				ctx.ui.notify("claim identity refresh returned invalid JSON", "error");
				return;
			}
			await broker.ensureConnected(identity, ctx.ui);
			ctx.ui.notify("Orchestrator claimed", "info");
		},
	});
}
