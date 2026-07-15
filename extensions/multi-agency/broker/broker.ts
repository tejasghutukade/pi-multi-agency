import net from "node:net";
import { randomUUID } from "node:crypto";
import { unlinkSync } from "node:fs";
import { createMessageReader, writeMessage } from "./framing.ts";
import {
	AGENCY_BROKER_PROTOCOL_NAME,
	AGENCY_BROKER_PROTOCOL_VERSION,
	ensureAgencyBrokerRuntimeDir,
	ensureAgencyBrokerSocketDir,
	getBrokerListenTarget,
	requireBrokerContext,
	resolveBrokerContext,
	restrictAgencyBrokerRuntimeFile,
	writeAgencyBrokerRuntimeFile,
	type BrokerConnectTarget,
} from "./paths.ts";
import type { AgencySessionInfo, AgencySessionRegistration, BrokerMessage } from "./types.ts";
import { isBrokerMessageEnvelope } from "../messages.ts";

// Adapted from pi-intercom (MIT, Copyright (c) 2026 Nico Bailon).
const BROKER_CONTEXT = requireBrokerContext(resolveBrokerContext());
const BROKER_DIR = BROKER_CONTEXT.brokerDir;
const LISTEN_TARGET = getBrokerListenTarget(BROKER_CONTEXT);
const PID_PATH = BROKER_CONTEXT.pidFile;
const PORT_PATH = BROKER_CONTEXT.portFile;
const BROKER_STATE_ID = randomUUID();
const MAX_SESSIONS = 128;
const MAX_UNREGISTERED_CONNECTIONS = 32;
const REGISTRATION_TIMEOUT_MS = 1000;
const RATE_LIMIT_CAPACITY = 240;
const RATE_LIMIT_REFILL_PER_SECOND = 120;
const PRESENCE_HEARTBEAT_MS = 1000;
const ASK_TIMEOUT_MS = Number.parseInt(process.env.AGENCY_BROKER_ASK_TIMEOUT_MS || "600000", 10);
const HUB = "orchestrator";

interface ConnectedSession {
	socket: net.Socket;
	info: AgencySessionInfo;
	lastPresenceBroadcastAt: number;
}

interface ConnectionState {
	socket: net.Socket;
	tokens: number;
	lastRefillAt: number;
}

interface AskEdge {
	from: string;
	to: string;
	createdAt: number;
}

function isSessionRegistration(value: unknown): value is AgencySessionRegistration {
	if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
	const session = value as Record<string, unknown>;
	if (typeof session.cwd !== "string" || typeof session.model !== "string" || typeof session.pid !== "number" || typeof session.startedAt !== "number" || typeof session.lastActivity !== "number") return false;
	if (session.name !== undefined && typeof session.name !== "string") return false;
	if (session.role !== undefined && typeof session.role !== "string") return false;
	if (session.isHub !== undefined && typeof session.isHub !== "boolean") return false;
	if (session.status !== undefined && typeof session.status !== "string") return false;
	if (session.lifecycle !== undefined && typeof session.lifecycle !== "string") return false;
	return session.taskId === undefined || session.taskId === null || typeof session.taskId === "string";
}

function isSessionInfo(value: unknown): value is AgencySessionInfo {
	if (!isSessionRegistration(value)) return false;
	return typeof (value as Record<string, unknown>).id === "string";
}

function isSessionId(value: unknown): value is string {
	return typeof value === "string" && value.trim().length > 0;
}

function displayName(session: AgencySessionInfo): string {
	return session.name || session.id;
}

function aclAllows(from: AgencySessionInfo, to: AgencySessionInfo): boolean {
	if (from.isHub || to.isHub || from.role === HUB || to.role === HUB || displayName(from) === HUB || displayName(to) === HUB) return true;
	return process.env.AGENCY_BROKER_ALLOW_PEERS === "1" || process.env.AGENCY_BROKER_ALLOW_PEERS === "true";
}

class AgencyBroker {
	private sessions = new Map<string, ConnectedSession>();
	private askEdges = new Map<string, AskEdge>();
	private connections = new Set<net.Socket>();
	private unregisteredConnections = new Set<net.Socket>();
	private server: net.Server;
	private shutdownTimer: NodeJS.Timeout | null = null;
	private readonly askTimeoutMs = Number.isFinite(ASK_TIMEOUT_MS) ? ASK_TIMEOUT_MS : 600_000;

	constructor() {
		ensureAgencyBrokerRuntimeDir(BROKER_DIR, BROKER_CONTEXT.platform, BROKER_CONTEXT.agencyRoot);
		ensureAgencyBrokerSocketDir(BROKER_CONTEXT);
		if (typeof LISTEN_TARGET === "string" && BROKER_CONTEXT.platform !== "win32") {
			try { unlinkSync(LISTEN_TARGET); } catch { /* clean startup */ }
		}
		this.server = net.createServer(this.handleConnection.bind(this));
	}

