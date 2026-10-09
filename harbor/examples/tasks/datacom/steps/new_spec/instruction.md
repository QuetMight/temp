You are the **specification author** for the `datacom` project.

`datacom` is a data-communication component: it accepts records on an ingress
interface, validates and normalises them, and forwards them to a downstream
sink with well-defined delivery guarantees.

## Your task

Write the specification for the next iteration of `datacom` to
**`/app/spec.md`**.

The specification must contain, in this order, these sections:

1. **Scope and goals** — what this iteration delivers and why.
2. **Interface contract** — ingress and egress surfaces, the message schema
   (field names, types, optionality, units), and the error model.
3. **Behavioural rules** — ordering guarantees, retry and backoff policy,
   idempotency / de-duplication, backpressure, shutdown behaviour.
4. **Non-functional requirements** — throughput and latency targets with the
   conditions they are measured under, memory ceiling, failure recovery
   expectations.
5. **Non-goals** — what is explicitly out of scope for this iteration.
6. **Open questions** — anything you had to assume.

## Rules

- Requirements must be **testable**: state the observable behaviour and the
  conditions, not the implementation. A reader must be able to derive a test
  from each requirement.
- Where the existing behaviour of `datacom` is discoverable from the
  repository, inspect it first and stay consistent with it; call out any
  intentional divergence.
- Prefer concrete values (numbers, units, limits) over adjectives. When you
  cannot pick a value, state the constraint on it and list it under open
  questions.
- Do **not** write an implementation plan in this step.
- Do **not** modify any implementation code in this step.

Keep the document self-contained and readable on its own.
