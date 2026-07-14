export interface AgencySessionInfo {
	id: string;
	name?: string;
	role?: string;
	isHub?: boolean;
	cwd: string;
	model: string;
	pid: number;
	startedAt: number;
	lastActivity: number;
	status?: string;
	lifecycle?: string;
	taskId?: string | null;
	trustedLocal?: boolean;
}

export type AgencySessionRegistration = Omit<AgencySessionInfo, "id" | "trustedLocal">;

export type AgencyMessageKind = "delegate" | "report" | "ask" | "reply" | "progress" | "release";

export interface AgencyMessage {
	id: string;
	kind: AgencyMessageKind;
	from: string;
	to: string;
	taskId?: string;
	workflowId?: string;
	correlationId?: string;
	replyTo?: string;
	expectsReply?: boolean;
	createdAt: number;
	payload?: unknown;
	payloadPath?: string;
}

export interface BrokerMessageEnvelope {
	id: string;
	timestamp: number;
	replyTo?: string;
	expectsReply?: boolean;
	agency: AgencyMessage;
}

export type ClientMessage =
	| { type: "health"; requestId: string; stateId?: string }
	| { type: "register"; session: AgencySessionRegistration; sessionId?: string; stateId?: string }
	| { type: "unregister" }
	| { type: "list"; requestId: string }
	| { type: "send"; to: string; message: BrokerMessageEnvelope }
	| { type: "cancel_ask"; messageId: string }
	| { type: "presence"; name?: string; role?: string; status?: string; model?: string; taskId?: string | null };

export type BrokerMessage =
	| { type: "health_ok"; requestId: string; protocol: string; version: number }
	| { type: "registered"; sessionId: string }
	| { type: "sessions"; requestId: string; sessions: AgencySessionInfo[] }
	| { type: "message"; from: AgencySessionInfo; message: BrokerMessageEnvelope }
	| { type: "presence_update"; session: AgencySessionInfo }
	| { type: "session_joined"; session: AgencySessionInfo }
	| { type: "session_left"; sessionId: string }
	| { type: "error"; error: string }
	| { type: "delivered"; messageId: string }
	| { type: "delivery_failed"; messageId: string; reason: string };
