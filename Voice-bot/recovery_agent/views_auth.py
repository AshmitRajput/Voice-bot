"""
Auth endpoints — session-cookie based (matches Django admin's own auth).

Single-admin MVP: there's no signup endpoint. The admin user is created via
`python manage.py createsuperuser` (or any existing Django User). This file
only exposes login/logout/me on top of Django's built-in auth system —
no new models, no token machinery.

Flow the frontend follows:
  1. GET  /api/auth/csrf/   -> sets the csrftoken cookie (call once on app load)
  2. POST /api/auth/login/  -> {username, password} -> sets sessionid cookie
  3. GET  /api/auth/me/     -> {authenticated: true, user: {...}} or 401
  4. POST /api/auth/logout/ -> clears the session

Because DRF's SessionAuthentication enforces CSRF on unsafe methods, every
POST here must carry the X-CSRFToken header read from the csrftoken cookie.
The frontend's api.ts helper does this automatically.
"""
import json
import logging

from django.contrib.auth import authenticate, login, logout
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_protect
from django.views.decorators.http import require_http_methods
from django.http import JsonResponse
from django.utils.decorators import method_decorator

logger = logging.getLogger('recovery_agent')


def _serialize_user(user):
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "name": (user.get_full_name() or user.username),
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
    }


@ensure_csrf_cookie
@require_http_methods(["GET"])
def auth_csrf(request):
    """GET /api/auth/csrf/ — call once on app load so the csrftoken cookie is set."""
    return JsonResponse({"success": True})


@csrf_protect
@require_http_methods(["POST"])
def auth_login(request):
    """POST /api/auth/login/  Body: {"username": "...", "password": "..."}"""
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return JsonResponse({"success": False, "error": "username and password are required"}, status=400)

    user = authenticate(request, username=username, password=password)
    if user is None:
        return JsonResponse({"success": False, "error": "Invalid credentials"}, status=401)
    if not user.is_active:
        return JsonResponse({"success": False, "error": "Account is disabled"}, status=403)

    login(request, user)
    return JsonResponse({"success": True, "user": _serialize_user(user)})


@require_http_methods(["GET"])
def auth_me(request):
    """GET /api/auth/me/ — returns the current session's user, if any."""
    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "authenticated": False}, status=401)
    return JsonResponse({"success": True, "authenticated": True, "user": _serialize_user(request.user)})


@csrf_protect
@require_http_methods(["POST"])
def auth_logout(request):
    """POST /api/auth/logout/"""
    logout(request)
    return JsonResponse({"success": True})
