import { mkdirSync, rmSync } from "node:fs";
import net from "node:net";
import { join } from "node:path";
import { createMessageReader, writeMessage } from "../broker/framing.ts";
import { AGENCY_BROKER_PROTOCOL_NAME, AGENCY_BROKER_PROTOCOL_VERSION, requireBrokerContext, resolveBrokerContext } from "../broker/paths.ts";
import { checkSocketConnectable } from "../broker/spawn.ts";

const agentDir = process.env.PI_CODING_AGENT_DIR!;
const project = process.env.TEST_AGENCY_PROJECT!;
const legacyDir = join(agentDir, "agency-broker");
const legacySocket = join(legacyDir, "broker.sock");
mkdirSync(legacyDir, { recursive: true });
let connections = 0;
const server = net.createServer((socket) => {
	connections++;
	const reader = createMessageReader((message) => {
		if (typeof message !== "object" || message === null) return;
		const requestId = (message as { requestId?: unknown }).requestId;
		if ((message as { type?: unknown }).type === "health" && typeof requestId === "string") {
			writeMessage(socket, { type: "health_ok", requestId, protocol: AGENCY_BROKER_PROTOCOL_NAME, version: AGENCY_BROKER_PROTOCOL_VERSION });
		}
	}, () => socket.destroy());
	socket.on("data", reader);
});

async function main(): Promise<void> {
	try {
		await new Promise<void>((resolve, reject) => {
			server.once("error", reject);
			server.listen(legacySocket, () => { server.off("error", reject); resolve(); });
		});
		const context = requireBrokerContext(resolveBrokerContext({ projectRoot: project, env: process.env }));
		const connected = await checkSocketConnectable(context);
		console.log(JSON.stringify({ connected, connections, legacySocket, selectedEndpoint: context.endpoint }));
	} finally {
		await new Promise<void>((resolve) => server.close(() => resolve()));
		rmSync(legacyDir, { recursive: true, force: true });
	}
}

void main().catch((error) => { console.error(error); process.exitCode = 1; });
