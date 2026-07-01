# Provenance Guard

Provenance Guard is a Flask backend that a creative writing platform could plug into to classify submitted text, return a confidence score, show a reader-facing transparency label, and support creator appeals.

The project intentionally treats AI detection as uncertain. It uses multiple signals, exposes each signal in the audit log, and routes contested decisions to `under_review` instead of pretending the automated result is final.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional Groq support:

```bash
GROQ_API_KEY=your_key_here
```

If `GROQ_API_KEY` is not set, the service uses a deterministic local semantic fallback so the app can still be run and graded.

Run the server:

```bash
python3 -m flask --app app run --port 5000
```

## API

### `POST /submit`

Accepts text and a creator ID:

```json
{
  "text": "The submitted poem, story, or blog excerpt...",
  "creator_id": "creator-123"
}
```

Returns a `content_id`, attribution result, AI-likelihood confidence score, transparency label, both signal outputs, and status.

### `POST /appeal`

Accepts a previous `content_id` and creator reasoning:

```json
{
  "content_id": "d73678d1-5ba7-4963-8744-e6fe8a114785",
  "creator_reasoning": "I wrote this myself as a formal class-style paragraph."
}
```

Updates the content status to `under_review` and records an appeal event in the audit log.

### `GET /log`

Returns recent structured audit log entries.

## Architecture Overview

A submission flows through validation, an LLM-style semantic signal, stylometric heuristics, weighted scoring, label generation, SQLite persistence, and structured audit logging. Appeals look up the original decision, update the content status, and write the creator's reasoning alongside the original classification.

See [planning.md](/Users/averieahn/Desktop/codepath/ai201-project4-provenance-guard/planning.md) for the full architecture diagram and implementation spec.

## Detection Signals

| Signal | What it measures | Why it was chosen | Blind spot |
| --- | --- | --- | --- |
| LLM semantic assessment | Holistic AI-likeness: generic phrasing, formulaic transitions, overly balanced prose, lack of concrete personal detail. | It can notice meaning and rhetoric that simple statistics miss. | Polished human work, non-native English, or formal classroom writing may look AI-like. |
| Stylometric heuristics | Sentence length variance, vocabulary diversity, punctuation density, contractions, and first-person markers. | It is deterministic, inspectable, and independent from the LLM-style signal. | Poems, short excerpts, academic prose, and heavily edited drafts can distort the metrics. |

The app uses Groq `llama-3.3-70b-versatile` when `GROQ_API_KEY` is present. Without a key, `local_semantic_fallback` keeps the endpoint testable.

## Confidence Scoring

Each signal returns an AI-likelihood score from `0.0` to `1.0`. The combined score is:

```text
combined_ai_score = (0.60 * llm_score) + (0.40 * stylometric_score)
```

Thresholds:

| Score range | Attribution |
| --- | --- |
| `0.00` to `0.34` | `likely_human` |
| `0.35` to `0.71` | `uncertain` |
| `0.72` to `1.00` | `likely_ai` |

The uncertain band is intentionally wide because a false positive against a human writer is more harmful than failing to flag an AI-assisted piece.

Example verification results from local testing:

| Example | LLM score | Stylometric score | Combined confidence | Result |
| --- | ---: | ---: | ---: | --- |
| Formulaic AI-style paragraph | `0.82` | `0.608` | `0.735` | `likely_ai` |
| Casual ramen review | `0.08` | `0.442` | `0.225` | `likely_human` |
| Formal policy paragraph | `0.61` | `0.664` | `0.632` | `uncertain` |
| Lightly edited remote-work paragraph | `0.55` | `0.649` | `0.590` | `uncertain` |

## Transparency Labels

The exact label variants returned by the API are:

| Variant | Exact text |
| --- | --- |
| High-confidence AI | "Provenance Guard: This piece shows strong signs of AI-generated writing. The score is high, but this is not a final judgment; the creator can appeal if they believe it is wrong." |
| High-confidence human | "Provenance Guard: This piece shows strong signs of human authorship. The score suggests low AI involvement, though no automated review can prove origin with certainty." |
| Uncertain | "Provenance Guard: The origin of this piece is uncertain. The signals are mixed, so readers should treat the label as context rather than a verdict." |

