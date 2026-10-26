# CMX — Customer & Market Experience

**Product direction:** Decision-to-Outcome Intelligence for businesses.

CMX helps a person or organization move from an unclear need to a defensible decision, an approved implementation and a measured outcome. It is designed to be vendor-neutral: it may recommend improving an existing process, buying software, automating a workflow, using AI, building a custom solution, seeking expert help, or taking no action.

> **Principle:** understand the need before recommending a solution; verify before asserting; obtain authorization before acting; measure outcomes after implementation.

## Initial focus

The first product increment is a small, explainable business-decision engine—not a universal marketplace or a new foundation model. It accepts a structured problem brief, records constraints and unknowns, compares candidate approaches using explicit criteria, and returns a recommendation with rationale, assumptions, risks and next questions.

The first target market is small and midsize businesses evaluating operational workflow improvements, software, automation and AI. The exact first workflow must be validated with prospective customers before the product is expanded.

## Architecture principles

- **Model-independent:** LLMs are optional capability providers, not the source of truth for deterministic rules.
- **Evidence-aware:** distinguish user inputs, verified facts, assumptions, estimates and unknowns.
- **Explainable:** show criteria, weights, trade-offs and why an option is eligible.
- **Fail safely:** do not invent prices, vendor availability, savings or integration support.
- **Human-controlled:** approval is required before consequential external actions.
- **Outcome-oriented:** record objectives and success criteria so results can later be measured.
- **Privacy-first:** collect only necessary data and keep tenant isolation and authorization as core requirements for any production deployment.

## Development status

CMX is at the initial engineering foundation stage. Any capability not implemented and tested in this repository must be treated as planned, not available. Live vendor data, LLM providers, authentication, multi-tenancy, advertising, marketplace payments and quantum-computing integrations are not part of the initial core.

## Intended first increment

1. Validated task and decision models.
2. Deterministic candidate eligibility and transparent weighted scoring.
3. Structured API endpoints for assessments and recommendations.
4. Automated tests for constraints, weights, missing evidence and invalid input.
5. Architecture notes and a staged roadmap.

## Local development

The initial API will target Python 3.11+ and FastAPI. Setup and test commands will be documented as implementation lands.

## Naming

**CMX** means **Customer & Market Experience**. This is the chosen working brand; legal, trademark and domain clearance remain separate tasks.
