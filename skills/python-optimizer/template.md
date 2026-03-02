# Python Optimizer Report Template

## Output Location
Write your report to: `.vnx-data/unified_reports/`

## Filename Format
`{timestamp}-{track}-optimization-{title}.md`

Example: `20260202-143000-A-optimization-memory-reduction.md`

## Report Structure

### Summary
Brief overview of optimization work completed (2-3 sentences)

### Tags (Required)
- [tag1, tag2, tag3]  # Use specific, compound tags (e.g., sse-streaming, browser-pool, kvk-validation). Avoid general-only tags.

### Performance Baseline
- Memory usage before optimization
- Execution time before optimization
- Profiling methodology used

### Optimizations Applied
- Code changes made with rationale
- Algorithm improvements
- Memory reduction techniques
- Async/concurrency improvements

### Results
- Memory usage after (with % reduction)
- Execution time after (with speedup factor)
- Before/after benchmark comparison

### Evidence
- Profiling output (cProfile, memory_profiler)
- Benchmark results
- Code snippets showing changes
- Test output confirming correctness

### Open Items
<!-- List any unfinished work, blockers, or issues discovered -->
<!-- Format: - [ ] [severity] Title (optional details) -->
<!-- Severities: [blocker], [warn], [info] -->
<!-- Leave empty if all work completed -->

### Recommendations
- Further optimization opportunities
- Trade-offs to consider
- Monitoring suggestions for regression detection

### Intelligence Feedback
<!-- If your dispatch included [INTELLIGENCE_DATA] with offered_pattern_hashes, report which patterns you actually used: -->
used_pattern_hashes: []

## Quality Standards
- 30%+ memory reduction target
- 2x+ speed improvement goal
- Maintain code readability
- Include benchmark results
