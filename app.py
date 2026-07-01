import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

try:
    from groq import Groq
except ImportError:  # pragma: no cover - requirements installs this for normal use
    Groq = None


load_dotenv()

DATABASE_PATH = os.getenv("DATABASE_PATH", "provenance_guard.db")

LABELS = {
    "likely_ai": (
        "Provenance Guard: This piece shows strong signs of AI-generated writing. "
        "The score is high, but this is not a final judgment; the creator can appeal "
        "if they believe it is wrong."
    ),
    "likely_human": (
        "Provenance Guard: This piece shows strong signs of human authorship. "
        "The score suggests low AI involvement, though no automated review can prove "
        "origin with certainty."
    ),
    "uncertain": (
        "Provenance Guard: The origin of this piece is uncertain. The signals are mixed, "
        "so readers should treat the label as context rather than a verdict."
    ),
}


def create_app(database_path=DATABASE_PATH):
    global DATABASE_PATH
    DATABASE_PATH = database_path

    app = Flask(__name__)
    app.config["DATABASE_PATH"] = database_path

    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[],
        storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    )

    init_db(database_path)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "service": "provenance-guard"})

    @app.post("/submit")
    @limiter.limit("10 per minute;100 per day")
    def submit():
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("text", "")).strip()
        creator_id = str(payload.get("creator_id", "")).strip()

        if not text:
            return jsonify({"error": "text is required"}), 400
        if not creator_id:
            return jsonify({"error": "creator_id is required"}), 400

        content_id = str(uuid.uuid4())
        llm_result = llm_semantic_signal(text)
        stylometric_result = stylometric_signal(text)
        confidence = combine_scores(llm_result["score"], stylometric_result["score"])
        attribution = classify_attribution(confidence)
        label = LABELS[attribution]
        timestamp = now_iso()

        content_record = {
            "content_id": content_id,
            "creator_id": creator_id,
            "text": text,
            "timestamp": timestamp,
            "attribution": attribution,
            "confidence": confidence,
            "llm_score": llm_result["score"],
            "llm_rationale": llm_result["rationale"],
            "llm_source": llm_result["source"],
            "stylometric_score": stylometric_result["score"],
            "stylometric_metrics": stylometric_result["metrics"],
            "label": label,
            "status": "classified",
        }
        save_content(content_record)
        write_audit_event(
            "classification",
            content_id,
            creator_id,
            {
                "attribution": attribution,
                "confidence": confidence,
                "signals_used": ["llm_semantic_assessment", "stylometric_heuristics"],
                "llm_score": llm_result["score"],
                "llm_rationale": llm_result["rationale"],
                "llm_source": llm_result["source"],
                "stylometric_score": stylometric_result["score"],
                "stylometric_metrics": stylometric_result["metrics"],
                "status": "classified",
            },
        )

        return jsonify(
            {
                "content_id": content_id,
                "creator_id": creator_id,
                "attribution": attribution,
                "confidence": confidence,
                "label": label,
                "signals": {
                    "llm_semantic_assessment": llm_result,
                    "stylometric_heuristics": stylometric_result,
                },
                "status": "classified",
            }
        ), 201

    @app.post("/appeal")
    def appeal():
        payload = request.get_json(silent=True) or {}
        content_id = str(payload.get("content_id", "")).strip()
        creator_reasoning = str(payload.get("creator_reasoning", "")).strip()

        if not content_id:
            return jsonify({"error": "content_id is required"}), 400
        if not creator_reasoning:
            return jsonify({"error": "creator_reasoning is required"}), 400

        content = get_content(content_id)
        if not content:
            return jsonify({"error": "content_id not found"}), 404

        update_appeal(content_id, creator_reasoning)
        write_audit_event(
            "appeal_received",
            content_id,
            content["creator_id"],
            {
                "attribution": content["attribution"],
                "confidence": content["confidence"],
                "llm_score": content["llm_score"],
                "stylometric_score": content["stylometric_score"],
                "appeal_reasoning": creator_reasoning,
                "status": "under_review",
            },
        )

        return jsonify(
            {
                "content_id": content_id,
                "creator_id": content["creator_id"],
                "message": "Appeal received and queued for human review.",
                "status": "under_review",
            }
        )

    @app.get("/log")
    def log():
        limit = request.args.get("limit", default=20, type=int)
        limit = max(1, min(limit, 100))
        return jsonify({"entries": get_audit_log(limit)})

    return app


def init_db(database_path=DATABASE_PATH):
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS content (
                content_id TEXT PRIMARY KEY,
                creator_id TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                attribution TEXT NOT NULL,
                confidence REAL NOT NULL,
                llm_score REAL NOT NULL,
                llm_rationale TEXT NOT NULL,
                llm_source TEXT NOT NULL,
                stylometric_score REAL NOT NULL,
                stylometric_metrics TEXT NOT NULL,
                label TEXT NOT NULL,
                status TEXT NOT NULL,
                appeal_reasoning TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                content_id TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )


def db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def round_score(value):
    return round(clamp(value), 3)


def llm_semantic_signal(text):
    api_key = os.getenv("GROQ_API_KEY")
    if api_key and Groq:
        try:
            return groq_semantic_signal(text, api_key)
        except Exception as exc:  # Keep the classroom demo reliable if the API fails.
            fallback = local_semantic_signal(text)
            fallback["rationale"] = f"Groq unavailable; used local fallback. Details: {exc}"
            return fallback

    return local_semantic_signal(text)


