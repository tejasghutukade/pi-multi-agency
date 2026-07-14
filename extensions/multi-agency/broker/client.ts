import { EventEmitter } from "node:events";
import net from "node:net";
import { randomUUID } from "node:crypto";
import { createMessageReader, writeMessage } from "./framing.ts";
import { getBrokerConnectTarget, type BrokerConnectTarget } from "./paths.ts";
import type { AgencyMessage, AgencySessionInfo, AgencySessionRegistration, BrokerMessageEnvelope } from "./types.ts";
import { isAgencyMessage, isBrokerMessageEnvelope, toBrokerEnvelope } from "../messages.ts";

// Adapted from pi-intercom (MIT, Copyright (c) 2026 Nico Bailon).
export interface SendResult {
	id: string;
	delivered: boolean;
	reason?: string;
}

function toError(error: unknown): Error {
	return error instanceof Error ? error : new Error(String(error));
}

function connectToBrokerTarget(target: BrokerConnectTarget): net.Socket {
	return typeof target === "string" ? net.connect(target) : net.connect({ host: target.host, port: target.port });
}

function isSessionInfo(value: unknown): value is AgencySessionInfo {
	if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
	const session = value as Record<string, unknown>;
	if (typeof session.id !== "string" || typeof session.cwd !== "string" || typeof session.model !== "string" || typeof session.pid !== "number" || typeof session.startedAt !== "number" || typeof session.lastActivity !== "number") return false;
	if (session.name !== undefined && typeof session.name !== "string") return false;
	if (session.role !== undefined && typeof session.role !== "string") return false;
	if (session.isHub !== undefined && typeof session.isHub !== "boolean") return false;
	if (session.status !== undefined && typeof session.status !== "string") return false;
	return session.taskId === undefined || session.taskId === null || typeof session.taskId === "string";
}

export class AgencyBrokerClient extends EventEmitter {
	private socket: net.Socket | null = null;
	private _sessionId: string | null = null;
	private pendingSends = new Map<string, { resolve: (r: SendResult) => void; reject: (e: Error) => void }>();
	private pendingLists = new Map<string, { resolve: (sessions: AgencySessionInfo[]) => void; reject: (e: Error) => void }>();
	private pendingAsks = new Map<string, { resolve: (m: AgencyMessage) => void; reject: (e: Error) => void; timeout: ReturnType<typeof setTimeout> }>();
	private disconnecting = false;
	private disconnectError: Error | null = null;

	get sessionId(): string | null { return this._sessionId; }

	isConnected(): boolean {
		const socket = this.socket;
		return Boolean(socket && this._sessionId && !this.disconnecting && !socket.destroyed && !socket.writableEnded && socket.writable);
	}

	private failPending(error: Error): void {
		for (const pending of this.pendingSends.values()) pending.reject(error);
		this.pendingSends.clear();
		for (const pending of this.pendingLists.values()) pending.reject(error);
		this.pendingLists.clear();
		for (const pending of this.pendingAsks.values()) {
			clearTimeout(pending.timeout);
			pending.reject(error);
		}
		this.pendingAsks.clear();
	}

	private requireActiveSocket(): net.Socket {
		if (this.disconnecting) throw new Error("Agency broker client disconnecting");
		const socket = this.socket;
		if (!socket || !this._sessionId) throw new Error("Agency broker not connected");
		if (socket.destroyed || socket.writableEnded || !socket.writable) throw new Error("Agency broker client disconnected");
		return socket;
	}

