import { createHash, randomUUID } from "node:crypto";
import { chmodSync, existsSync, lstatSync, mkdirSync, readFileSync, realpathSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, relative, resolve } from "node:path";

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

interface BrokerContextBase {
	available: boolean;
	projectRoot: string | null;
	agencyRoot: string | null;
	projectKey: string | null;
	brokerDir: string | null;
	socketDir: string | null;
	endpoint: string | null;
	portFile: string | null;
	pidFile: string | null;
	spawnLockFile: string | null;
	platform: NodeJS.Platform;
	useWindowsTcp: boolean;
	diagnostic: string;
}

export interface AvailableBrokerContext extends BrokerContextBase {
	available: true;
	projectRoot: string;
	agencyRoot: string;
	projectKey: string;
	brokerDir: string;
	socketDir: string | null;
	endpoint: string;
	portFile: string;
	pidFile: string;
	spawnLockFile: string;
}

export interface UnavailableBrokerContext extends BrokerContextBase {
	available: false;
	projectRoot: null;
	agencyRoot: null;
	projectKey: null;
	brokerDir: null;
	socketDir: null;
	endpoint: null;
	portFile: null;
	pidFile: null;
	spawnLockFile: null;
}

export type BrokerContext = AvailableBrokerContext | UnavailableBrokerContext;

export interface ResolveBrokerContextOptions {
	projectRoot?: string | null;
	agencyRoot?: string | null;
	env?: NodeJS.ProcessEnv;
	cwd?: string;
	platform?: NodeJS.Platform;
}

function canonicalize(input: string, base: string, platform: NodeJS.Platform): string {
	const absolute = resolve(base, input);
	let canonical = absolute;
	try { canonical = realpathSync.native(absolute); } catch { /* deterministic lexical fallback */ }
	if (platform === "win32") canonical = canonical.toLowerCase();
	return canonical;
}

export interface ProjectRootDiscovery {
	projectRoot: string;
	initialized: boolean;
}

export function discoverProjectRoot(
	start: string,
	platform: NodeJS.Platform = process.platform,
): ProjectRootDiscovery {
	const resolvedStart = resolve(start);
	let current = resolvedStart;
	let packageBoundary: string | null = null;
	while (true) {
		if (existsSync(join(current, ".pi", "agency"))) {
			return { projectRoot: canonicalize(current, current, platform), initialized: true };
		}
		if (!packageBoundary && existsSync(join(current, "package.json"))) packageBoundary = current;
		const parent = dirname(current);
		if (parent === current) break;
		current = parent;
	}
	return { projectRoot: packageBoundary || resolvedStart, initialized: false };
}

function deriveProjectFromAgency(agencyRoot: string): string | null {
	const piDir = dirname(agencyRoot);
	if (agencyRoot !== join(piDir, "agency") || dirname(piDir) === piDir) return null;
	if (piDir !== join(dirname(piDir), ".pi")) return null;
	return dirname(piDir);
}

function unavailable(diagnostic: string, platform: NodeJS.Platform): UnavailableBrokerContext {
	return Object.freeze({
		available: false,
		projectRoot: null,
		agencyRoot: null,
		projectKey: null,
		brokerDir: null,
		socketDir: null,
		endpoint: null,
		portFile: null,
		pidFile: null,
		spawnLockFile: null,
		platform,
		useWindowsTcp: false,
		diagnostic,
	});
}

export function shouldUseWindowsTcpTransport(
	platform: NodeJS.Platform = process.platform,
	env: NodeJS.ProcessEnv = process.env,
): boolean {
	if (platform !== "win32") return false;
	const transport = env.AGENCY_BROKER_TRANSPORT?.trim().toLowerCase();
	return transport === "tcp" || env.AGENCY_BROKER_TCP === "1" || env.AGENCY_BROKER_TCP === "true";
}

