# Provenance Guard Planning

## Problem Frame

Provenance Guard is a backend service for creative platforms that want to give readers context about whether submitted writing appears human-written, AI-generated, or uncertain. The system should avoid pretending that AI detection is perfect. A false positive against a human writer is especially harmful, so the design gives uncertain cases a neutral label and a clear appeal path.

## Detection Signals

### Signal 1: LLM semantic assessment

- **What it measures:** A Groq-hosted Llama model is prompted to judge whether the text reads like AI-generated writing, based on semantic flow, generic phrasing, hedging, and overall polish.
- **Output shape:** A JSON-like result with `score` from `0.0` to `1.0`, where higher means more likely AI-generated, plus a short rationale.
- **Why it helps:** An LLM can notice holistic cues that are difficult to capture with simple statistics, such as generic topic development or formulaic transitions.
- **Blind spots:** It may over-trust polished human prose, misread non-native English, or be biased toward common internet writing patterns. If `GROQ_API_KEY` is absent, the app uses a deterministic local semantic fallback so the project remains runnable.

### Signal 2: Stylometric heuristics

- **What it measures:** Structural properties of the text: sentence length variance, type-token ratio, punctuation density, contractions, first-person markers, and formulaic transition phrases.
- **Output shape:** A score from `0.0` to `1.0`, where higher means the structure resembles uniform AI-generated prose.
- **Why it helps:** AI-written passages often have steady sentence rhythm, broad but generic vocabulary, and balanced punctuation. Human drafts are frequently more uneven.
- **Blind spots:** Short texts, formal essays, intentionally repetitive poems, and edited human writing can all look uniform. Casual AI writing can also imitate human irregularity.

### Combined scoring

Both signals produce AI-likelihood scores. The combined score is:

`combined_ai_score = (0.60 * llm_score) + (0.40 * stylometric_score)`

The LLM signal gets more weight because it captures broader context. Stylometrics still gets substantial weight because it is deterministic, inspectable, and independent.

## Uncertainty Representation

The public `confidence` value is an AI-likelihood score, not an absolute truth claim.

- `0.00` to `0.34`: likely human-written.
- `0.35` to `0.71`: uncertain.
- `0.72` to `1.00`: likely AI-generated.

A score near `0.50` means the system sees mixed evidence and should not make a strong attribution claim. The uncertain band is intentionally wide because accusing a human creator of using AI is a high-cost false positive. A score of `0.95` should produce a stronger AI label than `0.51`; `0.51` stays uncertain.

## Transparency Label Design

| Variant | Exact label text |
| --- | --- |
| High-confidence AI | "Provenance Guard: This piece shows strong signs of AI-generated writing. The score is high, but this is not a final judgment; the creator can appeal if they believe it is wrong." |
| High-confidence human | "Provenance Guard: This piece shows strong signs of human authorship. The score suggests low AI involvement, though no automated review can prove origin with certainty." |
| Uncertain | "Provenance Guard: The origin of this piece is uncertain. The signals are mixed, so readers should treat the label as context rather than a verdict." |

## Appeals Workflow

Any creator with a `content_id` can submit an appeal. The appeal request includes:

- `content_id`: the submission being contested.
- `creator_reasoning`: the creator's explanation.

When an appeal arrives, the system:

1. Looks up the original content record.
2. Updates its status from `classified` to `under_review`.
3. Stores the appeal reasoning with the content.
4. Writes an `appeal_received` event to the audit log with the original decision and the appeal text.
5. Returns a confirmation payload that a human reviewer could use to open the appeal queue.

A human reviewer would see the content ID, creator ID, original attribution, confidence score, both signal scores, current status, appeal reasoning, and timestamps.

## Anticipated Edge Cases

- **Formal human essays:** Academic or policy writing can contain polished phrasing, low contractions, and even sentence lengths. The stylometric signal may push these toward AI even when they are human.
- **Poetry with repetition:** A poem that intentionally repeats simple lines may look statistically uniform, causing the stylometric signal to over-score AI likelihood.
- **Very short submissions:** A two-sentence excerpt does not provide enough structure for reliable sentence variance or vocabulary diversity.
- **Non-native English:** Careful, formal wording by a multilingual writer may resemble generic AI prose. The wide uncertain band and appeal workflow are meant to reduce harm in this case.

## API Surface

- `POST /submit`
  - Accepts: `{ "text": "...", "creator_id": "..." }`
  - Returns: `content_id`, `creator_id`, `attribution`, `confidence`, transparency `label`, individual signal results, and `status`.
- `POST /appeal`
  - Accepts: `{ "content_id": "...", "creator_reasoning": "..." }`
  - Returns: appeal confirmation and `status: "under_review"`.
- `GET /log`
  - Returns recent structured audit log entries.
- `GET /health`
  - Returns a simple service health response.

## Architecture

```text
Submission flow

POST /submit
  | raw text + creator_id
  v
Request validation
  | normalized text
  v
Signal 1: LLM semantic assessment
  | llm_score + rationale
  v
Signal 2: stylometric heuristics
  | stylometric_score + metrics
  v
Confidence scoring
  | combined AI-likelihood score
  v
Transparency label generator
  | attribution + label text
  v
SQLite content store + audit log
  | structured decision event
  v
JSON response to platform

Appeal flow

POST /appeal
  | content_id + creator_reasoning
  v
Content lookup
  | original decision
  v
Status update to under_review
  | appeal details
  v
SQLite audit log
  | structured appeal event
  v
JSON confirmation
```

The submission path validates text, runs two independent signals, combines them into an AI-likelihood score, maps that score to a reader-facing label, stores the classification, and records a structured audit event. The appeal path reopens a stored classification for human review and logs the creator's reasoning next to the original decision.

## AI Tool Plan

### M3: submission endpoint + first signal

- **Spec sections to provide:** Detection Signals, API Surface, Architecture.
- **Ask:** Generate a Flask skeleton with `POST /submit`, a `content_id`, an audit helper, and the LLM semantic signal function.
- **Verification:** Run the route with a hardcoded response first, then call the first signal directly with clearly AI and casual human examples before wiring it into `/submit`.

### M4: second signal + confidence scoring

- **Spec sections to provide:** Detection Signals, Uncertainty Representation, Architecture.
- **Ask:** Generate a stylometric scoring function and a weighted scoring function matching the thresholds above.
- **Verification:** Test at least four inputs: clearly AI, clearly human, formal human, and lightly edited AI. Print both signal scores to check whether differences are meaningful.

### M5: production layer

- **Spec sections to provide:** Transparency Label Design, Appeals Workflow, Architecture.
- **Ask:** Generate label mapping logic, `POST /appeal`, complete audit fields, and Flask-Limiter setup.
- **Verification:** Confirm all three label variants are reachable, appeal updates status to `under_review`, `GET /log` shows submissions and appeals, and rapid submissions trigger HTTP `429`.
