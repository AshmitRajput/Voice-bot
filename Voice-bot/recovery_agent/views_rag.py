"""
RAG Knowledge Base APIs — admin-only.
Live calls do NOT call these HTTP endpoints — they go through
recovery_service.py / recovery_tools.py -> rag_service.py -> Chroma
directly. Rewritten: no Dealer/Branch/module — KnowledgeDocument is keyed
by `category` only (payment_policy, hardship, dispute, callback, etc.)
"""
import json
import time
import logging

from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import KnowledgeDocument
from .services.rag_service import get_rag_service
from .services.cloud_llm_service import chat_turn as cloud_chat_turn

logger = logging.getLogger('voice_bot')


# ═══════════════════════════════════════════════════════════════
# STORE — create or update a knowledge document
# ═══════════════════════════════════════════════════════════════

@csrf_exempt
@require_http_methods(["POST"])
def kb_store(request):
    """
    POST /api/kb/store/
    Body: {
      "doc_id": "hardship-policy-2026", "title": "...",
      "category": "hardship", "content": "..."
    }
    category is one of KnowledgeDocument's documented values (payment_policy,
    payment_methods, payment_link, late_payment, promise_to_pay, hardship,
    dispute, complaint, callback, escalation, communication_policy).
    """
    try:
        data = json.loads(request.body)
        doc_id = data.get('doc_id') or f"doc_{int(time.time())}"
        content = data.get('content', data.get('text', ''))

        kb_doc, _created = KnowledgeDocument.objects.update_or_create(
            doc_id=doc_id, flag='c',
            defaults={
                "title": data.get('title', doc_id),
                "category": data.get('category', ''),
                "content": content,
                "source": data.get('source', ''),
                "metadata": data.get('metadata', {}),
                "status": "pending",
            },
        )

        rag = get_rag_service()
        result = rag.index_document(kb_doc)
        result["kb_document_id"] = kb_doc.id

        logger.info(f"📥 Stored+indexed: {doc_id} -> category={kb_doc.category}")
        return JsonResponse(result)

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"Store error: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ═══════════════════════════════════════════════════════════════
# ASK — one-shot RAG query
# ═══════════════════════════════════════════════════════════════

