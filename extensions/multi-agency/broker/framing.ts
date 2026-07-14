import type { Socket } from "node:net";

// Adapted from pi-intercom (MIT, Copyright (c) 2026 Nico Bailon).
// Protocol: 4-byte big-endian payload length + UTF-8 JSON payload.
export const MAX_FRAME_BYTES = 1024 * 1024;

export function writeMessage(socket: Socket, msg: unknown): void {
	const json = JSON.stringify(msg);
	const payload = Buffer.from(json, "utf-8");
	const header = Buffer.alloc(4);
	header.writeUInt32BE(payload.length, 0);
	socket.write(Buffer.concat([header, payload]));
}

export function createMessageReader(
	onMessage: (msg: unknown) => void,
	onError: (error: Error) => void,
	maxFrameBytes = MAX_FRAME_BYTES,
): (data: Buffer) => void {
	let buffer = Buffer.alloc(0);

	function reportMessage(payload: Buffer): boolean {
		let msg: unknown;
		try {
			msg = JSON.parse(payload.toString("utf-8"));
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			onError(new Error(`Failed to parse agency broker message: ${message}`, { cause: error }));
			return false;
		}

		try {
			onMessage(msg);
			return true;
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			onError(new Error(`Failed to handle agency broker message: ${message}`, { cause: error }));
			return false;
		}
	}

	return (data: Buffer) => {
		let remaining = data;
		while (remaining.length > 0) {
			if (buffer.length < 4) {
				const headerBytes = Math.min(4 - buffer.length, remaining.length);
				buffer = Buffer.concat([buffer, remaining.subarray(0, headerBytes)]);
				remaining = remaining.subarray(headerBytes);
				if (buffer.length < 4) return;
			}

			const length = buffer.readUInt32BE(0);
			if (length > maxFrameBytes) {
				buffer = Buffer.alloc(0);
				onError(new Error(`Agency broker frame length ${length} exceeds maximum ${maxFrameBytes} bytes`));
				return;
			}

			const currentPayloadBytes = Math.max(0, buffer.length - 4);
			const missingPayloadBytes = length - currentPayloadBytes;
			const payloadBytes = Math.min(missingPayloadBytes, remaining.length);
			buffer = Buffer.concat([buffer, remaining.subarray(0, payloadBytes)]);
			remaining = remaining.subarray(payloadBytes);

			if (buffer.length < 4 + length) return;
			const payload = buffer.subarray(4, 4 + length);
			buffer = buffer.subarray(4 + length);
			if (!reportMessage(payload)) return;
		}
	};
}