def groq_semantic_signal(text, api_key):
    client = Groq(api_key=api_key)
    prompt = f"""
Return only JSON with keys score and rationale.
Score from 0.0 to 1.0 where 1.0 means the writing strongly appears AI-generated.
Consider generic phrasing, formulaic transitions, excessive balance, and lack of concrete personal detail.

Text:
\"\"\"{text[:4000]}\"\"\"
"""
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": "You are a cautious provenance reviewer."},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content
    parsed = json.loads(raw)
    return {
        "score": round_score(float(parsed.get("score", 0.5))),
        "rationale": str(parsed.get("rationale", "No rationale returned."))[:500],
        "source": "groq_llama_3_3_70b",
    }


def local_semantic_signal(text):
    lowered = text.lower()
    formulaic_phrases = [
        "artificial intelligence",
        "it is important to note",
        "in conclusion",
        "furthermore",
        "moreover",
        "essential to consider",
        "stakeholders",
        "transformative",
        "paradigm shift",
        "ethical implications",
        "responsible deployment",
        "various sectors",
        "modern society",
        "on the other hand",
        "there are genuine tradeoffs",
    ]
    personal_markers = [
        "i ",
        "i'm",
        "i've",
        "my ",
        "honestly",
        "ok so",
        "friend",
        "downtown",
        "probably",
        "won't",
    ]
    phrase_hits = sum(1 for phrase in formulaic_phrases if phrase in lowered)
    personal_hits = sum(1 for marker in personal_markers if marker in lowered)
    sentences = split_sentences(text)
    avg_sentence_length = average([len(tokenize_words(sentence)) for sentence in sentences])

    score = 0.46
    score += min(0.42, phrase_hits * 0.09)
    score += 0.15 if avg_sentence_length >= 20 else 0
    score -= min(0.30, personal_hits * 0.06)
    score -= 0.08 if re.search(r"\b(WAY|LOL|ugh|honestly\?)\b", text) else 0

    return {
        "score": round_score(score),
        "rationale": (
            "Local semantic fallback based on formulaic AI-like phrases, personal detail, "
            "and casual markers."
        ),
        "source": "local_semantic_fallback",
    }


def stylometric_signal(text):
    sentences = split_sentences(text)
    words = tokenize_words(text)
    word_count = len(words)
    sentence_lengths = [len(tokenize_words(sentence)) for sentence in sentences if sentence.strip()]
    avg_sentence_length = average(sentence_lengths)
    sentence_variance = variance(sentence_lengths)
    type_token_ratio = len(set(word.lower() for word in words)) / word_count if word_count else 0
    punctuation_density = len(re.findall(r"[,:;!?-]", text)) / max(word_count, 1)
    contraction_count = len(re.findall(r"\b\w+'(?:t|re|ve|ll|d|m|s)\b", text.lower()))
    first_person_count = len(re.findall(r"\b(i|me|my|mine|we|our|us)\b", text.lower()))

    uniformity_score = 1 - min(sentence_variance / 80, 1)
    vocabulary_score = clamp((type_token_ratio - 0.58) / 0.28)
    punctuation_score = clamp((punctuation_density - 0.03) / 0.12)
    formality_score = clamp((avg_sentence_length - 10) / 18)
    human_marker_penalty = clamp((contraction_count + first_person_count) / 8)

    score = (
        0.30 * uniformity_score
        + 0.25 * vocabulary_score
        + 0.20 * formality_score
        + 0.15 * punctuation_score
        + 0.10 * (1 - human_marker_penalty)
    )

    metrics = {
        "word_count": word_count,
        "sentence_count": len(sentences),
        "avg_sentence_length": round(avg_sentence_length, 2),
        "sentence_length_variance": round(sentence_variance, 2),
        "type_token_ratio": round(type_token_ratio, 3),
        "punctuation_density": round(punctuation_density, 3),
        "contractions": contraction_count,
        "first_person_markers": first_person_count,
    }

    return {
        "score": round_score(score),
        "metrics": metrics,
        "rationale": "Stylometric score from sentence uniformity, vocabulary, punctuation, and human markers.",
    }


def split_sentences(text):
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]
    return sentences or [text.strip()]


def tokenize_words(text):
    return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)


def average(values):
    return sum(values) / len(values) if values else 0


def variance(values):
    if len(values) < 2:
        return 0
    avg = average(values)
    return sum((value - avg) ** 2 for value in values) / len(values)


def combine_scores(llm_score, stylometric_score):
    return round_score((0.60 * llm_score) + (0.40 * stylometric_score))


def classify_attribution(confidence):
    if confidence >= 0.72:
        return "likely_ai"
    if confidence <= 0.34:
        return "likely_human"
    return "uncertain"


def save_content(record):
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO content (
                content_id, creator_id, text, timestamp, attribution, confidence,
                llm_score, llm_rationale, llm_source, stylometric_score,
                stylometric_metrics, label, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["content_id"],
                record["creator_id"],
                record["text"],
                record["timestamp"],
                record["attribution"],
                record["confidence"],
                record["llm_score"],
                record["llm_rationale"],
                record["llm_source"],
                record["stylometric_score"],
                json.dumps(record["stylometric_metrics"]),
                record["label"],
                record["status"],
            ),
        )


def get_content(content_id):
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM content WHERE content_id = ?", (content_id,)).fetchone()
    return dict(row) if row else None


def update_appeal(content_id, creator_reasoning):
    with db_connection() as conn:
        conn.execute(
            "UPDATE content SET status = ?, appeal_reasoning = ? WHERE content_id = ?",
            ("under_review", creator_reasoning, content_id),
        )


def write_audit_event(event_type, content_id, creator_id, payload):
    event = {
        "timestamp": now_iso(),
        "event_type": event_type,
        "content_id": content_id,
        "creator_id": creator_id,
        **payload,
    }
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_log (timestamp, event_type, content_id, creator_id, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event["timestamp"],
                event_type,
                content_id,
                creator_id,
                json.dumps(event, sort_keys=True),
            ),
        )


def get_audit_log(limit=20):
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT payload FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
