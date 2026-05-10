# Patents — our public position

**TL;DR**: we don't patent. We publish, we trademark, and we invite forks.

## Why no patents

Cognitive Core's design — 5-layer memory, B+D orchestrator, long-poll rooms — is
a combination of engineering patterns (tiered storage, RAG summarization,
forgetting curves, pub/sub) that the field has known about for decades.

Trying to patent any of it would:

1. Fail the legal tests:
   - **US (Alice Corp v. CLS Bank)** — abstract idea without technical effect
   - **EU (EPC Art. 52(2))** — computer-implemented method without further technical effect (Hitachi T 258/03)
   - **РФ (Роспатент, ст. 1350 ГК)** — no technical character; utility models cover devices, not methods
2. Burn time and money for solo maintainers (~$25K USPTO + lawyer + maintenance × 5 years)
3. Be unenforceable against the obvious infringers (the OSS community itself, which can fork instantly under MIT)
4. Send the worst possible signal — that we'd sue contributors

## What we do instead

| Layer | Mechanism | Cost | Coverage |
|-------|-----------|------|----------|
| **Brand** | Trademark "Cognitive Core" (РФ + USPTO) | 30K ₽ + $500 + maint | Strong — can't ship clones under our name |
| **Code** | Copyright (automatic) + MIT license | 0 | Protects against verbatim copy, not reimplementation |
| **Contributor IP** | [Contributor License Agreement](../.github/CLA.md) | 0 | Lets us dual-license in the future if needed |
| **Prior art** | [White paper on memory architecture](../whitepaper/cognitive-core-memory-arch.md) | 0 | Blocks third parties from patenting our published work and suing us |
| **Quality** | Active maintenance, fast responses, real users | time | The real moat. Patents don't make a project successful. |

## Trademark

- **Cognitive Core™** is a registered trademark of the project maintainers.
- Use in editorial / educational context: free.
- Use in product branding (your fork, your SaaS, your book): ask first — `hello@cognitive-core.dev`.
- Logo files for legitimate use: `assets/logo/`.

## If you want to commercialize a derivative

You can. The MIT license permits commercial use, modification, redistribution.

Two friendly asks:
1. Don't call your derivative "Cognitive Core" — call it your own thing
2. Keep the LICENSE + NOTICE files in the source tree

If you're building a managed-SaaS competitor and want to talk partnership instead
of competition: `hello@cognitive-core.dev`.

## Defensive publication

The novel-looking parts of our design — the **5-layer memory promotion algorithm**
and the **B+D orchestrator (long-poll + LLM proxy fallback + async override)** —
are documented in detail in [`whitepaper/cognitive-core-memory-arch.md`](../whitepaper/cognitive-core-memory-arch.md).

Publishing this as prior art means:
- Nobody can later patent these techniques and sue Cognitive Core users
- The technical community has a clear reference to cite, build on, critique

If you're a researcher or working on related systems, a citation is appreciated
but not required.

## What we ask of contributors

- Sign the CLA (one-line click via [cla-assistant](https://cla-assistant.io/))
- Don't submit code under proprietary patents you don't own the rights to
- Read [`SECURITY.md`](../SECURITY.md) before reporting vulnerabilities

## What we promise contributors

- We will never sue you over your contribution
- We will never relicense the project under terms that make your contribution non-free
- If we ever dual-license a Premium edition, your contributions stay MIT in the
  open-source edition forever
