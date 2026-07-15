import { spawn, type ChildProcess } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import net from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createMessageReader, writeMessage } from "./framing.ts";
import {
	AGENCY_BROKER_PROTOCOL_NAME,
	AGENCY_BROKER_PROTOCOL_VERSION,
	AGENCY_BROKER_RUNTIME_FILE_MODE,
	ensureAgencyBrokerRuntimeDir,
	ensureAgencyBrokerSocketDir,
	getAgentDirPath,
	getBrokerConnectTarget,
	requireBrokerContext,
	restrictAgencyBrokerRuntimeFile,
	writeAgencyBrokerRuntimeFile,
	type AvailableBrokerContext,
	type BrokerConnectTarget,
	type BrokerContext,
} from "./paths.ts";

// Adapted from pi-intercom (MIT, Copyright (c) 2026 Nico Bailon).
const EXTENSION_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");

type BrokerLaunchSpec =
	| { kind: "direct"; command: string; args: string[] }
	| { kind: "windows-launcher"; command: string; args: string[]; launcherPath: string; launcherCommandLine: string };

export interface BrokerLaunchOptions {
	brokerPath: string;
	brokerCommand: string;
	brokerArgs: string[];
	brokerDir: string;
	extensionDir?: string;
	platform?: NodeJS.Platform;
	nodePath?: string;
}

function sleep(ms: number): Promise<void> { return new Promise((resolve) => setTimeout(resolve, ms)); }
function toError(error: unknown): Error { return error instanceof Error ? error : new Error(String(error)); }

export function getTsxCliPath(extensionDir: string = EXTENSION_DIR): string | null {
	try {
		const requireFromExtension = createRequire(import.meta.url);
		const tsxMain = requireFromExtension.resolve("tsx");
		return join(dirname(tsxMain), "cli.mjs");
	} catch {
		const fallback = join(extensionDir, "node_modules", "tsx", "dist", "cli.mjs");
		return existsSync(fallback) ? fallback : null;
	}
}

function quoteWindowsArg(value: string): string { return `"${value.replace(/"/g, '""')}"`; }
function usesDefaultBrokerCommand(command: string, args: string[]): boolean { return command === "npx" && args.length === 2 && args[0] === "--no-install" && args[1] === "tsx"; }

export function getWindowsHiddenLauncherPath(brokerDir: string): string { return join(brokerDir, "broker-launch.vbs"); }
export function getWindowsBrokerCommandLine(brokerPath: string, extensionDir = EXTENSION_DIR, nodePath = process.execPath, brokerCommand = "npx", brokerArgs: string[] = ["--no-install", "tsx"]): string {
	const tsxCli = getTsxCliPath(extensionDir);
	if (usesDefaultBrokerCommand(brokerCommand, brokerArgs) && tsxCli) return [quoteWindowsArg(nodePath), quoteWindowsArg(tsxCli), quoteWindowsArg(brokerPath)].join(" ");
	return [quoteWindowsArg(brokerCommand), ...brokerArgs.map(quoteWindowsArg), quoteWindowsArg(brokerPath)].join(" ");
}
export function getWindowsHiddenLauncherScript(commandLine: string): string {
	return ['Set WshShell = CreateObject("WScript.Shell")', `WshShell.Run "${commandLine.replace(/"/g, '""')}", 0, False`, 'Set WshShell = Nothing', ''].join("\r\n");
}
function writeWindowsHiddenLauncher(commandLine: string, launcherPath: string, platform: NodeJS.Platform): string {
	ensureAgencyBrokerRuntimeDir(dirname(launcherPath), platform);
	writeAgencyBrokerRuntimeFile(launcherPath, getWindowsHiddenLauncherScript(commandLine), platform);
	return launcherPath;
}

export function isBrokerHealthOkMessage(message: unknown, requestId: string): boolean {
	if (typeof message !== "object" || message === null || !("type" in message)) return false;
	const response = message as Record<string, unknown>;
	return response.type === "health_ok" && response.requestId === requestId && response.protocol === AGENCY_BROKER_PROTOCOL_NAME && response.version === AGENCY_BROKER_PROTOCOL_VERSION;
}

