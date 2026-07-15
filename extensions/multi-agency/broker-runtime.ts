import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import { AgencyBrokerClient, type SendResult } from "./broker/client.ts";
import { spawnBrokerIfNeeded } from "./broker/spawn.ts";
import { requireBrokerContext, type AvailableBrokerContext, type BrokerContext } from "./broker/paths.ts";
import type { AgencyMessage, AgencySessionInfo } from "./broker/types.ts";

export type AgencyIdentity = {
	isHub?: boolean;
	isTemporary?: boolean;
	instance?: {
		intercomName?: string;
		role?: string;
		status?: string;
		taskId?: string | null;
		lifecycle?: string;
		cwd?: string;
	} | null;
};

export type AgencyInboundHandler = (from: AgencySessionInfo, message: AgencyMessage) => void | Promise<void>;
export type AgencyTransportId = `agency:${string}:${string}`;

export function buildAgencyTransportId(context: AvailableBrokerContext, logicalName: string): AgencyTransportId {
	return `agency:${context.projectKey}:${logicalName}`;
}

function instanceName(identity: AgencyIdentity | null): string | null {
	return identity?.instance?.intercomName || (identity?.isHub ? "orchestrator" : null);
}

function statusText(ui: ExtensionContext["ui"] | null, text?: string): void {
	ui?.setStatus?.("agency-broker", text);
}

export class AgencyBrokerRuntime {
	private client: AgencyBrokerClient | null = null;
	private connecting: Promise<void> | null = null;
	private identity: AgencyIdentity | null = null;
	private readonly context: BrokerContext;
	private lastUi: ExtensionContext["ui"] | null = null;
	private inboundHandlers = new Set<AgencyInboundHandler>();
	private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

	constructor(context: BrokerContext) {
		this.context = context;
	}

	getBrokerStatus(): { projectRoot: string | null; agencyRoot: string | null; projectKey: string | null; endpoint: string | null; connectionState: "connected" | "connecting" | "disconnected" | "unavailable"; diagnostic: string } {
		return {
			projectRoot: this.context.projectRoot,
			agencyRoot: this.context.agencyRoot,
			projectKey: this.context.projectKey,
			endpoint: this.context.available ? (this.context.useWindowsTcp ? this.context.portFile : this.context.endpoint) : null,
			connectionState: !this.context.available ? "unavailable" : this.isConnected() ? "connected" : this.connecting ? "connecting" : "disconnected",
			diagnostic: this.context.diagnostic,
		};
	}

	get sessionName(): string | null {
		return instanceName(this.identity);
	}

	isConnected(): boolean {
		return Boolean(this.client?.isConnected());
	}

	onMessage(handler: AgencyInboundHandler): () => void {
		this.inboundHandlers.add(handler);
		return () => this.inboundHandlers.delete(handler);
	}

	async ensureConnected(identity: AgencyIdentity | null, ui?: ExtensionContext["ui"] | null): Promise<void> {
		this.identity = identity;
		this.lastUi = ui || this.lastUi;
		const name = instanceName(identity);
		if (!name || !identity?.instance) {
			statusText(this.lastUi, undefined);
			return;
		}
		if (this.client?.isConnected()) {
			this.updatePresence(identity);
			return;
		}
		if (this.connecting) return this.connecting;
		this.connecting = this.connect(identity).finally(() => {
			this.connecting = null;
		});
		return this.connecting;
	}

	private async connect(identity: AgencyIdentity): Promise<void> {
		const name = instanceName(identity);
		if (!name || !identity.instance) return;
		statusText(this.lastUi, "agency broker: connecting…");
		await spawnBrokerIfNeeded(this.context);
		const client = new AgencyBrokerClient(this.context);
		client.on("message", (from: AgencySessionInfo, message: AgencyMessage) => {
			for (const handler of this.inboundHandlers) void handler(from, message);
		});
		client.on("disconnected", () => {
			if (this.client === client) {
				this.client = null;
				statusText(this.lastUi, "agency broker: disconnected — retrying…");
				this.scheduleReconnect();
			}
		});
		client.on("error", (error) => {
			statusText(this.lastUi, `agency broker error: ${error instanceof Error ? error.message : String(error)}`);
		});
		await client.connect(
			{
				name,
				role: identity.instance.role,
				isHub: Boolean(identity.isHub || identity.instance.role === "orchestrator" || name === "orchestrator"),
				cwd: identity.instance.cwd || this.context.projectRoot || process.cwd(),
				model: process.env.PI_MODEL || process.env.MODEL || "unknown",
				pid: process.pid,
				startedAt: Date.now(),
				lastActivity: Date.now(),
				status: identity.instance.status || "idle",
				lifecycle: identity.instance.lifecycle,
				taskId: identity.instance.taskId,
			},
			buildAgencyTransportId(requireBrokerContext(this.context), name),
		);
		this.client = client;
		statusText(this.lastUi, undefined);
	}

	private scheduleReconnect(): void {
		if (this.reconnectTimer) return;
		this.reconnectTimer = setTimeout(() => {
			this.reconnectTimer = null;
			void this.ensureConnected(this.identity, this.lastUi).catch(() => this.scheduleReconnect());
		}, 2000);
	}

	updatePresence(identity: AgencyIdentity | null = this.identity, status?: string): void {
		if (!identity?.instance || !this.client?.isConnected()) return;
		this.client.updatePresence({
			name: instanceName(identity) || undefined,
			role: identity.instance.role,
			status: status || identity.instance.status,
			model: process.env.PI_MODEL || process.env.MODEL || "unknown",
			taskId: identity.instance.taskId,
		});
	}

	async send(to: string, message: AgencyMessage): Promise<SendResult> {
		await this.ensureConnected(this.identity, this.lastUi);
		if (!this.client?.isConnected()) throw new Error("Agency broker is not connected");
		return this.client.send(to, message);
	}

	async ask(to: string, message: AgencyMessage, timeoutMs?: number): Promise<AgencyMessage> {
		await this.ensureConnected(this.identity, this.lastUi);
		if (!this.client?.isConnected()) throw new Error("Agency broker is not connected");
		return this.client.ask(to, message, timeoutMs);
	}

	async disconnect(): Promise<void> {
		if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
		this.reconnectTimer = null;
		const client = this.client;
		this.client = null;
		statusText(this.lastUi, undefined);
		await client?.disconnect();
	}
}
