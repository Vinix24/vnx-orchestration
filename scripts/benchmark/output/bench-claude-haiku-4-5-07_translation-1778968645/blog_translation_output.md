# Why I Don't Use LangGraph — and What I Build Instead

Two years ago I started building AI agents. The first thing everyone recommended: LangGraph.

I tried it. I understood it. And then I threw it away.

## The Fundamental Problem

LangGraph composes workers as in-process Runnable nodes. That means: no process isolation, no true parallelization, and — worst of all — no audit trail that would survive a financial audit.

In my practice, I work for organizations that are ISO- and ISAE-certified. "The model decided" is not an answer they accept. They want to know *which model*, *which version*, *which input*, *which output*, *how long*, and *who approved it*.

LangGraph doesn't give you that out of the box. You have to build it yourself — and then you're really building your own governance layer on top of a framework that wasn't designed for that.

## What I Built

VNX Orchestration is a governance-first runtime. Every dispatch gets a unique ID. Every response is recorded as an NDJSON receipt with:

```json
{
  "dispatch_id": "20260514-feat-auth-T1",
  "model": "claude-sonnet-4-6",
  "provider": "claude",
  "input_tokens": 4821,
  "output_tokens": 1203,
  "duration_seconds": 14.2,
  "exit_code": 0,
  "timestamp": "2026-05-14T09:41:22Z"
}
```

Nothing exits the chain without a receipt. Nothing gets merged without a gate turning green.

## The Tradeoff

Is it more work? Yes. VNX has 1,100+ receipts, 860+ sessions analyzed, and 11 agents in production.

But I sleep well. And so do my clients.