export function getBrokerLaunchSpec(options: BrokerLaunchOptions): BrokerLaunchSpec {
	const {
		brokerPath,
		brokerCommand,
		brokerArgs,
		brokerDir,
		extensionDir = EXTENSION_DIR,
		platform = process.platform,
		nodePath = process.execPath,
	} = options;
	if (platform === "win32") {
		const launcherPath = getWindowsHiddenLauncherPath(brokerDir);
		return { kind: "windows-launcher", command: "wscript.exe", args: [launcherPath], launcherPath, launcherCommandLine: getWindowsBrokerCommandLine(brokerPath, extensionDir, nodePath, brokerCommand, brokerArgs) };
	}
	const tsxCli = getTsxCliPath(extensionDir);
	if (usesDefaultBrokerCommand(brokerCommand, brokerArgs) && tsxCli) return { kind: "direct", command: nodePath, args: [tsxCli, brokerPath] };
	if (usesDefaultBrokerCommand(brokerCommand, brokerArgs)) return { kind: "direct", command: "npx", args: ["--yes", "tsx", brokerPath] };
	return { kind: "direct", command: brokerCommand, args: [...brokerArgs, brokerPath] };
}

export function getBrokerSpawnOptions(
	context: BrokerContext,
	extensionDir: string = EXTENSION_DIR,
	env: NodeJS.ProcessEnv = process.env,
): { detached: true; stdio: "ignore"; cwd: string; env: NodeJS.ProcessEnv; windowsHide: true } {
	const available = requireBrokerContext(context);
	const childEnv: NodeJS.ProcessEnv = {
		...env,
		AGENCY_ROOT: available.agencyRoot,
		AGENCY_PROJECT_ROOT: available.projectRoot,
		PI_CODING_AGENT_DIR: getAgentDirPath(env),
		NODE_NO_WARNINGS: "1",
	};
	delete childEnv.AGENCY_BROKER_TRANSPORT;
	delete childEnv.AGENCY_BROKER_TCP;
	if (available.useWindowsTcp) {
		childEnv.AGENCY_BROKER_TRANSPORT = "tcp";
		childEnv.AGENCY_BROKER_TCP = "1";
	}
	return { detached: true, stdio: "ignore", cwd: extensionDir, env: childEnv, windowsHide: true };
}

export interface SpawnBrokerOptions {
	startupTimeoutMs?: number;
}

export async function spawnBrokerIfNeeded(
	context: BrokerContext,
	brokerCommand = "npx",
	brokerArgs: string[] = ["--no-install", "tsx"],
	options: SpawnBrokerOptions = {},
): Promise<void> {
	const available = requireBrokerContext(context);
	const startupTimeoutMs = options.startupTimeoutMs ?? 5000;
	ensureAgencyBrokerRuntimeDir(available.brokerDir, available.platform, available.agencyRoot);
	ensureAgencyBrokerSocketDir(available);
	if (await isBrokerRunning(available)) return;
	const lockToken = acquireBrokerSpawnLock(available);
	if (!lockToken) { await waitForBroker(available, startupTimeoutMs); return; }
	try {
		if (await isBrokerRunning(available)) return;
		const brokerPath = join(dirname(fileURLToPath(import.meta.url)), "broker.ts");
		const launch = getBrokerLaunchSpec({
			brokerPath,
			brokerCommand,
			brokerArgs,
			brokerDir: available.brokerDir,
			extensionDir: EXTENSION_DIR,
			platform: available.platform,
		});
		if (launch.kind === "windows-launcher") writeWindowsHiddenLauncher(launch.launcherCommandLine, launch.launcherPath, available.platform);
		const child = spawn(launch.command, launch.args, getBrokerSpawnOptions(available));
		child.unref();
		try {
			await new Promise<void>((resolve, reject) => {
				const cleanup = () => { child.off("error", onError); child.off("exit", onExit); };
				const onError = (error: Error) => { cleanup(); reject(new Error(`Failed to spawn agency broker: ${error.message}`, { cause: error })); };
				const onExit = (code: number | null, signal: NodeJS.Signals | null) => {
					if (launch.kind === "windows-launcher" && code === 0 && signal === null) return;
					cleanup(); reject(new Error(signal ? `Agency broker exited before startup with signal ${signal}` : `Agency broker exited before startup with code ${code ?? "unknown"}`));
				};
				child.once("error", onError); child.once("exit", onExit);
				waitForBroker(available, startupTimeoutMs).then(() => { cleanup(); resolve(); }, (error) => { cleanup(); reject(toError(error)); });
			});
		} catch (error) {
			if (launch.kind === "direct") await terminateAndReap(child);
			throw error;
		}
	} finally {
		releaseBrokerSpawnLock(available, lockToken);
	}
}

export async function isBrokerRunning(context: BrokerContext): Promise<boolean> {
	const available = requireBrokerContext(context);
	if (await checkSocketConnectable(available)) return true;
	if (!existsSync(available.pidFile)) return false;
	try {
		const pid = Number.parseInt(readFileSync(available.pidFile, "utf-8").trim(), 10);
		if (!Number.isFinite(pid)) return false;
		process.kill(pid, 0);
		return checkSocketConnectable(available);
	} catch { return false; }
}

