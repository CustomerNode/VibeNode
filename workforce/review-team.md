---
id: review-team
name: Review Team
department: compose
source: vibenode
version: 1.0.0
depends_on: [compose-test-engineer, compose-quality-engineer, compose-product-manager, compose-senior-engineer]
type: prompt-template
---

# Review Team

Reusable prompt template. Invoke by typing: **review team**

## The Prompt

Run the review team: Test Engineer, Quality Engineer, Product Manager, Senior Software Engineer. All have full knowledge of the product spec and VibeNode architecture.

Review what we've been working on. You should understand what this means from our conversation. If additional context is needed, check git diff and implementation-notes.md. Run the review as a team, not four independent reports. Each agent reviews from their lane, but findings feed into a shared picture. If one agent's finding affects another's area, they coordinate.

The team fixes what it finds. Don't report problems back to me unless:
- The fix would change the spec or user-facing behavior
- The fix has a meaningful tradeoff that needs a judgment call
- The issue is ambiguous enough that two reasonable people would disagree on the right answer

Everything else, just fix it. Update the code, update the tests, update implementation-notes.md. When done, give me one combined team report: what you found, what you fixed, what (if anything) needs my input. Keep it short.
