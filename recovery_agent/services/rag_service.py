"""
RAG Service — wired to KnowledgeDocument.

Collection naming: kb_{dealer.code}_{module}   e.g. kb_OMH_service
One collection per dealer per module — keeps a service question from
matching insurance chunks, and keeps dealers' data physically separate.

Branch scoping (KnowledgeDocument.branch_ids):
    []      -> applies to the whole dealer (all branches)
    [3, 5]  -> applies only to branches 3 and 5
Chroma metadata values must be str/int/float/bool, not a list, so each
indexed chunk carries:
    is_global   : bool
    branch_ids  : comma-separated string ("3,5", or "" if global)
and branch filtering happens in Python after the similarity search
(with a small over-fetch so a branch-filtered top_k still comes back full).
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

BRANCH_OVERFETCH_MULTIPLIER = 4
BRANCH_OVERFETCH_MAX = 50

def _is_global_branch_ids(branch_ids):
    """
    Per KnowledgeDocument.branch_ids convention (see models.py / admin help text):
    [] or [0] => applies to the whole dealer (all branches).
    Anything else (e.g. [3, 5]) => scoped to those specific branch ids.
    """
    ids = branch_ids or []
    return len(ids) == 0 or list(ids) == [0]

def _target_modules(module):
    """Modules this doc's chunks should be indexed into. Path A: every
    module's docs also feed general_query, so the catch-all agent can
    draw on facts even when a call didn't resolve to a specific module.
    Docs that ARE general_query don't get duplicated into themselves."""
    modules = [module]
    if module != "general_query":
        modules.append("general_query")
    return modules


def collection_name_for(dealer, module):
    """kb_{dealer.code}_{module} — must match KnowledgeDocument.collection_name"""
    return f"kb_{dealer.code}_{module}"


def _branch_ids_to_str(branch_ids):
    return ",".join(str(b) for b in (branch_ids or []))


def _branch_matches(doc_branch_ids_str, is_global, branch_id):
    if branch_id is None or is_global:
        return True
    if not doc_branch_ids_str:
        return True  # defensive: blank/missing metadata treated as global
    ids = {int(x) for x in doc_branch_ids_str.split(",") if x}
    return branch_id in ids


