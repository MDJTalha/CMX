# Phase 1 Market-Validation Kit — SME Back-Office Workflow Automation

## Evidence status (2026-10-27)

- Workflow category selected for discovery: SME back-office workflow automation.
- Customer interviews completed: 0 reported.
- Paid-pilot interest or commitment: not established.
- Customer evidence supplied: none.
- Validation decision: **unvalidated / insufficient evidence**.
- No customer quotes, names, budgets, conversion rates, savings, or demand claims are asserted in this document.

This kit is a research protocol, not evidence that CMX has product-market fit. Keep the hypothesis falsifiable and record negative findings as carefully as positive findings.

## Research question

Among small and medium-sized businesses, is there one recurring back-office workflow where a clearly defined buyer experiences a material, measurable problem and is willing to pay for a bounded pilot to improve it?

Do not assume that “automation” itself is the problem or the desired solution. Start with the person's existing process and outcomes.

## Sampling plan

Target at least five discovery interviews before making a first workflow decision. Interview people with direct responsibility for a real process and, where possible, include both:
- the person doing or supervising the work; and
- the person who controls budget or approves changes.

Seek variety in business type and workflow, but keep the final decision cohort to one specific workflow. Avoid interviewing only friends or people who already agree with the idea. Record recruitment source and any existing relationship. Five interviews is a project decision threshold, not a statistically representative sample.

Suggested roles to recruit (examples, not pre-validated segments):
- owner / general manager of an SME;
- finance or accounts lead;
- operations or administration lead;
- procurement / order-processing lead;
- HR / payroll administrator.

Do not collect sensitive employee, customer, payroll, financial-account, or commercially confidential records during discovery. Anonymize notes unless explicit consent and a genuine need justify attribution.

## 30-minute interview guide

### 1. Context (3 minutes)
- What is your role and which processes are you personally responsible for?
- Roughly how large is the team or business? (Use broad bands; do not request confidential details.)
- Which back-office process consumes the most avoidable effort in a normal week?

### 2. Reconstruct the last real occurrence (8 minutes)
- Please walk me through the last time this process happened, from trigger to completion.
- Who touched it, and which tools/files/systems were involved?
- How often does it happen and roughly how many cases/items are handled?
- Where did the last case wait, require rework, or fail?
- What was the actual consequence? What happened next?
- Which parts require human judgment or approval?

Prefer specific past events over hypothetical statements such as “Would you use an AI tool?”

### 3. Establish a baseline (5 minutes)
Record ranges when exact measures are unavailable:
- frequency and volume per week/month;
- active staff time per case and total time per period;
- delay / turnaround time;
- error, rework, missed-deadline, or exception frequency;
- direct costs or downstream consequences;
- confidence in each estimate and who supplied it.

Mark unknown values as unknown. Do not calculate savings from unsupported assumptions.

### 4. Existing alternatives and urgency (5 minutes)
- How do you handle this today?
- What have you already tried, including spreadsheets, scripts, software, outsourcing, or manual checks?
- What did it cost in money, staff time, and implementation effort?
- What would make you change the current approach? What would stop you?
- Is solving this a priority in the next 90 days? Why or why not?

### 5. Buyer, constraints, and purchasing (4 minutes)
- Who owns the problem and who can approve spending?
- What budget/process would a purchase use?
- What data access, privacy, security, audit trail, human approval, or integration requirements would be mandatory?
- What would make a small pilot unacceptable?

### 6. Test a bounded pilot (5 minutes)
Only after understanding the current problem, describe a proposed pilot as a hypothesis and ask:
- If we scoped a pilot to [specific workflow], over [duration], using [agreed inputs], and measured [baseline-to-target metric], what would need to be true for you to consider it?
- What would be a meaningful success threshold?
- Who would approve it, and what steps are required?
- Would you consider a **paid** pilot? If yes, what scope and price range could you realistically approve, and by when? If no or unsure, why?
- What is the next concrete step you are willing to take?

Do not lead the participant toward a “yes”. Do not describe a hypothetical answer as a commitment. A paid-pilot commitment must be explicit, attributable with consent, and written by the prospective buyer or documented with their permission.

## Evidence quality and note-taking

For every interview, record:
- anonymized interview ID;
- date, participant role, business-size band, recruitment source, relationship/conflict, and note-retention consent;
- workflow in the participant's own words;
- specific past example and evidence type (observed, artifact voluntarily shared, estimate, or opinion);
- baseline measures and confidence;
- current tools/workarounds and why they fall short;
- problem severity/urgency and consequences;
- buyer, approval path, timing, constraints;
- exact pilot response, proposed scope, price if volunteered, next step, and permission to retain the record;
- disconfirming evidence and unresolved questions;
- interviewer interpretation, kept separate from participant statements.

Never fabricate quotes, fill unknown fields with estimates without labeling them, or infer consent. Do not store raw confidential artifacts in this public repository. Keep sensitive raw notes privately with appropriate access controls; commit only anonymized, consented summaries.

## Workflow decision rubric

Score each candidate workflow from 0 to 2 for each dimension, citing interview IDs and evidence:
- recurrence / volume;
- measurable cost, delay, or error;
- pain and urgency;
- identifiable buyer and budget path;
- repeatability across multiple independent businesses;
- feasibility within safe, bounded automation and human approval;
- credible paid-pilot next step.

0 = evidence against or absent; 1 = mixed/weak; 2 = specific supporting evidence. A missing observation is not automatically a zero: label it “unknown” until researched. Scores summarize evidence; they do not replace judgment.

A candidate should not be called “validated” merely because it has the highest score among weak options. Before choosing one workflow, look for consistent specific past examples across multiple independent businesses, a measurable baseline, a clear buyer, manageable constraints, and a concrete paid-pilot path. Document contrary evidence and alternative explanations.

## Phase 1 exit gate

The suggested project gate is:
1. At least five completed interviews focused on one narrow workflow, with notes and consent status recorded.
2. An evidence summary that distinguishes observations, estimates, opinions, and interpretations.
3. At least one written paid-pilot commitment from an authorized buyer, **or** a documented decision to reject/revise the hypothesis and repeat discovery. A verbal “sounds interesting” is not a commitment.
4. A decision record: validate, reject, or continue testing; rationale; remaining uncertainty; next action and owner.
5. Engineering CI green on the final merge commit and a restore drill in the intended production-like environment.

The first four are customer/business evidence; software tests cannot satisfy them. If interviews remain at zero or pilot willingness remains unknown, report Phase 1 market validation as **not started / not established**, not complete.

## Outreach templates

### Short message
Hello [Name], I'm researching how SMEs handle recurring back-office work such as [specific process, only if known]. I'm not selling a product on this call; I want to understand the current process, where time or errors occur, and how teams handle them today. Would you be open to a 20–30 minute discovery conversation? I will not record or attribute your comments without permission.

### Email
Subject: Short research conversation about SME back-office workflows

Hello [Name],

I'm researching how SMEs manage recurring back-office workflows and where manual effort, delays, rework, or exceptions create a measurable problem. I'm speaking with people who are close to these processes to understand what actually happens today—not to ask for a hypothetical endorsement.

Would you be available for a 20–30 minute conversation? Participation is voluntary. I will ask permission before retaining notes and will not include your name or confidential business information in public project materials.

If this is outside your responsibilities, I'd appreciate a pointer to the person who manages the process.

Regards,
CMX project team

## Tracker

Use `market-validation-register.csv` as an empty tracker. Do not add a person or interview until contact/interview activity actually occurs. Keep personal contact details outside the public repository.