@csrf_exempt
@require_http_methods(["POST"])
def kb_ask(request):
    """
    POST /api/kb/ask/
    Body: {"category": "hardship", "question": "...", "top_k": 3}
    category may be omitted to search across all categories.
    """
    try:
        data = json.loads(request.body)
        rag = get_rag_service()
        result = rag.ask_question(
            category=data.get('category'),
            question=data.get('question', ''),
            top_k=data.get('top_k', 3),
        )
        return JsonResponse(result)

    except Exception as e:
        logger.error(f"Ask error: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ═══════════════════════════════════════════════════════════════
# ASK STREAM — admin RAG chat with token streaming
# ═══════════════════════════════════════════════════════════════

@csrf_exempt
def kb_ask_stream(request):
    """
    POST /api/kb/ask/stream/
    Body: {"category": "hardship", "question": "...", "top_k": 3, "session_id": "..."}
    """
    try:
        data = json.loads(request.body)
        category = data.get('category')
        question = data.get('question', '')
        top_k = data.get('top_k', 3)
        session_id = data.get('session_id', 'kb_ask_stream_default')

        rag = get_rag_service()
        start_time = time.time()

        def generate():
            t0 = time.time()
            rag_result = rag.ask_question(category=category, question=question, top_k=top_k)

            contexts = rag_result.get('contexts', [])
            reference_context = "\n\n".join(contexts) if contexts else None

            yield json.dumps({
                "type": "meta",
                "sources": rag_result.get('sources', []),
                "timing_ms": {
                    "embedding_time": 0,
                    "chroma_search_time": rag_result['timing_ms']['chroma_search_time'],
                    "rag_total": rag_result['timing_ms']['total_time'],
                }
            }) + "\n"

            llm_start = time.time()
            first_token_ms = None

            cloud_context = {
                "customer_name": data.get("customer_name", "Admin"),
                "workflow": "revenue_recovery",
                "rag_category": category or "communication_policy",
            }

            cloud_result = cloud_chat_turn(
                session_id=session_id,
                customer_text=question,
                context=cloud_context,
                use_rag=False,
                reference_context=reference_context,
            )

            answer_text = cloud_result.get("response_text", "")
            usage = cloud_result.get("usage", {})

            for word in answer_text.split(" "):
                if first_token_ms is None:
                    first_token_ms = int((time.time() - start_time) * 1000)
                yield json.dumps({"type": "token", "text": word + " "}) + "\n"

            total_time = (time.time() - start_time) * 1000
            gpt_time = (time.time() - llm_start) * 1000

            yield json.dumps({
                "type": "done",
                "timing_ms": {
                    "first_token_ms": first_token_ms,
                    "gpt_time": round(gpt_time, 2),
                    "total_time": round(total_time, 2),
                },
                "usage": usage,
            }) + "\n"

        return StreamingHttpResponse(generate(), content_type="application/x-ndjson")

    except Exception as e:
        logger.error(f"Stream error: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ═══════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════

@api_view(['GET'])
def kb_stats(request):
    """GET /api/kb/stats/?category=hardship"""
    qs = KnowledgeDocument.objects.filter(flag='c')
    category = request.GET.get('category')
    if category:
        qs = qs.filter(category=category)

    by_category = {}
    for cat, in qs.values_list('category').distinct():
        by_category[cat or "(uncategorized)"] = qs.filter(category=cat).count()

    return Response({
        "success": True,
        "total_documents": qs.count(),
        "by_category": by_category,
        "embedding_model": "E5-multilingual",
    })


# ═══════════════════════════════════════════════════════════════
# LIST / GET / UPDATE / DELETE
# ═══════════════════════════════════════════════════════════════

@api_view(['GET'])
def kb_documents(request):
    """GET /api/kb/documents/?category=hardship&status=indexed"""
    qs = KnowledgeDocument.objects.filter(flag='c')
    if request.GET.get('category'):
        qs = qs.filter(category=request.GET['category'])
    if request.GET.get('status'):
        qs = qs.filter(status=request.GET['status'])

    documents = [{
        "id": d.id, "doc_id": d.doc_id, "title": d.title, "category": d.category,
        "status": d.status, "chunk_count": d.chunk_count,
        "collection_name": d.collection_name,
        "source": d.source, "metadata": d.metadata,
        "indexed_at": d.indexed_at,
    } for d in qs.order_by('-created_at')]

    return Response({
        "success": True,
        "documents": documents,
        "total_count": len(documents),
    })


@api_view(['GET'])
def kb_document_detail(request, doc_id):
    """GET /api/kb/documents/<doc_id>/"""
    try:
        d = KnowledgeDocument.objects.get(doc_id=doc_id, flag='c')
    except KnowledgeDocument.DoesNotExist:
        return Response({"success": False, "error": f"Document {doc_id} not found"}, status=404)

    return Response({"success": True, "document": {
        "id": d.id, "doc_id": d.doc_id, "title": d.title, "category": d.category,
        "content": d.content, "source": d.source, "metadata": d.metadata,
        "status": d.status, "chunk_count": d.chunk_count,
        "collection_name": d.collection_name, "indexed_at": d.indexed_at,
    }})


@csrf_exempt
@require_http_methods(["PUT"])
def kb_document_update(request, doc_id):
    """PUT /api/kb/documents/<doc_id>/"""
    try:
        data = json.loads(request.body)
        try:
            kb_doc = KnowledgeDocument.objects.get(doc_id=doc_id, flag='c')
        except KnowledgeDocument.DoesNotExist:
            return JsonResponse({"success": False, "error": f"Document {doc_id} not found"}, status=404)

        for field in ('title', 'category', 'content', 'source', 'metadata'):
            if field in data:
                setattr(kb_doc, field, data[field])
        kb_doc.status = 'stale'
        kb_doc.save()

        rag = get_rag_service()
        result = rag.index_document(kb_doc)
        return JsonResponse(result)

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@csrf_exempt
@require_http_methods(["DELETE"])
def kb_document_delete(request, doc_id):
    """DELETE /api/kb/documents/<doc_id>/"""
    try:
        kb_doc = KnowledgeDocument.objects.get(doc_id=doc_id, flag='c')
    except KnowledgeDocument.DoesNotExist:
        return JsonResponse({"success": False, "error": f"Document {doc_id} not found"}, status=404)

    rag = get_rag_service()
    result = rag.delete_document(kb_doc)
    return JsonResponse(result)