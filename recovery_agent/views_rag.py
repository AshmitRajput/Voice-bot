"""
RAG Knowledge Base APIs — admin-only.
Live calls do NOT call these HTTP endpoints — they go through
recovery_service.py → rag_service.py → Vector DB directly. """
import json
import time
import logging

from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import KnowledgeDocument, Dealer, Branch
from .services.rag_service import get_rag_service, _is_global_branch_ids
from .services.cloud_llm_service import chat_turn as cloud_chat_turn
from django.conf import settings

logger = logging.getLogger('voice_bot')


def _resolve_dealer_branch(data_or_params):
    """Shared dealer_id/branch_id resolution used by every endpoint below."""
    dealer_id = data_or_params.get('dealer_id')
    if not dealer_id:
        raise ValueError("dealer_id is required")
    dealer = get_object_or_404(Dealer, pk=dealer_id, flag='c')
    branch = None
    branch_id = data_or_params.get('branch_id')
    if branch_id:
        branch = get_object_or_404(Branch, pk=branch_id, dealer=dealer, flag='c')
    return dealer, branch


# ═══════════════════════════════════════════════════════════════
# STORE — create or update a knowledge document
# ═══════════════════════════════════════════════════════════════

@csrf_exempt
@require_http_methods(["POST"])
def kb_store(request):
    """
    POST /api/kb/store/
    Body: {
      "dealer_id": 1, "module": "service", "branch_ids": [3, 5],
      "doc_id": "warranty-policy-2026", "title": "...",
      "category": "policy", "content": "..."
    }
    branch_ids omitted or [] => applies to the whole dealer. """
    try:
        data = json.loads(request.body)
        dealer, _ = _resolve_dealer_branch(data)

        module = data.get('module', 'service')
        branch_ids = data.get('branch_ids', []) or []
        doc_id = data.get('doc_id') or f"doc_{int(time.time())}"
        content = data.get('content', data.get('text', ''))

        kb_doc, _created = KnowledgeDocument.objects.update_or_create(
            dealer=dealer, doc_id=doc_id, flag='c',
            defaults={
                "branch_ids": branch_ids,
                "module": module,
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

        logger.info(f"📥 Stored+indexed: {doc_id} -> {result.get('collection')}")
        return JsonResponse(result)

    except ValueError as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)
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
    Body: {"dealer_id": 1, "module": "service", "branch_id": 3,
           "question": "...", "top_k": 3}
    """
    try:
        data = json.loads(request.body)
        dealer, branch = _resolve_dealer_branch(data)
        module = data.get('module', 'service')

        rag = get_rag_service()
        result = rag.ask_question(
            dealer=dealer, module=module,
            question=data.get('question', ''),
            branch=branch, top_k=data.get('top_k', 3),
        )
        return JsonResponse(result)

    except ValueError as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)
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
    Body: {"dealer_id": 1, "module": "service", "branch_id": 3,
           "question": "...", "top_k": 3, "session_id": "..."}
    """
    try:
        data = json.loads(request.body)
        dealer, branch = _resolve_dealer_branch(data)
        module = data.get('module', 'service')

        question = data.get('question', '')
        top_k = data.get('top_k', 3)
        session_id = data.get('session_id', 'kb_ask_stream_default')

        llm_provider = data.get(
            'llm_provider',
            getattr(settings, 'ACTIVE_LLM_PROVIDER', 'cloud')
        ).lower().strip()

        rag = get_rag_service()
        start_time = time.time()

        def generate():
            t0 = time.time()
            rag_result = rag.ask_question(
                dealer=dealer, module=module,
                question=question, branch=branch, top_k=top_k,
            )
            rag_time = (time.time() - t0) * 1000

            contexts = rag_result.get('contexts', [])
            reference_context = "\n\n".join(contexts) if contexts else None

            yield json.dumps({
                "type": "meta",
                "provider": llm_provider,
                "sources": rag_result.get('sources', []),
                "timing_ms": {
                    "embedding_time": 0,
                    "chroma_search_time": rag_result['timing_ms']['chroma_search_time'],
                    "rag_total": rag_result['timing_ms']['total_time'],
                }
            }) + "\n"

            llm_start = time.time()
            answer_text = ""
            usage = {}
            first_token_ms = None

            cloud_context = {
                "customer_name": data.get("customer_name", "Admin"),
                "vehicle_model": "Unknown",
                "due_date": "Unknown",
                "module": module,
                "dealer_code": dealer.code,
                "branch_id": branch.id if branch else None,
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

    except ValueError as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)
    except Exception as e:
        logger.error(f"Stream error: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ═══════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════

@api_view(['GET'])
def kb_stats(request):
    """GET /api/kb/stats/?dealer_id=1&module=service"""
    dealer_id = request.GET.get('dealer_id')
    if not dealer_id:
        return Response({"success": False, "error": "dealer_id is required"}, status=400)

    qs = KnowledgeDocument.objects.filter(dealer_id=dealer_id, flag='c')
    module = request.GET.get('module')
    if module:
        qs = qs.filter(module=module)

    by_module = {}
    for m, in qs.values_list('module').distinct():
        by_module[m] = qs.filter(module=m).count()

    return Response({
        "success": True,
        "total_documents": qs.count(),
        "by_module": by_module,
        "embedding_model": "E5-multilingual",
    })


# ═══════════════════════════════════════════════════════════════
# LIST / GET / UPDATE / DELETE
# ═══════════════════════════════════════════════════════════════

@api_view(['GET'])
def kb_documents(request):
    """
    GET /api/kb/documents/?dealer_id=1&module=service&branch_id=3&category=...
    """
    dealer_id = request.GET.get('dealer_id')
    if not dealer_id:
        return Response({"success": False, "error": "dealer_id is required"}, status=400)

    qs = KnowledgeDocument.objects.filter(dealer_id=dealer_id, flag='c')
    if request.GET.get('module'):
        qs = qs.filter(module=request.GET['module'])
    if request.GET.get('category'):
        qs = qs.filter(category=request.GET['category'])

    docs = list(qs)
    branch_id = request.GET.get('branch_id')
    if branch_id:
        branch_id = int(branch_id)
        docs = [d for d in docs
                if _is_global_branch_ids(d.branch_ids) or branch_id in d.branch_ids]

    documents = [{
        "id": d.id, "doc_id": d.doc_id, "title": d.title, "category": d.category,
        "module": d.module, "branch_ids": d.branch_ids, "status": d.status,
        "chunk_count": d.chunk_count, "collection_name": d.collection_name,
        "source": d.source, "metadata": d.metadata,
        "indexed_at": d.indexed_at,
    } for d in docs]

    return Response({
        "success": True,
        "documents": documents,
        "total_count": len(documents),
    })


@api_view(['GET'])
def kb_document_detail(request, doc_id):
    """GET /api/kb/documents/<doc_id>/?dealer_id=1"""
    dealer_id = request.GET.get('dealer_id')
    if not dealer_id:
        return Response({"success": False, "error": "dealer_id is required"}, status=400)

    try:
        d = KnowledgeDocument.objects.get(dealer_id=dealer_id, doc_id=doc_id, flag='c')
    except KnowledgeDocument.DoesNotExist:
        return Response({"success": False, "error": f"Document {doc_id} not found"}, status=404)

    return Response({"success": True, "document": {
        "id": d.id, "doc_id": d.doc_id, "title": d.title, "category": d.category,
        "module": d.module, "branch_ids": d.branch_ids, "content": d.content,
        "source": d.source, "metadata": d.metadata,
        "status": d.status, "chunk_count": d.chunk_count,
        "collection_name": d.collection_name, "indexed_at": d.indexed_at,
    }})


@csrf_exempt
@require_http_methods(["PUT"])
def kb_document_update(request, doc_id):
    """PUT /api/kb/documents/<doc_id>/"""
    try:
        data = json.loads(request.body)
        dealer_id = data.get('dealer_id')
        if not dealer_id:
            return JsonResponse({"success": False, "error": "dealer_id is required"}, status=400)

        try:
            kb_doc = KnowledgeDocument.objects.get(dealer_id=dealer_id, doc_id=doc_id, flag='c')
        except KnowledgeDocument.DoesNotExist:
            return JsonResponse({"success": False, "error": f"Document {doc_id} not found"}, status=404)

        for field in ('title', 'category', 'content', 'branch_ids', 'source', 'metadata'):
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
    """DELETE /api/kb/documents/<doc_id>/?dealer_id=1"""
    dealer_id = request.GET.get('dealer_id')
    if not dealer_id:
        return JsonResponse({"success": False, "error": "dealer_id is required"}, status=400)

    try:
        kb_doc = KnowledgeDocument.objects.get(dealer_id=dealer_id, doc_id=doc_id, flag='c')
    except KnowledgeDocument.DoesNotExist:
        return JsonResponse({"success": False, "error": f"Document {doc_id} not found"}, status=404)

    rag = get_rag_service()
    result = rag.delete_document(kb_doc)
    return JsonResponse(result)