	connect(session: AgencySessionRegistration, sessionId?: string): Promise<void> {
		if (this.socket) return Promise.reject(new Error("Already connected"));
		return new Promise((resolve, reject) => {
			let socket: net.Socket;
			let target: BrokerConnectTarget;
			try {
				target = getBrokerConnectTarget();
				socket = connectToBrokerTarget(target);
			} catch (error) {
				reject(toError(error));
				return;
			}
			this.socket = socket;
			this.disconnectError = null;
			let settled = false;
			let connectionEstablished = false;
			const timeout = setTimeout(() => {
				if (!this._sessionId) {
					cleanupConnectionAttempt(); cleanupSocketListeners();
					if (this.socket === socket) this.socket = null;
					socket.destroy(); reject(new Error("Agency broker connection timeout"));
				}
			}, 10_000);
			const onRegistered = () => { settled = true; connectionEstablished = true; cleanupConnectionAttempt(); resolve(); };
			const onError = (err: Error) => {
				settled = true; cleanupConnectionAttempt(); cleanupSocketListeners();
				if (this.socket === socket) this.socket = null;
				socket.destroy(); reject(err);
			};
			const onClose = () => {
				const wasConnecting = !settled && !this._sessionId;
				const wasDisconnecting = this.disconnecting;
				const disconnectError = this.disconnectError ?? new Error("Agency broker client disconnected");
				this.disconnecting = false; cleanupConnectionAttempt(); cleanupSocketListeners(); this.failPending(disconnectError);
				if (this.socket === socket) this.socket = null;
				this._sessionId = null; this.disconnectError = null;
				if (connectionEstablished && !wasDisconnecting) this.emit("disconnected", disconnectError);
				if (wasConnecting) reject(new Error("Connection closed before registration"));
			};
			const onSocketError = (err: Error) => {
				if (connectionEstablished) { this.disconnectError = err; this.emit("error", err); }
			};
			const onReaderError = (error: Error) => {
				const protocolError = new Error(`Agency broker protocol error: ${error.message}`, { cause: error });
				if (!connectionEstablished) { onError(protocolError); return; }
				this.disconnectError = protocolError; this.emit("error", protocolError); socket.destroy();
			};
			const reader = createMessageReader((msg) => this.handleBrokerMessage(msg), onReaderError);
			const cleanupConnectionAttempt = () => { this.off("_registered", onRegistered); socket.off("error", onError); clearTimeout(timeout); };
			const cleanupSocketListeners = () => { socket.off("data", reader); socket.off("error", onSocketError); socket.off("close", onClose); };
			socket.on("data", reader);
			socket.on("error", onError);
			socket.on("close", onClose);
			socket.on("error", onSocketError);
			this.once("_registered", onRegistered);
			try {
				writeMessage(socket, { type: "register", session, ...(sessionId ? { sessionId } : {}), ...(typeof target === "string" ? {} : { stateId: target.stateId }) });
			} catch (error) {
				cleanupConnectionAttempt(); cleanupSocketListeners(); if (this.socket === socket) this.socket = null; socket.destroy(); reject(toError(error));
			}
		});
	}

	private handleBrokerMessage(msg: unknown): void {
		if (typeof msg !== "object" || msg === null || !("type" in msg) || typeof msg.type !== "string") throw new Error("Invalid agency broker message");
		const brokerMessage = msg as { type: string } & Record<string, unknown>;
		if (this._sessionId === null && brokerMessage.type !== "registered" && brokerMessage.type !== "error") throw new Error(`Received ${brokerMessage.type} before registered`);
		switch (brokerMessage.type) {
			case "registered":
				if (typeof brokerMessage.sessionId !== "string") throw new Error("Invalid registered message");
				if (this._sessionId !== null) throw new Error("Received duplicate registered message");
				this._sessionId = brokerMessage.sessionId;
				this.emit("_registered", { type: "registered", sessionId: brokerMessage.sessionId });
				break;
			case "sessions": {
				const { requestId, sessions } = brokerMessage;
				if (typeof requestId !== "string" || !Array.isArray(sessions) || !sessions.every(isSessionInfo)) throw new Error("Invalid sessions message");
				const pending = this.pendingLists.get(requestId);
				if (!pending) return;
				this.pendingLists.delete(requestId); pending.resolve(sessions);
				break;
			}
			case "message": {
				const { from, message } = brokerMessage;
				if (!isSessionInfo(from) || !isBrokerMessageEnvelope(message)) throw new Error("Invalid message event");
				const agency = (message as BrokerMessageEnvelope).agency;
				if (agency.replyTo) {
					const waiter = this.pendingAsks.get(agency.replyTo);
					if (waiter) {
						this.pendingAsks.delete(agency.replyTo);
						clearTimeout(waiter.timeout);
						waiter.resolve(agency);
						break;
					}
				}
				this.emit("message", from, agency);
				break;
			}
			case "delivered": {
				if (typeof brokerMessage.messageId !== "string") throw new Error("Invalid delivered message");
				const pending = this.pendingSends.get(brokerMessage.messageId); if (!pending) return;
				this.pendingSends.delete(brokerMessage.messageId); pending.resolve({ id: brokerMessage.messageId, delivered: true });
				break;
			}
			case "delivery_failed": {
				if (typeof brokerMessage.messageId !== "string" || typeof brokerMessage.reason !== "string") throw new Error("Invalid delivery_failed message");
				const pending = this.pendingSends.get(brokerMessage.messageId); if (!pending) return;
				this.pendingSends.delete(brokerMessage.messageId); pending.resolve({ id: brokerMessage.messageId, delivered: false, reason: brokerMessage.reason });
				break;
			}
			case "session_joined":
				if (!isSessionInfo(brokerMessage.session)) throw new Error("Invalid session_joined message");
				this.emit("session_joined", brokerMessage.session); break;
			case "session_left":
				if (typeof brokerMessage.sessionId !== "string") throw new Error("Invalid session_left message");
				this.emit("session_left", brokerMessage.sessionId); break;
			case "presence_update":
				if (!isSessionInfo(brokerMessage.session)) throw new Error("Invalid presence_update message");
				this.emit("presence_update", brokerMessage.session); break;
			case "error":
				if (typeof brokerMessage.error !== "string") throw new Error("Invalid error message");
				if (this._sessionId === null) throw new Error(brokerMessage.error);
				this.emit("error", new Error(brokerMessage.error)); break;
			default:
				throw new Error(`Unknown agency broker message type: ${brokerMessage.type}`);
		}
	}

