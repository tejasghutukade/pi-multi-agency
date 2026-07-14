import { chmodSync, mkdirSync, readFileSync } from "node:fs";
import { isAbsolute, join, resolve } from "node:path";
import { homedir } from "node:os";

// Adapted from pi-intercom (MIT, Copyright (c) 2026 Nico Bailon).
export const AGENCY_BROKER_DIR_MODE = 0o700;
export const AGENCY_BROKER_RUNTIME_FILE_MODE = 0o600;
export const AGENCY_BROKER_TCP_HOST = "127.0.0.1";
export const AGENCY_BROKER_PROTOCOL_NAME = "multi-agency-broker";
export const AGENCY_BROKER_PROTOCOL_VERSION = 1;

export interface BrokerTcpEndpoint {
	transport: "tcp";
	host: string;
	port: number;
	stateId?: string;
}

export type BrokerConnectTarget = string | BrokerTcpEndpoint;

function sanitizePipeSegment(value: string): string {
	return value.replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase() || "default";
}

export function getAgencyRootPath(
	env: NodeJS.ProcessEnv = process.env,
	cwd: string = process.cwd(),
): string | null {
	const configured = env.AGENCY_ROOT?.trim();
	if (configured) return isAbsolute(configured) ? configured : resolve(cwd, configured);
	const projectRoot = env.AGENCY_PROJECT_ROOT?.trim();
	if (projectRoot) return join(isAbsolute(projectRoot) ? projectRoot : resolve(cwd, projectRoot), ".pi", "agency");
	return null;
}

export function getAgentDirPath(
	env: NodeJS.ProcessEnv = process.env,
	homeDir: string = homedir(),
	cwd: string = process.cwd(),
): string {
	const configured = env.PI_CODING_AGENT_DIR?.trim();
	if (!configured) return join(homeDir, ".pi", "agent");
	return isAbsolute(configured) ? configured : resolve(cwd, configured);
}

export function getAgencyBrokerDirPath(
	env: NodeJS.ProcessEnv = process.env,
	agentDir: string = getAgentDirPath(env),
	cwd: string = process.cwd(),
): string {
	const agencyRoot = getAgencyRootPath(env, cwd);
	return agencyRoot ? join(agencyRoot, "runtime", "broker") : join(agentDir, "agency-broker");
}

export function shouldUseWindowsTcpTransport(
	platform: NodeJS.Platform = process.platform,
	env: NodeJS.ProcessEnv = process.env,
): boolean {
	if (platform !== "win32") return false;
	const transport = env.AGENCY_BROKER_TRANSPORT?.trim().toLowerCase();
	return transport === "tcp" || env.AGENCY_BROKER_TCP === "1" || env.AGENCY_BROKER_TCP === "true";
}

export function getBrokerPortFilePath(brokerDir: string = getAgencyBrokerDirPath()): string {
	return join(brokerDir, "broker.port.json");
}

export function getBrokerSocketPath(
	platform: NodeJS.Platform = process.platform,
	env: NodeJS.ProcessEnv = process.env,
): string {
	const brokerDir = getAgencyBrokerDirPath(env);
	if (platform === "win32") return `\\\\.\\pipe\\multi-agency-${sanitizePipeSegment(brokerDir)}`;
	return join(brokerDir, "broker.sock");
}

export function getBrokerConnectTarget(
	platform: NodeJS.Platform = process.platform,
	env: NodeJS.ProcessEnv = process.env,
	brokerDir: string = getAgencyBrokerDirPath(env),
): BrokerConnectTarget {
	if (shouldUseWindowsTcpTransport(platform, env)) {
		const endpointFile = getBrokerPortFilePath(brokerDir);
		const parsed: unknown = JSON.parse(readFileSync(endpointFile, "utf-8"));
		if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
			throw new Error(`Invalid agency broker TCP endpoint at ${endpointFile}: expected object`);
		}
		const endpoint = parsed as Record<string, unknown>;
		if (
			endpoint.transport !== "tcp" || endpoint.host !== AGENCY_BROKER_TCP_HOST ||
			typeof endpoint.port !== "number" || !Number.isSafeInteger(endpoint.port) || endpoint.port <= 0 || endpoint.port > 65535 ||
			typeof endpoint.stateId !== "string" || endpoint.stateId.length === 0
		) {
			throw new Error(`Invalid agency broker TCP endpoint at ${endpointFile}`);
		}
		return { transport: "tcp", host: endpoint.host, port: endpoint.port, stateId: endpoint.stateId };
	}
	return getBrokerSocketPath(platform, env);
}

export function getBrokerListenTarget(
	platform: NodeJS.Platform = process.platform,
	env: NodeJS.ProcessEnv = process.env,
): BrokerConnectTarget {
	if (shouldUseWindowsTcpTransport(platform, env)) return { transport: "tcp", host: AGENCY_BROKER_TCP_HOST, port: 0 };
	return getBrokerSocketPath(platform, env);
}

export function ensureAgencyBrokerRuntimeDir(
	brokerDir: string = getAgencyBrokerDirPath(),
	platform: NodeJS.Platform = process.platform,
): void {
	mkdirSync(brokerDir, { recursive: true, mode: AGENCY_BROKER_DIR_MODE });
	if (platform !== "win32") chmodSync(brokerDir, AGENCY_BROKER_DIR_MODE);
}

export function restrictAgencyBrokerRuntimeFile(
	filePath: string,
	platform: NodeJS.Platform = process.platform,
): void {
	if (platform !== "win32") chmodSync(filePath, AGENCY_BROKER_RUNTIME_FILE_MODE);
}
