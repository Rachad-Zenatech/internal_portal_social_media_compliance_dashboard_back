# Explicit compliance checking and per-rule coverage

Implemented on `feat/compliance-changes`. Incomplete coverage is visible and does not add
a submission block during benchmarking. Existing scoring, critical flags, approval
thresholds and explicitly configured human confirmations continue to apply.

## Checking methods

Each rule has a nonempty `checking_methods` array. The rule editor requires at least one:

- `TEXT_ANALYSIS`: configured wording/disclaimers, or recorded contextual text judgments.
- `VISUAL_INSPECTION`: inspection of actual image/video content; OCR is not equivalent.
- `SOURCE_VERIFICATION`: recorded claim-to-source verification.
- `CLEARANCE_DOCUMENTS`: authorization, release or clearance documentation verification.
- `HUMAN_CONFIRMATION`: deliberately assigned reviewer confirmation.

The startup migration explicitly configures all 31 baseline rules by rule number.
Existing custom rules receive a deliberate human-confirmation fallback and should be
reviewed in the rule editor. Empty phrase lists no longer assign a rule to manual review.
Administrator choices are retained on subsequent startups. New rules cannot be saved
without methods. Database constraints also reject empty/unknown method arrays.

## Results

Every applicable rule has a `rule_results` row with its ID, number, title, configured
method results, a reason, and one of the following outcomes when a judgment exists:

- No issue found
- Issue
- Evidence required
- Human review required
- Not applicable

`complete: false` and a null outcome identify an unevaluated rule/method. A known issue
can remain visible alongside incomplete methods. A partially clean result never marks
the whole rule as passed. Reasons are required and cannot be blank.

`complete` means a result was recorded; it does not mean the post passed. For example,
Human review required is a recorded result and still needs the reviewer's confirmation.
The checklist labels these as **recorded results**, never as rules checked automatically.

The existing fast checker is conservative about its coverage: a missing keyword match
is not a full contextual-policy judgment. Missing visual inspections and unverified
clearance documents remain incomplete. A normal source read cannot silently pass a
different source-dependent rule. Existing saved checks without coverage metadata show
an explicit legacy notice and can be rerun to get the new checklist.

Creators and reviewers see the checklist on post compliance screens and script checks,
including batch scripts and script prechecks. Incomplete/issue/evidence/human results
can be filtered, and each row exposes the configured methods and their reasons.

## Full-rule AI benchmark

The general checker NEVER invokes the full-rule pass. The opt-in CLI runs it in shadow
mode and cannot modify a stored check, score, approval, or rule configuration.
It is full-rule **text triage**, not a claim that images or clearance were inspected.
A clean text answer cannot complete other required methods. Missing, duplicate,
malformed, unknown-rule, and ungrounded issue answers remain incomplete.

Plan without API calls:

```powershell
python scripts/benchmark_rule_coverage.py
```

Explicitly run the three synthetic cases (normal event text, sensitive AI concept,
and a sourced factual claim):

```powershell
python scripts/benchmark_rule_coverage.py --run-ai --output .data/rule-coverage-benchmark.json
```

The pass batches eight rules per request with at most two requests in flight, and
records per-request errors and usage. Failed batches remain incomplete.
Use `--cases`, `--limit`, `--batch-size` and `--model` for a larger labelled sample or a model comparison.
The report records normal-pipeline latency, incremental full-pass latency, received
judgments, missing results, token usage, and disagreements with the fast checker.
`--input-price-per-million` and `--output-price-per-million` optionally calculate an
incremental cost estimate using rates supplied by the operator; no vendor prices are
hard-coded. Provider failures/timeouts produce incomplete results in the report.

Before rollout, benchmark a representative labelled set (including true negatives,
images/video, missing evidence, historical references and each rule). Review accuracy
and false positives, p50/p95 latency and added usage. Three smoke cases are not a
production acceptance test, and there is no automatic promotion based on this report.

## Current benchmark status

Live benchmarking is deferred at the user's request. Earlier full-rule smoke requests
returned no usable judgments; a one-rule diagnostic succeeded. Bounded batches are
implemented and tested with mocked requests, but have not been measured live. There
is no valid full-pass latency, cost or accuracy result yet. The full-rule pass remains
CLI-only and disabled for normal checks. Do not run `--run-ai` without renewed approval.

## Verification

```powershell
python -m unittest tests.test_rule_coverage tests.test_full_rule_benchmark tests.test_compliance_scoring
node .agents/check_file_sizes.mjs --staged
```

Migration idempotency and rule create/update SQL are also tested against a temporary
table with changes rolled back. Automated tests mock model requests and email delivery.