	async disconnect(): Promise<void> {
		const socket = this.socket;
		if (!socket) return;
		this.disconnecting = true; this.disconnectError = null; this.failPending(new Error("Agency broker client disconnected"));
		await new Promise<void>((resolve) => {
			let settled = false;
			const finish = () => { if (settled) return; settled = true; clearTimeout(timeout); socket.off("close", onClose); socket.off("error", onError); resolve(); };
			const onClose = () => finish();
			const onError = () => socket.destroy();
			const timeout = setTimeout(() => socket.destroy(), 2000);
			socket.once("close", onClose); socket.once("error", onError);
			try { writeMessage(socket, { type: "unregister" }); socket.end(); } catch { socket.destroy(); }
		});
	}

	listSessions(): Promise<AgencySessionInfo[]> {
		let socket: net.Socket;
		try { socket = this.requireActiveSocket(); } catch (error) { return Promise.reject(toError(error)); }
		return new Promise((resolve, reject) => {
			const requestId = randomUUID();
			const timeout = setTimeout(() => { if (this.pendingLists.has(requestId)) { this.pendingLists.delete(requestId); reject(new Error("List sessions timeout")); } }, 5000);
			this.pendingLists.set(requestId, { resolve: (sessions) => { clearTimeout(timeout); resolve(sessions); }, reject: (error) => { clearTimeout(timeout); reject(error); } });
			try { writeMessage(socket, { type: "list", requestId }); } catch (error) { clearTimeout(timeout); this.pendingLists.delete(requestId); reject(toError(error)); }
		});
	}

	send(to: string, message: AgencyMessage): Promise<SendResult> {
		let socket: net.Socket;
		try { socket = this.requireActiveSocket(); } catch (error) { return Promise.reject(toError(error)); }
		if (!isAgencyMessage(message)) return Promise.reject(new Error("Invalid agency message"));
		const envelope = toBrokerEnvelope(message);
		return this.sendEnvelope(socket, to, envelope);
	}

	ask(to: string, message: AgencyMessage, timeoutMs = 600_000): Promise<AgencyMessage> {
		const ask = { ...message, expectsReply: true };
		return new Promise((resolve, reject) => {
			const timeout = setTimeout(() => {
				this.pendingAsks.delete(ask.id);
				this.cancelAsk(ask.id);
				reject(new Error("Agency ask timeout"));
			}, timeoutMs);
			this.pendingAsks.set(ask.id, { resolve, reject, timeout });
			this.send(to, ask).then((result) => {
				if (!result.delivered) {
					this.pendingAsks.delete(ask.id);
					clearTimeout(timeout);
					reject(new Error(result.reason || "Agency ask not delivered"));
				}
			}, (error) => {
				this.pendingAsks.delete(ask.id);
				clearTimeout(timeout);
				reject(error);
			});
		});
	}

	private sendEnvelope(socket: net.Socket, to: string, message: BrokerMessageEnvelope): Promise<SendResult> {
		return new Promise((resolve, reject) => {
			const timeout = setTimeout(() => { if (this.pendingSends.has(message.id)) { this.pendingSends.delete(message.id); reject(new Error("Send timeout")); } }, 10_000);
			this.pendingSends.set(message.id, { resolve: (result) => { clearTimeout(timeout); resolve(result); }, reject: (error) => { clearTimeout(timeout); reject(error); } });
			try { writeMessage(socket, { type: "send", to, message }); } catch (error) { clearTimeout(timeout); this.pendingSends.delete(message.id); reject(toError(error)); }
		});
	}

	cancelAsk(messageId: string): void {
		if (this.disconnecting) return;
		const socket = this.socket;
		if (!socket || !this._sessionId || socket.destroyed || socket.writableEnded || !socket.writable) return;
		try { writeMessage(socket, { type: "cancel_ask", messageId }); } catch { /* best effort */ }
	}

	updatePresence(updates: { name?: string; role?: string; status?: string; model?: string; taskId?: string | null }): void {
		if (this.disconnecting) return;
		const socket = this.socket;
		if (!socket || !this._sessionId || socket.destroyed || socket.writableEnded || !socket.writable) return;
		writeMessage(socket, { type: "presence", ...updates });
	}
}