function connectToBrokerTarget(target: BrokerConnectTarget): net.Socket { return typeof target === "string" ? net.connect(target) : net.connect({ host: target.host, port: target.port }); }

export function checkSocketConnectable(context: BrokerContext): Promise<boolean> {
	return new Promise((resolve) => {
		let target: BrokerConnectTarget;
		try { target = getBrokerConnectTarget(context); } catch { resolve(false); return; }
		const socket = connectToBrokerTarget(target);
		const requestId = randomUUID();
		const expectedStateId = typeof target === "string" ? undefined : target.stateId;
		let settled = false;
		const finish = (isConnected: boolean) => {
			if (settled) return;
			settled = true; clearTimeout(timeout); socket.off("connect", onConnect); socket.off("error", onError); socket.off("data", reader); socket.destroy(); resolve(isConnected);
		};
		const onConnect = () => { try { writeMessage(socket, { type: "health", requestId, ...(expectedStateId ? { stateId: expectedStateId } : {}) }); } catch { finish(false); } };
		const onError = () => finish(false);
		const reader = createMessageReader((message) => finish(isBrokerHealthOkMessage(message, requestId)), () => finish(false));
		socket.on("connect", onConnect); socket.on("error", onError); socket.on("data", reader);
		const timeout = setTimeout(() => finish(false), 1000);
	});
}

interface BrokerSpawnLock {
	pid: number;
	createdAt: number;
	token: string;
}

function parseSpawnLock(contents: string): BrokerSpawnLock | null {
	try {
		const parsed = JSON.parse(contents) as Partial<BrokerSpawnLock>;
		return Number.isSafeInteger(parsed.pid) && Number.isFinite(parsed.createdAt) && typeof parsed.token === "string" && parsed.token.length > 0
			? parsed as BrokerSpawnLock
			: null;
	} catch { return null; }
}

export function acquireBrokerSpawnLock(context: AvailableBrokerContext): string | null {
	for (let attempt = 0; attempt < 5; attempt++) {
		const token = randomUUID();
		try {
			writeFileSync(context.spawnLockFile, `${JSON.stringify({ pid: process.pid, createdAt: Date.now(), token })}\n`, { flag: "wx", mode: AGENCY_BROKER_RUNTIME_FILE_MODE });
			restrictAgencyBrokerRuntimeFile(context.spawnLockFile, context.platform);
			return token;
		} catch (error) {
			if (!(error instanceof Error) || (error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
			if (isBrokerSpawnLockStale(context)) { try { unlinkSync(context.spawnLockFile); } catch { /* ownership changed; retry */ } continue; }
			return null;
		}
	}
	return null;
}

export function isBrokerSpawnLockStale(context: AvailableBrokerContext): boolean {
	let contents: string;
	try {
		contents = readFileSync(context.spawnLockFile, "utf-8");
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return true;
		throw error;
	}
	const lock = parseSpawnLock(contents);
	if (!lock) return true;
	try {
		process.kill(lock.pid, 0);
		return false;
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "EPERM") return false;
		return true;
	}
}

export function releaseBrokerSpawnLock(context: AvailableBrokerContext, token: string): boolean {
	try {
		const lock = parseSpawnLock(readFileSync(context.spawnLockFile, "utf-8"));
		if (lock?.token !== token || lock.pid !== process.pid) return false;
		unlinkSync(context.spawnLockFile);
		return true;
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
		throw error;
	}
}

async function terminateAndReap(child: ChildProcess): Promise<void> {
	if (child.exitCode !== null || child.signalCode !== null || !child.pid) return;
	child.ref();
	await new Promise<void>((resolve) => {
		let forceTimer: ReturnType<typeof setTimeout> | undefined;
		let settled = false;
		const finish = () => {
			if (settled) return;
			settled = true;
			if (forceTimer) clearTimeout(forceTimer);
			child.off("exit", finish);
			resolve();
		};
		child.once("exit", finish);
		if (child.exitCode !== null || child.signalCode !== null) { finish(); return; }
		try { child.kill("SIGTERM"); } catch { finish(); return; }
		forceTimer = setTimeout(() => { try { child.kill("SIGKILL"); } catch { /* already exited */ } }, 1000);
		forceTimer.unref?.();
	});
}

export async function waitForBroker(context: BrokerContext, timeoutMs = 5000): Promise<void> {
	const start = Date.now();
	while (Date.now() - start < timeoutMs) {
		if (await checkSocketConnectable(context)) return;
		await sleep(Math.min(100, Math.max(1, timeoutMs - (Date.now() - start))));
	}
	throw new Error("Agency broker failed to start within timeout");
}
