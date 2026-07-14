import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import net from "node:net";
import { randomUUID } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { existsSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { createMessageReader, writeMessage } from "./framing.ts";
import {
	AGENCY_BROKER_PROTOCOL_NAME,
	AGENCY_BROKER_PROTOCOL_VERSION,
	AGENCY_BROKER_RUNTIME_FILE_MODE,
	ensureAgencyBrokerRuntimeDir,
	getAgencyBrokerDirPath,
	getAgentDirPath,
	getBrokerConnectTarget,
	restrictAgencyBrokerRuntimeFile,
	type BrokerConnectTarget,
} from "./paths.ts";

// Adapted from pi-intercom (MIT, Copyright (c) 2026 Nico Bailon).
const BROKER_DIR = getAgencyBrokerDirPath();
const EXTENSION_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");
const BROKER_PID = join(BROKER_DIR, "broker.pid");
const BROKER_SPAWN_LOCK = join(BROKER_DIR, "broker.spawn.lock");

type BrokerLaunchSpec =
	| { kind: "direct"; command: string; args: string[] }
	| { kind: "windows-launcher"; command: string; args: string[]; launcherPath: string; launcherCommandLine: string };

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

export function getWindowsHiddenLauncherPath(brokerDir: string = BROKER_DIR): string { return join(brokerDir, "broker-launch.vbs"); }
export function getWindowsBrokerCommandLine(brokerPath: string, extensionDir = EXTENSION_DIR, nodePath = process.execPath, brokerCommand = "npx", brokerArgs: string[] = ["--no-install", "tsx"]): string {
	const tsxCli = getTsxCliPath(extensionDir);
	if (usesDefaultBrokerCommand(brokerCommand, brokerArgs) && tsxCli) return [quoteWindowsArg(nodePath), quoteWindowsArg(tsxCli), quoteWindowsArg(brokerPath)].join(" ");
	return [quoteWindowsArg(brokerCommand), ...brokerArgs.map(quoteWindowsArg), quoteWindowsArg(brokerPath)].join(" ");
}
export function getWindowsHiddenLauncherScript(commandLine: string): string {
	return ['Set WshShell = CreateObject("WScript.Shell")', `WshShell.Run "${commandLine.replace(/"/g, '""')}", 0, False`, 'Set WshShell = Nothing', ''].join("\r\n");
}
function writeWindowsHiddenLauncher(commandLine: string, launcherPath: string = getWindowsHiddenLauncherPath()): string {
	ensureAgencyBrokerRuntimeDir(dirname(launcherPath));
	writeFileSync(launcherPath, getWindowsHiddenLauncherScript(commandLine), { encoding: "utf-8", mode: AGENCY_BROKER_RUNTIME_FILE_MODE });
	restrictAgencyBrokerRuntimeFile(launcherPath);
	return launcherPath;
}

export function isBrokerHealthOkMessage(message: unknown, requestId: string): boolean {
	if (typeof message !== "object" || message === null || !("type" in message)) return false;
	const response = message as Record<string, unknown>;
	return response.type === "health_ok" && response.requestId === requestId && response.protocol === AGENCY_BROKER_PROTOCOL_NAME && response.version === AGENCY_BROKER_PROTOCOL_VERSION;
}

export function getBrokerLaunchSpec(brokerPath: string, brokerCommand: string, brokerArgs: string[], extensionDir = EXTENSION_DIR, platform: NodeJS.Platform = process.platform, brokerDir: string = BROKER_DIR, nodePath: string = process.execPath): BrokerLaunchSpec {
	if (platform === "win32") {
		const launcherPath = getWindowsHiddenLauncherPath(brokerDir);
		return { kind: "windows-launcher", command: "wscript.exe", args: [launcherPath], launcherPath, launcherCommandLine: getWindowsBrokerCommandLine(brokerPath, extensionDir, nodePath, brokerCommand, brokerArgs) };
	}
	const tsxCli = getTsxCliPath(extensionDir);
	if (usesDefaultBrokerCommand(brokerCommand, brokerArgs) && tsxCli) return { kind: "direct", command: nodePath, args: [tsxCli, brokerPath] };
	if (usesDefaultBrokerCommand(brokerCommand, brokerArgs)) return { kind: "direct", command: "npx", args: ["--yes", "tsx", brokerPath] };
	return { kind: "direct", command: brokerCommand, args: [...brokerArgs, brokerPath] };
}

export function getBrokerSpawnOptions(extensionDir: string = EXTENSION_DIR, env: NodeJS.ProcessEnv = process.env): { detached: true; stdio: "ignore"; cwd: string; env: NodeJS.ProcessEnv; windowsHide: true } {
	return { detached: true, stdio: "ignore", cwd: extensionDir, env: { ...env, PI_CODING_AGENT_DIR: getAgentDirPath(env), NODE_NO_WARNINGS: "1" }, windowsHide: true };
}

export async function spawnBrokerIfNeeded(brokerCommand = "npx", brokerArgs: string[] = ["--no-install", "tsx"]): Promise<void> {
	ensureAgencyBrokerRuntimeDir(BROKER_DIR);
	if (await isBrokerRunning()) return;
	const ownsLock = acquireSpawnLock();
	if (!ownsLock) { await waitForBroker(); return; }
	try {
		if (await isBrokerRunning()) return;
		const brokerPath = join(dirname(fileURLToPath(import.meta.url)), "broker.ts");
		const launch = getBrokerLaunchSpec(brokerPath, brokerCommand, brokerArgs);
		if (launch.kind === "windows-launcher") writeWindowsHiddenLauncher(launch.launcherCommandLine, launch.launcherPath);
		const child = spawn(launch.command, launch.args, getBrokerSpawnOptions());
		child.unref();
		await new Promise<void>((resolve, reject) => {
			const cleanup = () => { child.off("error", onError); child.off("exit", onExit); };
			const onError = (error: Error) => { cleanup(); reject(new Error(`Failed to spawn agency broker: ${error.message}`, { cause: error })); };
			const onExit = (code: number | null, signal: NodeJS.Signals | null) => {
				if (launch.kind === "windows-launcher" && code === 0 && signal === null) return;
				cleanup(); reject(new Error(signal ? `Agency broker exited before startup with signal ${signal}` : `Agency broker exited before startup with code ${code ?? "unknown"}`));
			};
			child.once("error", onError); child.once("exit", onExit);
			waitForBroker().then(() => { cleanup(); resolve(); }, (error) => { cleanup(); reject(toError(error)); });
		});
	} finally {
		releaseSpawnLock();
	}
}

async function isBrokerRunning(): Promise<boolean> {
	if (await checkSocketConnectable()) return true;
	if (!existsSync(BROKER_PID)) return false;
	try {
		const pid = Number.parseInt(readFileSync(BROKER_PID, "utf-8").trim(), 10);
		if (!Number.isFinite(pid)) return false;
		process.kill(pid, 0);
		return checkSocketConnectable();
	} catch { return false; }
}

function connectToBrokerTarget(target: BrokerConnectTarget): net.Socket { return typeof target === "string" ? net.connect(target) : net.connect({ host: target.host, port: target.port }); }

function checkSocketConnectable(): Promise<boolean> {
	return new Promise((resolve) => {
		let target: BrokerConnectTarget;
		try { target = getBrokerConnectTarget(); } catch { resolve(false); return; }
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

function acquireSpawnLock(): boolean {
	for (let attempt = 0; attempt < 5; attempt++) {
		try {
			writeFileSync(BROKER_SPAWN_LOCK, `${process.pid}\n${Date.now()}\n`, { flag: "wx", mode: AGENCY_BROKER_RUNTIME_FILE_MODE });
			restrictAgencyBrokerRuntimeFile(BROKER_SPAWN_LOCK);
			return true;
		} catch (error) {
			if (!(error instanceof Error) || (error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
			if (isSpawnLockStale()) { try { unlinkSync(BROKER_SPAWN_LOCK); } catch { /* retry */ } continue; }
			return false;
		}
	}
	return false;
}

function isSpawnLockStale(): boolean {
	if (!existsSync(BROKER_SPAWN_LOCK)) return false;
	try {
		const [pidLine = "", createdAtLine = "0"] = readFileSync(BROKER_SPAWN_LOCK, "utf-8").trim().split("\n");
		const pid = Number.parseInt(pidLine, 10);
		const createdAt = Number.parseInt(createdAtLine, 10);
		if (Number.isFinite(pid)) { try { process.kill(pid, 0); } catch { return true; } }
		return !Number.isFinite(createdAt) || Date.now() - createdAt > 10_000;
	} catch { return true; }
}

function releaseSpawnLock(): void { try { unlinkSync(BROKER_SPAWN_LOCK); } catch { /* already removed */ } }

async function waitForBroker(timeoutMs = 5000): Promise<void> {
	const start = Date.now();
	while (Date.now() - start < timeoutMs) {
		if (await checkSocketConnectable()) return;
		await sleep(100);
	}
	throw new Error("Agency broker failed to start within timeout");
}