	start(): void {
		const onListening = () => {
			if (typeof LISTEN_TARGET === "string") {
				restrictAgencyBrokerRuntimeFile(LISTEN_TARGET, BROKER_CONTEXT.platform);
			} else {
				const address = this.server.address();
				if (!address || typeof address === "string") throw new Error("Agency broker started without TCP address");
				const endpoint: BrokerConnectTarget = { transport: "tcp", host: LISTEN_TARGET.host, port: address.port, stateId: BROKER_STATE_ID };
				writeAgencyBrokerRuntimeFile(PORT_PATH, `${JSON.stringify(endpoint)}\n`, BROKER_CONTEXT.platform);
			}
			writeAgencyBrokerRuntimeFile(PID_PATH, String(process.pid), BROKER_CONTEXT.platform);
			console.log(`Agency broker started (pid: ${process.pid})`);
		};
		if (typeof LISTEN_TARGET === "string") this.server.listen(LISTEN_TARGET, onListening);
		else this.server.listen({ host: LISTEN_TARGET.host, port: LISTEN_TARGET.port }, onListening);
		process.on("SIGTERM", () => this.shutdown());
		process.on("SIGINT", () => this.shutdown());
	}

	private handleConnection(socket: net.Socket): void {
		this.connections.add(socket);
		let sessionId: string | null = null;
		let registrationTimeout: NodeJS.Timeout | null = null;
		const armRegistrationTimeout = () => {
			if (registrationTimeout) clearTimeout(registrationTimeout);
			this.unregisteredConnections.delete(socket);
			this.unregisteredConnections.add(socket);
			this.evictOldestUnregisteredConnections(socket);
			registrationTimeout = setTimeout(() => { if (!sessionId) socket.destroy(); }, REGISTRATION_TIMEOUT_MS);
			registrationTimeout.unref?.();
		};
		const clearRegistrationTimeout = () => {
			if (registrationTimeout) clearTimeout(registrationTimeout);
			registrationTimeout = null;
			this.unregisteredConnections.delete(socket);
		};
		armRegistrationTimeout();
		const connection: ConnectionState = { socket, tokens: RATE_LIMIT_CAPACITY, lastRefillAt: Date.now() };
		const reader = createMessageReader((msg) => {
			if (!this.consumeToken(connection)) {
				writeMessage(socket, { type: "error", error: "Agency broker rate limit exceeded" });
				socket.destroy(new Error("Agency broker rate limit exceeded"));
				return;
			}
			this.handleMessage(socket, msg, sessionId, (id) => {
				sessionId = id;
				if (id) clearRegistrationTimeout(); else armRegistrationTimeout();
			});
		}, (error) => socket.destroy(error));
		socket.on("data", reader);
		socket.on("close", () => {
			clearRegistrationTimeout();
			this.connections.delete(socket);
			if (sessionId) {
				const existing = this.sessions.get(sessionId);
				if (existing?.socket === socket) {
					this.sessions.delete(sessionId);
					this.clearAskEdgesForSession(sessionId);
					this.broadcast({ type: "session_left", sessionId }, sessionId);
					this.scheduleShutdownCheck();
				}
			}
		});
		socket.on("error", (error) => console.error("Agency broker socket error:", error));
	}

	private evictOldestUnregisteredConnections(currentSocket: net.Socket): void {
		while (this.unregisteredConnections.size > MAX_UNREGISTERED_CONNECTIONS) {
			const [oldest] = this.unregisteredConnections;
			if (!oldest || (oldest === currentSocket && this.unregisteredConnections.size === 1)) return;
			this.unregisteredConnections.delete(oldest);
			oldest.destroy();
		}
	}

	private consumeToken(connection: ConnectionState, now = Date.now()): boolean {
		const elapsedMs = now - connection.lastRefillAt;
		if (elapsedMs > 0) {
			connection.tokens = Math.min(RATE_LIMIT_CAPACITY, connection.tokens + elapsedMs * RATE_LIMIT_REFILL_PER_SECOND / 1000);
			connection.lastRefillAt = now;
		}
		if (connection.tokens < 1) return false;
		connection.tokens -= 1;
		return true;
	}

	private scheduleShutdownCheck(): void {
		if (this.shutdownTimer) return;
		this.shutdownTimer = setTimeout(() => {
			this.shutdownTimer = null;
			if (this.sessions.size === 0) this.shutdown();
		}, 5000);
	}

