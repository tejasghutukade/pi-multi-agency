export type AgencyReportInput = {
	status?: "succeeded" | "failed";
	summary?: string;
	output?: string;
	artifacts?: Record<string, string>;
	error?: string;
	payloadJson?: string;
};

/** Build either the caller's exact JSON payload or a compact parameter payload. */
export function buildAgencyReportPayload(input: AgencyReportInput): Record<string, unknown> {
	if (input.payloadJson) return JSON.parse(input.payloadJson) as Record<string, unknown>;
	return Object.fromEntries(
		Object.entries({
			status: input.status,
			summary: input.summary,
			output: input.output,
			artifacts: input.artifacts,
			error: input.error,
		}).filter(([, value]) => value !== undefined),
	);
}

export function isPipelineRunnerTarget(preflight: unknown): boolean {
	if (!preflight || typeof preflight !== "object") return false;
	const instance = (preflight as { instance?: unknown }).instance;
	return Boolean(
		instance
		&& typeof instance === "object"
		&& (instance as { role?: unknown }).role === "pipeline-runner",
	);
}