class RAGService:
    """
    Multi-tenant, multi-module RAG over ChromaDB, driven by the
    KnowledgeDocument model rather than a single flat "prompts" collection.
    """

    def __init__(self):
        self.client = chromadb.PersistentClient(path=settings.CHROMA_PATH)
        self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="intfloat/multilingual-e5-small"
        )
        self._collections = {}          # collection_name -> chroma Collection
        self._collections_lock = threading.Lock()

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

    def _collection_for(self, dealer, module):
        name = collection_name_for(dealer, module)
        if name not in self._collections:
            with self._collections_lock:
                if name not in self._collections:
                    self._collections[name] = self._get_or_create(name)
        return self._collections[name]

    # ========== KNOWLEDGE DOCUMENT CRUD (wired to the Django model) ==========

    def index_document(self, kb_doc):
        dealer = kb_doc.dealer
        chunks = self._chunk_text(kb_doc.content or kb_doc.title)
        is_global = _is_global_branch_ids(kb_doc.branch_ids)
        branch_ids_str = _branch_ids_to_str(kb_doc.branch_ids)

        primary_name = collection_name_for(dealer, kb_doc.module)
        indexed_collections = []

        for mod in _target_modules(kb_doc.module):
            collection = self._collection_for(dealer, mod)
            name = collection_name_for(dealer, mod)

            # wipe previous chunks for this doc in THIS collection first
            self._delete_doc_chunks(collection, kb_doc.doc_id)
            
            chunk_embeddings = self.embedding_fn([f"passage: {c}" for c in chunks])
            ids = [f"{kb_doc.doc_id}__{i}" for i in range(len(chunks))]
            metadatas = [{
                "kb_document_id": kb_doc.id,
                "doc_id": kb_doc.doc_id,
                "title": kb_doc.title,
                "category": kb_doc.category or "",
                "module": kb_doc.module,        # 🔥 owning module, even inside general_query's copy
                "dealer_code": dealer.code,
                "is_global": is_global,
                "branch_ids": branch_ids_str,
                "chunk_index": i,
            } for i in range(len(chunks))]

            try:
                collection.add(ids=ids, documents=chunks, metadatas=metadatas,
                                embeddings=chunk_embeddings)
                indexed_collections.append(name)
            except Exception as exc:
                kb_doc.status = 'failed'
                kb_doc.save(update_fields=['status', 'updated_at'])
                logger.error("index failed for %s in %s: %s", kb_doc.doc_id, name, exc)
                return {"success": False, "error": str(exc), "partial": indexed_collections}

        kb_doc.collection_name = primary_name
        kb_doc.chunk_count = len(chunks)
        kb_doc.status = 'indexed'
        kb_doc.indexed_at = timezone.now()
        kb_doc.save(update_fields=['collection_name', 'chunk_count', 'status', 'indexed_at', 'updated_at'])

        return {
            "success": True,
            "collection": primary_name,
            "collections": indexed_collections,   # 🔥 both, for debugging/UI
            "chunk_count": len(chunks),
        }

    def delete_document(self, kb_doc):
        for mod in _target_modules(kb_doc.module):
            collection = self._collection_for(kb_doc.dealer, mod)
            self._delete_doc_chunks(collection, kb_doc.doc_id)
        kb_doc.flag = 'd'
        kb_doc.save(update_fields=['flag', 'updated_at'])
        return {"success": True, "deleted": kb_doc.doc_id}

    def _delete_doc_chunks(self, collection, doc_id):
        try:
            existing = collection.get(where={"doc_id": doc_id})
            if existing.get("ids"):
                collection.delete(ids=existing["ids"])
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

    # ========== MAIN RAG QUERY (branch + module aware) ==========

    def ask_question(self, dealer, module, question, branch=None, top_k=3):
        """
        dealer : Dealer instance (required — selects the collection)
        module : 'service' | 'insurance' | 'amc' | ... (selects the collection)
        branch : Branch instance or None. If given, only chunks that are
                 global (branch_ids=[]) or explicitly cover this branch come back.
        """
        start_total = time.time()
        collection = self._collection_for(dealer, module)
        branch_id = branch.id if branch else None

        fetch_k = top_k
        if branch_id is not None:
            fetch_k = min(max(top_k * BRANCH_OVERFETCH_MULTIPLIER, top_k), BRANCH_OVERFETCH_MAX)

        t0 = time.time()
        try:
            # match the "query: " prefix used at index time (see index_document)
            query_embedding = self.embedding_fn([f"query: {question}"])
            results = collection.query(
                query_embeddings=query_embedding,
                n_results=fetch_k,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            logger.error("chroma query failed for %s: %s", collection_name_for(dealer, module), exc)
            return {
                "success": False, "error": str(exc), "question": question,
                "contexts": [], "sources": [], "distances": [], "best_distance": None,
                "timing_ms": {"embedding_time": 0, "chroma_search_time": 0, "total_time": 0},
            }
        search_time = (time.time() - t0) * 1000

        raw_docs = results["documents"][0] if results["documents"] else []
        raw_meta = results["metadatas"][0] if results["metadatas"] else []
        raw_dist = results["distances"][0] if results.get("distances") else []

        contexts, sources, distances = [], [], []
        for doc, meta, dist in zip(raw_docs, raw_meta, raw_dist):
            if branch_id is not None and not _branch_matches(
                meta.get("branch_ids", ""), meta.get("is_global", True), branch_id
            ):
                continue
            contexts.append(doc)
            sources.append(meta)
            distances.append(dist)
            if len(contexts) >= top_k:
                break

        # respect Dealer.rag_distance_threshold if it's set
        threshold = getattr(dealer, "rag_distance_threshold", None)
        if threshold is not None:
            kept = [(c, s, d) for c, s, d in zip(contexts, sources, distances) if d <= threshold]
            contexts, sources, distances = (list(x) for x in zip(*kept)) if kept else ([], [], [])

        best_distance = min(distances) if distances else None
        total_time = (time.time() - start_total) * 1000

        return {
            "success": True,
            "question": question,
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