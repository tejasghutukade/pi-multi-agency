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

test("report payload supports needs_attention status with question and options", () => {
	// Regression for review finding #17: the typed tool could not emit
	// `needs_attention`, so specialists could never ask the operator a question.
	const payload = buildAgencyReportPayload({
		status: "needs_attention",
		summary: "ambiguous scope",
		artifacts: { primary: "out/draft.md" },
		question: "which approach?",
		options: ["A", "B"],
	});
	assert.deepEqual(payload, {
		status: "needs_attention",
		summary: "ambiguous scope",
		artifacts: { primary: "out/draft.md" },
		question: "which approach?",
		options: ["A", "B"],
	});
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