export function resolveBrokerContext(options: ResolveBrokerContextOptions = {}): BrokerContext {
	const env = options.env ?? process.env;
	const cwd = resolve(options.cwd ?? process.cwd());
	const platform = options.platform ?? process.platform;
	const configuredProject = options.projectRoot?.trim() || env.AGENCY_PROJECT_ROOT?.trim() || null;
	const configuredAgency = options.agencyRoot?.trim() || env.AGENCY_ROOT?.trim() || null;

	let projectRoot: string | null = configuredProject ? canonicalize(configuredProject, cwd, platform) : null;
	let agencyRoot: string | null = null;
	let lexicalAgencyRoot: string | null = null;
	if (configuredAgency) {
		const agencyBase = projectRoot && !isAbsolute(configuredAgency) ? projectRoot : cwd;
		lexicalAgencyRoot = resolve(agencyBase, configuredAgency);
		agencyRoot = canonicalize(configuredAgency, agencyBase, platform);
	}

	if (!projectRoot && agencyRoot) {
		projectRoot = deriveProjectFromAgency(lexicalAgencyRoot || agencyRoot);
		if (!projectRoot) {
			return unavailable(
				`Agency broker unavailable: AGENCY_ROOT must use the conventional <project>/.pi/agency location; received ${agencyRoot}. Set both AGENCY_PROJECT_ROOT and AGENCY_ROOT consistently.`,
				platform,
			);
		}
		projectRoot = canonicalize(projectRoot, cwd, platform);
	}
	if (!projectRoot) {
		const discovered = discoverProjectRoot(cwd, platform);
		if (discovered.initialized) projectRoot = discovered.projectRoot;
	}
	if (!projectRoot) {
		return unavailable(
			`Agency broker unavailable: no initialized .pi/agency ancestor was found from ${cwd}. Run /agency-init in the owning project or set AGENCY_PROJECT_ROOT and AGENCY_ROOT before starting Pi.`,
			platform,
		);
	}

	const expectedAgencyRoot = canonicalize(join(projectRoot, ".pi", "agency"), projectRoot, platform);
	const agencyRelative = relative(projectRoot, expectedAgencyRoot);
	if (!agencyRelative || agencyRelative.split(/[\\/]/)[0] === ".." || isAbsolute(agencyRelative)) {
		return unavailable(
			`Agency broker unavailable: canonical agency root ${expectedAgencyRoot} escapes owning project ${projectRoot}. Replace the .pi/agency symlink with a project-local directory.`,
			platform,
		);
	}
	if (agencyRoot && agencyRoot !== expectedAgencyRoot) {
		return unavailable(
			`Agency broker unavailable: agency root ${agencyRoot} does not match owning project ${projectRoot}; expected ${expectedAgencyRoot}. Restart Pi with consistent AGENCY_PROJECT_ROOT and AGENCY_ROOT values.`,
			platform,
		);
	}
	agencyRoot = expectedAgencyRoot;

	const projectKey = createHash("sha256").update(projectRoot).digest("hex").slice(0, 16);
	const brokerDir = join(agencyRoot, "runtime", "broker");
	const useWindowsTcp = shouldUseWindowsTcpTransport(platform, env);
	const socketDir = platform === "win32" ? null : join("/tmp", `pi-agency-${process.getuid?.() ?? "user"}`);
	const endpoint = platform === "win32"
		? (useWindowsTcp ? join(brokerDir, "broker.sock") : `\\\\.\\pipe\\multi-agency-${projectKey}`)
		: join(socketDir!, `${projectKey}.sock`);
	const portFile = join(brokerDir, "broker.port.json");
	return Object.freeze({
		available: true,
		projectRoot,
		agencyRoot,
		projectKey,
		brokerDir,
		socketDir,
		endpoint,
		portFile,
		pidFile: join(brokerDir, "broker.pid"),
		spawnLockFile: join(brokerDir, "broker.spawn.lock"),
		platform,
		useWindowsTcp,
		diagnostic: `project=${projectRoot}; agency=${agencyRoot}; key=${projectKey}; endpoint=${useWindowsTcp ? portFile : endpoint}`,
	});
}

