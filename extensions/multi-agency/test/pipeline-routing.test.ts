import assert from "node:assert/strict";
import { test } from "node:test";
import { buildAgencyReportPayload, isPipelineRunnerTarget } from "../pipeline-routing.ts";

test("report payload omits undefined fields and keeps structured fields", () => {
	const payload = buildAgencyReportPayload({
		status: "failed",
		summary: "partial output",
		artifacts: { primary: "out/result.md" },
		error: "stage failed",
	});
	assert.deepEqual(payload, {
		status: "failed",
		summary: "partial output",
		artifacts: { primary: "out/result.md" },
		error: "stage failed",
	});
	assert.equal(Object.values(payload).includes(undefined), false);
});

test("report payload preserves legacy output without undefined keys", () => {
	assert.deepEqual(buildAgencyReportPayload({ output: "ordinary result" }), {
		output: "ordinary result",
	});
});

test("payloadJson remains the exact payload source", () => {
	assert.deepEqual(
		buildAgencyReportPayload({
			status: "succeeded",
			summary: "ignored",
			payloadJson: '{"status":"failed","summary":"json","artifacts":{},"error":"nope"}',
		}),
		{ status: "failed", summary: "json", artifacts: {}, error: "nope" },
	);
});

test("pipeline runner routing requires the exact preflight instance role", () => {
	assert.equal(isPipelineRunnerTarget({ instance: { role: "pipeline-runner" } }), true);
	assert.equal(isPipelineRunnerTarget({ instance: { role: "scout" } }), false);
	assert.equal(isPipelineRunnerTarget({ role: "pipeline-runner" }), false);
	assert.equal(isPipelineRunnerTarget(null), false);
});
