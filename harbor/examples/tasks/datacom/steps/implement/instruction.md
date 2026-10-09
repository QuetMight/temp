You are the **implementing engineer** for the `datacom` project.

The specification is at **`/app/spec.md`** and the implementation plan is at
**`/app/plan.md`**. Read both before writing code — they are the authority for
what to build and how.

## Your task

Implement the plan in `/app`.

1. Work through the work items in `plan.md` in their stated order.
2. Build the project and run the tests you can run locally.
3. Update `/app/plan.md` only where reality diverged from the plan, and record
   the divergence with a short reason. Do not rewrite the plan to match
   whatever you happened to build.
4. Leave the repository in a state where a fresh checkout builds and tests pass
   with the documented commands.

## Rules

- Implement against **`spec.md`**, not against your own idea of what would be
  nicer. If a requirement is unimplementable as written, say so in the final
  message and state the closest behaviour you implemented.
- Every requirement you claim to satisfy must be covered by a test that fails
  without your change.
- Keep the documented build and test commands up to date with what actually
  works.
- Do not weaken or delete existing tests to make things pass.
- When you are done, report: what you implemented, what you deliberately did
  not implement, the exact build command, and the exact test command.
