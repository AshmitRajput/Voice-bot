"""
RAG Service — RecoverAI edition. Wired to KnowledgeDocument.

Design (plan doc §8-9, confirmed against real models.py):
    KnowledgeDocument.category  ->  a single Chroma collection, filtered by category
No dealer, no branch, no module. Categories are the recovery-policy
buckets defined on KnowledgeDocument.category:
    payment_policy, payment_methods, payment_link, late_payment,
    promise_to_pay, hardship, dispute, complaint, callback,
    escalation, communication_policy

We use ONE Chroma collection ("recoverai_knowledge") and filter by a
`category` metadata field at query time, rather than one collection per
category — simpler to manage, and cross-category search is still
possible later (top_k across all categories) without touching indexing.

Public entry point used by cloud_llm_service.py:
    rag_service.ask_question(category, question, top_k=3)
"""
import os
import time
import threading
import logging

import chromadb
from chromadb.utils import embedding_functions
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger('voice_bot')
RAG_MODEL_PATH = os.path.join(settings.BASE_DIR, "models")

KNOWLEDGE_COLLECTION_NAME = "recoverai_knowledge"


class RAGService:
    """Single-collection, category-filtered RAG over ChromaDB, driven by
    the KnowledgeDocument model."""

    def __init__(self):
        self.client = chromadb.PersistentClient(path=settings.CHROMA_PATH)
        self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="intfloat/multilingual-e5-small"
        )
        self._lock = threading.Lock()
        self.knowledge_collection = self._get_or_create(KNOWLEDGE_COLLECTION_NAME)

        # non-KB collections some other code paths still touch directly
        self.voice_collection = self._get_or_create("voice_settings")
        self.murf_collection = self._get_or_create("murf_voices")

        logger.info("RAG Service initialized with E5 embeddings (model cache: %s)", RAG_MODEL_PATH)

    def _get_or_create(self, name):
        try:
            return self.client.get_collection(name, embedding_function=self.embedding_fn)
        except Exception:
            return self.client.create_collection(
                name=name,
                embedding_function=self.embedding_fn,
                metadata={"hnsw:space": "cosine"},
            )

    # ========== KNOWLEDGE DOCUMENT CRUD (wired to the Django model) ==========

    def index_document(self, kb_doc):
        """kb_doc: a KnowledgeDocument instance."""
        chunks = self._chunk_text(kb_doc.content or kb_doc.title)
        category = kb_doc.category or "communication_policy"

        # wipe previous chunks for this doc first (re-index case)
        self._delete_doc_chunks(kb_doc.doc_id)

        try:
            chunk_embeddings = self.embedding_fn([f"passage: {c}" for c in chunks])
            ids = [f"{kb_doc.doc_id}__{i}" for i in range(len(chunks))]
            metadatas = [{
                "kb_document_id": kb_doc.id,
                "doc_id": kb_doc.doc_id,
                "title": kb_doc.title,
                "category": category,
                "chunk_index": i,
            } for i in range(len(chunks))]

            self.knowledge_collection.add(
                ids=ids, documents=chunks, metadatas=metadatas,
                embeddings=chunk_embeddings,
            )
        except Exception as exc:
            kb_doc.status = 'failed'
            kb_doc.error_message = str(exc)
            kb_doc.save(update_fields=['status', 'error_message', 'updated_at'])
            logger.error("index failed for %s: %s", kb_doc.doc_id, exc)
            return {"success": False, "error": str(exc)}

        kb_doc.collection_name = KNOWLEDGE_COLLECTION_NAME
        kb_doc.chunk_count = len(chunks)
        kb_doc.status = 'indexed'
        kb_doc.indexed_at = timezone.now()
        kb_doc.error_message = ''
        kb_doc.save(update_fields=[
            'collection_name', 'chunk_count', 'status', 'indexed_at',
            'error_message', 'updated_at',
        ])

        return {
            "success": True,
            "collection": KNOWLEDGE_COLLECTION_NAME,
            "category": category,
            "chunk_count": len(chunks),
        }

    def delete_document(self, kb_doc):
        self._delete_doc_chunks(kb_doc.doc_id)
        kb_doc.flag = 'd'
        kb_doc.save(update_fields=['flag', 'updated_at'])
        return {"success": True, "deleted": kb_doc.doc_id}

    def _delete_doc_chunks(self, doc_id):
        try:
            existing = self.knowledge_collection.get(where={"doc_id": doc_id})
            if existing.get("ids"):
                self.knowledge_collection.delete(ids=existing["ids"])
        except Exception as exc:
            logger.warning("chunk cleanup failed for %s: %s", doc_id, exc)

    @staticmethod
    def _chunk_text(text, max_chars=1200):
        text = (text or "").strip()
        if len(text) <= max_chars:
            return [text] if text else [""]
        chunks, buf = [], ""
        for para in text.split("\n\n"):
            if len(buf) + len(para) + 2 > max_chars and buf:
                chunks.append(buf.strip())
                buf = para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        if buf.strip():
            chunks.append(buf.strip())
        return chunks or [text]

    # ========== MAIN RAG QUERY (category-filtered) ==========

    def ask_question(self, category, question, top_k=3):
        """
        category : one of KnowledgeDocument.category values, e.g.
                   'payment_policy', 'hardship', 'callback'. Pass None or
                   '' to search across all categories.
        question : the customer's utterance / the query text.
        """
        start_total = time.time()
        where = {"category": category} if category else None

        t0 = time.time()
        try:
            query_embedding = self.embedding_fn([f"query: {question}"])
            results = self.knowledge_collection.query(
                query_embeddings=query_embedding,
                n_results=top_k,
                where=where,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            logger.error("chroma query failed for category=%s: %s", category, exc)
            return {
                "success": False, "error": str(exc), "question": question,
                "contexts": [], "sources": [], "distances": [], "best_distance": None,
                "timing_ms": {"embedding_time": 0, "chroma_search_time": 0, "total_time": 0},
            }
        search_time = (time.time() - t0) * 1000

        contexts = results["documents"][0] if results["documents"] else []
        sources = results["metadatas"][0] if results["metadatas"] else []
        distances = results["distances"][0] if results.get("distances") else []

        best_distance = min(distances) if distances else None
        total_time = (time.time() - start_total) * 1000

        return {
            "success": True,
            "question": question,
            "category": category,
            "contexts": contexts,
            "sources": sources,
            "distances": distances,
            "best_distance": best_distance,
            "timing_ms": {
                "embedding_time": 0,
                "chroma_search_time": round(search_time, 2),
                "total_time": round(total_time, 2),
            },
        }

    # ========== VOICE SETTINGS / MURF (unchanged) ==========

    def store_voice_setting(self, doc_id, text, metadata):
        self.voice_collection.add(ids=[doc_id], documents=[text], metadatas=[metadata])
        return {"success": True}

    def store_murf_voice(self, doc_id, text, metadata):
        meta = metadata or {}
        meta["provider"] = "murf"
        self.murf_collection.add(ids=[doc_id], documents=[text], metadatas=[meta])
        return {"success": True}

    def get_murf_voices(self):
        result = self.murf_collection.get()
        return {"success": True, "voices": result.get("documents", []), "metadatas": result.get("metadatas", [])}


# ========== SINGLETON ==========
_rag_service = None
_rag_lock = threading.Lock()


def get_rag_service():
    global _rag_service
    if _rag_service is None:
        with _rag_lock:
            if _rag_service is None:
                _rag_service = RAGService()
    return _rag_service