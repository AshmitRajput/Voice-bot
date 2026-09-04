"""
voice_bot/intent.py

9 intents — har intent ek alag BOT ACTION hai.
Filler pehle STATE se aata hai (zero latency), warna intent se.

    booking        -> slot flow chalao
    query_general  -> RAG se jawab do
    complaint      -> ticket banao
    callback       -> time note karo
    upset          -> tone naram, escalate
    off_topic      -> deflect karo
    greeting       -> opening
    call_end       -> call band karo
    generic        -> LLM ko context ke saath do

Setup:
    pip install onnxruntime transformers scikit-learn joblib numpy

Usage:
    from voice_bot.intent import detect_intent, get_filler

    # state pata ho to classifier bypass karo — 0ms:
    if state == "awaiting_time":
        filler = get_filler(state=state)
    else:
        r = detect_intent(text, state=state)
        filler = r["filler"]
"""

import logging
import os
import random
import threading

import joblib
import numpy as np
import onnxruntime as ort
from transformers import AutoTokenizer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- config

MODEL_DIR = os.environ.get(
    "INTENT_MODEL_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ml_models"),
)
MAX_LENGTH = 64

THRESHOLD = float(os.environ.get("INTENT_THRESHOLD", "0.40"))
FALLBACK_INTENT = "generic"

RAG_INTENTS = {"query_general", "complaint"}

# ------------------------------------------------------------ fillers
# STATE filler intent filler ko override karta hai.
# Jab bot ko pata hai usne abhi kya poocha, guess karne ki zarurat nahi.

FILLER_BY_STATE = {
    "opening":               ["नमस्ते जी...", "जी नमस्ते..."],
    "awaiting_name":         ["जी...", "जी बताइए..."],
    "awaiting_date":         ["जी, देखती हूँ...", "एक सेकंड..."],
    "awaiting_time":         ["जी, चेक करती हूँ...", "एक पल रुकिए..."],
    "awaiting_confirmation": ["ठीक है जी...", "जी..."],
    "awaiting_vehicle":      ["जी...", "अच्छा जी..."],
    "reading_slots":         ["एक पल...", "जी, देखती हूँ..."],
}

FILLER_BY_INTENT = {
    "booking":       ["जी, देखती हूँ...", "ठीक है, एक पल...", "{name} जी, चेक करती हूँ..."],
    "query_general": ["जी, बताती हूँ...", "एक सेकंड, देखती हूँ..."],
    "complaint":     ["जी, समझ गई...", "{name} जी, माफ़ी चाहती हूँ...", "जी, देखती हूँ इसे..."],
    "callback":      ["बिल्कुल जी...", "जी, नोट कर लेती हूँ...", "{name} जी, ज़रूर..."],
    "upset":         ["जी, समझ रही हूँ...", "{name} जी, खेद है...", "माफ़ कीजिए जी..."],
    "off_topic":     ["जी...", "जी, काम की बात करते हैं..."],
    "greeting":      ["नमस्ते जी...", "जी नमस्ते, बताइए..."],
    "call_end":      ["जी, धन्यवाद! नमस्ते।", "ठीक है जी, नमस्ते।"],
    "generic":       ["जी...", "जी हाँ...", "जी, बताइए..."],
}

DEFAULT_FILLER = "जी..."


def get_filler(intent=None, state=None, customer_name=None):
    """State pehle, phir intent. Koi model call nahi — instant."""
    options = None
    if state:
        options = FILLER_BY_STATE.get(state)
    if not options and intent:
        options = FILLER_BY_INTENT.get(intent)
    if not options:
        options = FILLER_BY_INTENT[FALLBACK_INTENT]

    if not customer_name:
        options = [o for o in options if "{name}" not in o] or [DEFAULT_FILLER]

    return random.choice(options).replace("{name}", customer_name or "").strip()


# ---------------------------------------------------------------- engine

class _Engine:
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        onnx_path = os.path.join(MODEL_DIR, "model_quantized.onnx")
        if not os.path.exists(onnx_path):
            onnx_path = os.path.join(MODEL_DIR, "model.onnx")
        if not os.path.exists(onnx_path):
            raise FileNotFoundError(f"ONNX model nahi mila: {MODEL_DIR}")

        self.tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(os.environ.get("INTENT_ONNX_THREADS", "1"))
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self.session = ort.InferenceSession(
            onnx_path, sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.input_names = {i.name for i in self.session.get_inputs()}

        self.clf = joblib.load(os.path.join(MODEL_DIR, "classifier.joblib"))
        self.labels = list(
            joblib.load(os.path.join(MODEL_DIR, "label_encoder.joblib")).classes_
        )

        self._embed(["warmup"])
        logger.info("Intent model loaded: %s", self.labels)

    def _embed(self, texts):
        enc = self.tokenizer(
            texts, padding=True, truncation=True,
            max_length=MAX_LENGTH, return_tensors="np",
        )
        inputs = {k: v for k, v in enc.items() if k in self.input_names}
        hidden = self.session.run(None, inputs)[0]

        mask = enc["attention_mask"][..., None].astype(np.float32)
        summed = (hidden * mask).sum(axis=1)
        counts = np.clip(mask.sum(axis=1), 1e-9, None)
        return summed / counts

    def probs(self, texts):
        return self.clf.predict_proba(self._embed(list(texts)))


# ---------------------------------------------------------------- api

def _empty(text="", state=None, customer_name=None):
    return {
        "text": text,
        "intent": FALLBACK_INTENT,
        "raw_intent": None,
        "confidence": 0.0,
        "fallback": True,
        "needs_rag": False,
        "filler": get_filler(FALLBACK_INTENT, state, customer_name),
        "top_k": [],
    }


def _build(text, row, top_k, state, customer_name):
    labels = _Engine.get().labels
    order = np.argsort(row)[::-1]

    best = int(order[0])
    raw_intent = labels[best]
    confidence = float(row[best])
    fallback = confidence < THRESHOLD
    intent = FALLBACK_INTENT if fallback else raw_intent

    return {
        "text": text,
        "intent": intent,
        "raw_intent": raw_intent,
        "confidence": round(confidence, 4),
        "fallback": fallback,
        "needs_rag": intent in RAG_INTENTS,
        "filler": get_filler(intent, state, customer_name),
        "top_k": [
            {"intent": labels[int(i)], "confidence": round(float(row[i]), 4)}
            for i in order[:top_k]
        ],
    }


def detect_intent(text, state=None, customer_name=None, top_k=3):
    """Ek utterance -> intent dict. Kabhi raise nahi karta."""
    text = (text or "").strip()
    if not text:
        return _empty(state=state, customer_name=customer_name)

    try:
        row = _Engine.get().probs([text])[0]
        return _build(text, row, top_k, state, customer_name)
    except Exception:
        logger.exception("Intent detection failed")
        return _empty(text, state, customer_name)


def detect_intent_batch(texts, state=None, customer_name=None, top_k=3):
    texts = [(t or "").strip() for t in texts]
    try:
        rows = _Engine.get().probs(texts)
        return [_build(t, r, top_k, state, customer_name)
                for t, r in zip(texts, rows)]
    except Exception:
        logger.exception("Batch intent detection failed")
        return [detect_intent(t, state, customer_name, top_k) for t in texts]


def warmup():
    _Engine.get()


def labels():
    return list(_Engine.get().labels)