## Appeals Workflow

Creators submit appeals with `content_id` and `creator_reasoning`. The system updates the stored content row to `under_review`, keeps the creator's explanation, and writes an `appeal_received` audit event containing the original attribution and scores.

Verified appeal response:

```json
{
  "content_id": "d73678d1-5ba7-4963-8744-e6fe8a114785",
  "creator_id": "test-formal",
  "message": "Appeal received and queued for human review.",
  "status": "under_review"
}
```

## Rate Limiting

`POST /submit` is limited to:

```text
10 per minute;100 per day
```

The minute limit allows a writer to test several drafts without friction while blocking rapid automated flooding. The daily limit is generous for normal creator behavior but still caps abuse during a classroom/demo deployment. The limiter uses `memory://` storage for local development.

Rate-limit evidence from a burst test after restarting the dev server:

```text
201
201
201
201
201
201
201
201
201
201
429
429
```

The app returns `201` for successful created classifications and `429` after the limit is exceeded.

## Audit Log

Audit data is stored in SQLite tables for `content` and `audit_log`. Every classification records timestamp, content ID, creator ID, attribution, combined confidence, both signal scores, signal metadata, and status. Appeals add the creator reasoning and move the status to `under_review`.

Sample `GET /log?limit=6` entries:

```json
[
  {
    "event_type": "appeal_received",
    "content_id": "d73678d1-5ba7-4963-8744-e6fe8a114785",
    "creator_id": "test-formal",
    "attribution": "uncertain",
    "confidence": 0.632,
    "llm_score": 0.61,
    "stylometric_score": 0.664,
    "status": "under_review",
    "appeal_reasoning": "I wrote this myself as a formal class-style paragraph, so the polished tone may look more uniform than my casual writing."
  },
  {
    "event_type": "classification",
    "content_id": "5877b14f-99a2-4367-b4a4-94f013b1050b",
    "creator_id": "test-ai",
    "attribution": "likely_ai",
    "confidence": 0.735,
    "llm_score": 0.82,
    "stylometric_score": 0.608,
    "signals_used": ["llm_semantic_assessment", "stylometric_heuristics"],
    "status": "classified"
  },
  {
    "event_type": "classification",
    "content_id": "6a9bc908-2345-485d-bec6-64a12483f6f9",
    "creator_id": "test-human",
    "attribution": "likely_human",
    "confidence": 0.225,
    "llm_score": 0.08,
    "stylometric_score": 0.442,
    "signals_used": ["llm_semantic_assessment", "stylometric_heuristics"],
    "status": "classified"
  }
]
```

## Known Limitations

Formal human essays are the clearest risk. Their long sentences, low contraction count, and polished transitions can look AI-like to both signals, so the system may push them into the uncertain band or occasionally too high. Repetitive poems are another weak case because deliberate repetition can look like low sentence variance rather than artistic style.

In a production deployment, I would add reviewer outcomes back into calibration data, use authenticated log access, and store rate limits in Redis instead of memory.

## Spec Reflection

Writing `planning.md` first made the thresholds and label text concrete before implementation, which kept the endpoint response and README consistent. The main implementation divergence is the local semantic fallback: the spec centers Groq as the first signal, but the fallback makes the project runnable without an API key and keeps grading independent from network/API availability.

## AI Usage

1. I directed AI assistance to turn the project brief into a concrete `planning.md` with architecture, signal definitions, thresholds, label variants, appeal workflow, and an implementation plan. I revised the thresholds to use a wider uncertain band because false positives against human writers are especially harmful.
2. I directed AI assistance to generate the Flask implementation for `/submit`, `/appeal`, `/log`, weighted scoring, labels, SQLite audit logging, and Flask-Limiter setup. I revised the local semantic scoring after testing because the clearly AI sample initially landed in the uncertain band.

## Walkthrough Notes

For the short portfolio walkthrough, show:

1. `planning.md` architecture diagram.
2. A `POST /submit` that returns `likely_ai`.
3. A `POST /submit` that returns `likely_human`.
4. A `POST /appeal` changing a content item to `under_review`.
5. `GET /log` showing classification and appeal events.
6. The rate-limit burst producing `429`.