export function requireBrokerContext(context: BrokerContext): AvailableBrokerContext {
	if (!context.available) throw new Error(context.diagnostic);
	return context;
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

export function getBrokerPortFilePath(context: AvailableBrokerContext): string {
	return context.portFile;
}

export function getBrokerSocketPath(context: AvailableBrokerContext): string {
	return context.endpoint;
}

export function getBrokerConnectTarget(context: BrokerContext): BrokerConnectTarget {
	const available = requireBrokerContext(context);
	if (available.useWindowsTcp) {
		const parsed: unknown = JSON.parse(readFileSync(available.portFile, "utf-8"));
		if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
			throw new Error(`Invalid agency broker TCP endpoint at ${available.portFile}: expected object`);
		}
		const endpoint = parsed as Record<string, unknown>;
		if (
			endpoint.transport !== "tcp" || endpoint.host !== AGENCY_BROKER_TCP_HOST ||
			typeof endpoint.port !== "number" || !Number.isSafeInteger(endpoint.port) || endpoint.port <= 0 || endpoint.port > 65535 ||
			typeof endpoint.stateId !== "string" || endpoint.stateId.length === 0
		) throw new Error(`Invalid agency broker TCP endpoint at ${available.portFile}`);
		return { transport: "tcp", host: endpoint.host, port: endpoint.port, stateId: endpoint.stateId };
	}
	return available.endpoint;
}

export function getBrokerListenTarget(context: BrokerContext): BrokerConnectTarget {
	const available = requireBrokerContext(context);
	if (available.useWindowsTcp) return { transport: "tcp", host: AGENCY_BROKER_TCP_HOST, port: 0 };
	return available.endpoint;
}

export function ensureAgencyBrokerRuntimeDir(
	brokerDir: string,
	platform: NodeJS.Platform = process.platform,
	agencyRoot?: string,
): void {
	mkdirSync(brokerDir, { recursive: true, mode: AGENCY_BROKER_DIR_MODE });
	if (platform !== "win32") {
		if (agencyRoot && realpathSync.native(brokerDir) !== resolve(brokerDir)) {
			throw new Error(`Agency broker runtime directory must not contain symlinks: ${brokerDir}`);
		}
		chmodSync(brokerDir, AGENCY_BROKER_DIR_MODE);
	}
}

export function ensureAgencyBrokerSocketDir(context: AvailableBrokerContext): void {
	if (!context.socketDir) return;
	try { mkdirSync(context.socketDir, { mode: AGENCY_BROKER_DIR_MODE }); } catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
	}
	const info = lstatSync(context.socketDir);
	if (!info.isDirectory() || info.isSymbolicLink()) throw new Error(`Agency broker socket directory is not a private directory: ${context.socketDir}`);
	const uid = process.getuid?.();
	if (uid !== undefined && statSync(context.socketDir).uid !== uid) throw new Error(`Agency broker socket directory is not owned by uid ${uid}: ${context.socketDir}`);
	chmodSync(context.socketDir, AGENCY_BROKER_DIR_MODE);
}

export function restrictAgencyBrokerRuntimeFile(filePath: string, platform: NodeJS.Platform = process.platform): void {
	if (platform !== "win32") chmodSync(filePath, AGENCY_BROKER_RUNTIME_FILE_MODE);
}

export function writeAgencyBrokerRuntimeFile(
	filePath: string,
	contents: string,
	platform: NodeJS.Platform = process.platform,
): void {
	const temporary = join(dirname(filePath), `.${basename(filePath)}.${process.pid}.${randomUUID()}.tmp`);
	try {
		writeFileSync(temporary, contents, { flag: "wx", mode: AGENCY_BROKER_RUNTIME_FILE_MODE });
		restrictAgencyBrokerRuntimeFile(temporary, platform);
		renameSync(temporary, filePath);
	} finally {
		try { unlinkSync(temporary); } catch { /* renamed or never created */ }
	}
}