	private handleMessage(socket: net.Socket, msg: unknown, currentId: string | null, setId: (id: string | null) => void): void {
		if (typeof msg !== "object" || msg === null || !("type" in msg) || typeof msg.type !== "string") throw new Error("Invalid agency broker client message");
		const clientMessage = msg as { type: string } & Record<string, unknown>;
		const requiresEndpointAuth = typeof LISTEN_TARGET !== "string";
		const hasEndpointAuth = clientMessage.stateId === BROKER_STATE_ID;

		if (clientMessage.type === "health") {
			if (typeof clientMessage.requestId !== "string") throw new Error("Invalid health message");
			if (requiresEndpointAuth && !hasEndpointAuth) throw new Error("Invalid agency broker TCP credentials");
			writeMessage(socket, { type: "health_ok", requestId: clientMessage.requestId, protocol: AGENCY_BROKER_PROTOCOL_NAME, version: AGENCY_BROKER_PROTOCOL_VERSION });
			return;
		}
		if (requiresEndpointAuth && clientMessage.type === "register" && !hasEndpointAuth) throw new Error("Invalid agency broker TCP credentials");
		if (currentId === null && clientMessage.type !== "register") throw new Error(`Received ${clientMessage.type} before register`);

		switch (clientMessage.type) {
			case "register": {
				if (!isSessionRegistration(clientMessage.session)) throw new Error("Invalid register message");
				if (currentId) throw new Error("Received duplicate register message");
				let id: string = randomUUID();
				if (clientMessage.sessionId !== undefined) {
					if (!isSessionId(clientMessage.sessionId)) throw new Error("Invalid register sessionId");
					id = clientMessage.sessionId;
				}
				const previous = this.sessions.get(id);
				if (!previous && this.sessions.size >= MAX_SESSIONS) {
					writeMessage(socket, { type: "error", error: "Too many registered agency sessions" });
					socket.destroy();
					break;
				}
				if (previous) {
					this.clearAskEdgesForSession(id);
					previous.socket.end();
				}
				setId(id);
				const session = clientMessage.session;
				const info: AgencySessionInfo = {
					id,
					...(session.name !== undefined ? { name: session.name } : {}),
					...(session.role !== undefined ? { role: session.role } : {}),
					...(session.isHub !== undefined ? { isHub: session.isHub } : {}),
					cwd: session.cwd,
					model: session.model,
					pid: session.pid,
					startedAt: session.startedAt,
					lastActivity: session.lastActivity,
					...(session.status !== undefined ? { status: session.status } : {}),
					...(session.lifecycle !== undefined ? { lifecycle: session.lifecycle } : {}),
					...(session.taskId !== undefined ? { taskId: session.taskId } : {}),
					trustedLocal: typeof LISTEN_TARGET === "string" && process.platform !== "win32",
				};
				this.sessions.set(id, { socket, info, lastPresenceBroadcastAt: Date.now() });
				if (this.shutdownTimer) { clearTimeout(this.shutdownTimer); this.shutdownTimer = null; }
				writeMessage(socket, { type: "registered", sessionId: id });
				this.broadcast({ type: "session_joined", session: info }, id);
				break;
			}
			case "unregister": {
				if (!currentId) throw new Error("Received unregister before register");
				const existing = this.sessions.get(currentId);
				if (existing?.socket === socket) {
					this.sessions.delete(currentId);
					this.clearAskEdgesForSession(currentId);
					this.broadcast({ type: "session_left", sessionId: currentId }, currentId);
					this.scheduleShutdownCheck();
				}
				setId(null);
				break;
			}
			case "list": {
				if (typeof clientMessage.requestId !== "string") throw new Error("Invalid list message");
				writeMessage(socket, { type: "sessions", requestId: clientMessage.requestId, sessions: Array.from(this.sessions.values()).map((s) => s.info) });
				break;
			}
			case "send": {
				if (!currentId) throw new Error("Received send before register");
				const message = clientMessage.message;
				const messageId = isBrokerMessageEnvelope(message) ? message.id : "unknown";
				if (typeof clientMessage.to !== "string" || !isBrokerMessageEnvelope(message)) {
					writeMessage(socket, { type: "delivery_failed", messageId, reason: "Invalid message format" });
					break;
				}
				this.pruneAskEdges();
				const replyEdge = message.replyTo ? this.askEdges.get(message.replyTo) : undefined;
				const fromSession = this.sessions.get(currentId);
				if (!fromSession || fromSession.socket !== socket) {
					writeMessage(socket, { type: "delivery_failed", messageId: message.id, reason: "Sender session not found" });
					break;
				}
				if (message.agency.from !== displayName(fromSession.info)) {
					writeMessage(socket, { type: "delivery_failed", messageId: message.id, reason: "Message sender does not match registered agency session" });
					break;
				}
				const targets = this.findSessions(clientMessage.to);
				if (targets.length === 1) {
					const target = targets[0];
					if (message.replyTo && !replyEdge) {
						writeMessage(socket, { type: "delivery_failed", messageId: message.id, reason: "Reply target does not match a pending ask" });
						break;
					}
					if (replyEdge && (replyEdge.to !== currentId || replyEdge.from !== target.info.id)) {
						writeMessage(socket, { type: "delivery_failed", messageId: message.id, reason: "Reply target does not match the pending ask" });
						break;
					}
					if (!aclAllows(fromSession.info, target.info)) {
						writeMessage(socket, { type: "delivery_failed", messageId: message.id, reason: "ACL denied by hub-only agency policy" });
						break;
					}
					if (message.expectsReply) {
						const reverseEdge = Array.from(this.askEdges.entries()).find(([edgeMessageId, edge]) => edgeMessageId !== message.replyTo && edge.from === target.info.id && edge.to === currentId);
						if (reverseEdge) {
							writeMessage(socket, { type: "delivery_failed", messageId: message.id, reason: "Mutual ask refused: target session is already waiting for a reply from this session." });
							break;
						}
						this.askEdges.set(message.id, { from: currentId, to: target.info.id, createdAt: Date.now() });
					}
					writeMessage(target.socket, { type: "message", from: fromSession.info, message });
					if (message.replyTo) this.askEdges.delete(message.replyTo);
					writeMessage(socket, { type: "delivered", messageId: message.id });
					break;
				}
				if (targets.length > 1) {
					writeMessage(socket, { type: "delivery_failed", messageId: message.id, reason: `Multiple sessions named "${clientMessage.to}" are connected. Use the session ID instead.` });
					break;
				}
				writeMessage(socket, { type: "delivery_failed", messageId: message.id, reason: "Session not found" });
				break;
			}
			case "cancel_ask": {
				if (!currentId) throw new Error("Received cancel_ask before register");
				if (typeof clientMessage.messageId !== "string") throw new Error("Invalid cancel_ask message");
				const edge = this.askEdges.get(clientMessage.messageId);
				if (edge?.from === currentId) this.askEdges.delete(clientMessage.messageId);
				break;
			}
			case "presence": {
				if (!currentId) throw new Error("Received presence before register");
				const session = this.sessions.get(currentId);
				if (session?.socket === socket) {
					let changed = false;
					for (const key of ["name", "role", "status", "model"] as const) {
						if (clientMessage[key] !== undefined) {
							if (typeof clientMessage[key] !== "string") throw new Error(`Invalid presence ${key}`);
							if (session.info[key] !== clientMessage[key]) { (session.info as unknown as Record<string, unknown>)[key] = clientMessage[key]; changed = true; }
						}
					}
					if (clientMessage.taskId !== undefined) {
						if (clientMessage.taskId !== null && typeof clientMessage.taskId !== "string") throw new Error("Invalid presence taskId");
						if (session.info.taskId !== clientMessage.taskId) { session.info.taskId = clientMessage.taskId as string | null; changed = true; }
					}
					const now = Date.now();
					session.info.lastActivity = now;
					if (changed || now - session.lastPresenceBroadcastAt >= PRESENCE_HEARTBEAT_MS) {
						session.lastPresenceBroadcastAt = now;
						this.broadcast({ type: "presence_update", session: session.info }, currentId);
					}
				}
				break;
			}
			default:
				throw new Error(`Unknown agency broker client message type: ${clientMessage.type}`);
		}
	}

