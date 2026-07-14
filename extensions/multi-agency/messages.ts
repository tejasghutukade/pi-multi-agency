import { randomUUID } from "node:crypto";
import type { AgencyMessage, AgencyMessageKind, BrokerMessageEnvelope } from "./broker/types.ts";

export const AGENCY_MESSAGE_KINDS: readonly AgencyMessageKind[] = [
	"delegate",
	"report",
	"ask",
	"reply",
	"progress",
	"release",
] as const;

export function isAgencyMessageKind(value: unknown): value is AgencyMessageKind {
	return typeof value === "string" && (AGENCY_MESSAGE_KINDS as readonly string[]).includes(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isAgencyMessage(value: unknown): value is AgencyMessage {
	if (!isRecord(value)) return false;
	if (typeof value.id !== "string" || value.id.length === 0) return false;
	if (!isAgencyMessageKind(value.kind)) return false;
	if (typeof value.from !== "string" || value.from.length === 0) return false;
	if (typeof value.to !== "string" || value.to.length === 0) return false;
	if (value.taskId !== undefined && typeof value.taskId !== "string") return false;
	if (value.workflowId !== undefined && typeof value.workflowId !== "string") return false;
	if (value.correlationId !== undefined && typeof value.correlationId !== "string") return false;
	if (value.replyTo !== undefined && typeof value.replyTo !== "string") return false;
	if (value.expectsReply !== undefined && typeof value.expectsReply !== "boolean") return false;
	if (typeof value.createdAt !== "number" || !Number.isFinite(value.createdAt)) return false;
	if (value.payloadPath !== undefined && typeof value.payloadPath !== "string") return false;
	return true;
}

export function isBrokerMessageEnvelope(value: unknown): value is BrokerMessageEnvelope {
	if (!isRecord(value)) return false;
	if (typeof value.id !== "string" || value.id.length === 0) return false;
	if (typeof value.timestamp !== "number" || !Number.isFinite(value.timestamp)) return false;
	if (value.replyTo !== undefined && typeof value.replyTo !== "string") return false;
	if (value.expectsReply !== undefined && typeof value.expectsReply !== "boolean") return false;
	return isAgencyMessage(value.agency);
}

export function makeAgencyMessage(input: Omit<AgencyMessage, "id" | "createdAt"> & Partial<Pick<AgencyMessage, "id" | "createdAt">>): AgencyMessage {
	const message = {
		...input,
		id: input.id ?? randomUUID(),
		createdAt: input.createdAt ?? Date.now(),
	};
	if (!isAgencyMessage(message)) throw new Error("Invalid agency message");
	return message;
}

export function toBrokerEnvelope(message: AgencyMessage): BrokerMessageEnvelope {
	return {
		id: message.id,
		timestamp: message.createdAt,
		replyTo: message.replyTo,
		expectsReply: message.expectsReply,
		agency: message,
	};
}

export function formatInboundAgencyMessage(message: AgencyMessage): string {
	const payload = isRecord(message.payload) ? message.payload : {};
	if (message.kind === "delegate") {
		const lines = [
			`# Agency delegate: ${message.taskId || message.id}`,
			"",
			`From: ${message.from}`,
			`To: ${message.to}`,
		];
		if (message.workflowId) lines.push(`Workflow: ${message.workflowId}`);
		if (typeof payload.goal === "string") lines.push("", "## Goal", payload.goal);
		if (Array.isArray(payload.contextPaths) && payload.contextPaths.length) lines.push("", "## Context paths", ...payload.contextPaths.map(String).map((p) => `- ${p}`));
		if (typeof payload.successCriteria === "string") lines.push("", "## Success criteria", payload.successCriteria);
		if (typeof payload.constraints === "string") lines.push("", "## Constraints", payload.constraints);
		if (typeof payload.outputShape === "string") lines.push("", "## Output shape", payload.outputShape);
		if (typeof payload.stopRules === "string") lines.push("", "## Stop rules", payload.stopRules);
		lines.push("", "Report completion with the `agency_report` tool. Ask blocking questions with `agency_ask`.");
		return lines.join("\n");
	}

	const title = message.kind === "ask" ? "Agency ask" : message.kind === "report" ? "Agency report" : `Agency ${message.kind}`;
	const summary = typeof payload.summary === "string" ? payload.summary : typeof payload.output === "string" ? payload.output : typeof payload.message === "string" ? payload.message : JSON.stringify(payload, null, 2);
	const lines = [`# ${title}: ${message.taskId || message.id}`, "", `From: ${message.from}`, `To: ${message.to}`];
	if (message.replyTo) lines.push(`Reply-To: ${message.replyTo}`);
	if (summary && summary !== "{}") lines.push("", summary);
	return lines.join("\n");
}
