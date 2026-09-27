---
name: code-review
description: Review a diff or a set of changed files for correctness, safety, and style problems, then report findings ordered by severity.
---

# Code Review

Review code the way a careful senior engineer would: read it, understand what it
is supposed to do, then look for the ways it can be wrong.

## Procedure

1. Establish the scope. Run `git diff` (or `git diff --staged`), or review the
   specific files you were asked about. Never review a diff you have not seen.

2. Read the surrounding code, not just the changed lines. A change is correct or
   incorrect relative to its context. Use `read_file` on each touched file.

3. Check, in this order:

   **Correctness**
   - Off-by-one errors in loops, slices, and ranges.
   - Inverted or incomplete conditions; missing `else` branches.
   - Unhandled error paths: what happens when the call fails, returns empty, or
     returns `None`?
   - Resource leaks: files, sockets, locks, subprocesses.
   - Concurrency: shared mutable state without a lock; check-then-act races.
   - Boundary values: empty input, one element, very large input, unicode.

   **Safety**
   - Does anything write outside its intended directory?
   - Is untrusted input used to build a path, a shell command, or a query?
   - Are destructive operations reversible, or at least confirmed?

   **Clarity**
   - Names that lie about what the thing does.
   - Functions doing more than one job.
   - Comments that restate the code instead of explaining why.
   - Dead code and unused parameters.

   **Tests**
   - Is the new behaviour covered? Is the failure path covered?
   - Do the tests assert the right thing, or just that nothing raised?

4. Report findings ordered by severity. For each one give: the file and line, what
   is wrong, why it matters, and the smallest fix. Do not rewrite the whole file.

## Rules

- Do not report style preferences as bugs. Separate "this is wrong" from "I would
  have written it differently".
- If you are unsure whether something is a bug, say so and explain the condition
  under which it breaks. Do not guess confidently.
- If the code is fine, say so plainly. Do not invent findings to seem useful.