	private pruneAskEdges(now = Date.now()): void {
		for (const [messageId, edge] of this.askEdges) if (now - edge.createdAt > this.askTimeoutMs) this.askEdges.delete(messageId);
	}

	private clearAskEdgesForSession(sessionId: string): void {
		for (const [messageId, edge] of this.askEdges) if (edge.from === sessionId || edge.to === sessionId) this.askEdges.delete(messageId);
	}

	private findSessions(nameOrId: string): ConnectedSession[] {
		const byId = this.sessions.get(nameOrId);
		if (byId) return [byId];
		const lowerName = nameOrId.toLowerCase();
		const byName = Array.from(this.sessions.values()).filter((session) => session.info.name?.toLowerCase() === lowerName);
		if (byName.length > 0) return byName;
		return Array.from(this.sessions.entries()).filter(([id]) => id.startsWith(nameOrId)).map(([, session]) => session);
	}

	private broadcast(msg: BrokerMessage, exclude?: string): void {
		for (const [id, session] of this.sessions) if (id !== exclude) writeMessage(session.socket, msg);
	}

	private shutdown(): void {
		for (const session of this.sessions.values()) session.socket.end();
		this.sessions.clear();
		this.askEdges.clear();
		if (typeof LISTEN_TARGET === "string" && BROKER_CONTEXT.platform !== "win32") { try { unlinkSync(LISTEN_TARGET); } catch { /* ignore */ } }
		try { unlinkSync(PORT_PATH); } catch { /* ignore */ }
		try { unlinkSync(PID_PATH); } catch { /* ignore */ }
		this.server.close();
		process.exit(0);
	}
}

new AgencyBroker().start();
