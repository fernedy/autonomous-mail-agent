"""Tests unitarios para auth_router.py — login multi-tenant.

Evalúa:
- Login con username
- Login con email Outlook (get_user_by_email)
- Login con super admin (bypassea tenant status check)
- Login con tenant pendiente/rechazado/inactivo (debe fallar)
- Registro de nuevo tenant
- Registro con username duplicado
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request


@pytest.fixture
def mock_request():
    """Mock Request for slowapi rate limiter."""
    req = MagicMock(spec=Request)
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_mock_user(
    user_id: str = None,
    tenant_id: str = "tenant-1",
    username: str = "jperez",
    password_hash: str = "$2b$12$correct_hash",
    role: str = "admin",
    is_active: bool = True,
    is_super_admin: bool = False,
):
    u = MagicMock()
    u.id = user_id or str(uuid.uuid4())
    u.tenant_id = tenant_id
    u.username = username
    u.password_hash = password_hash
    u.role = role
    u.is_active = is_active
    u.is_super_admin = is_super_admin
    return u


def _make_mock_tenant(
    tenant_id: str = "tenant-1",
    name: str = "Mi Tenant",
    status: str = "active",
    is_active: bool = True,
    outlook_user: str = "correo@empresa.com",
):
    t = MagicMock()
    t.id = tenant_id
    t.name = name
    t.status = status
    t.is_active = is_active
    t.outlook_user = outlook_user
    return t


@pytest.fixture
def mock_session():
    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.get = AsyncMock()
    return session


# ── Tests: login ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("src.web.api.auth_router.get_user_by_username")
@patch("src.web.api.auth_router.get_user_by_email")
@patch("src.web.api.auth_router.verify_password", return_value=True)
@patch("src.web.api.auth_router.create_token", return_value="jwt_token_123")
async def test_login_with_username(
    mock_token, mock_verify, mock_get_email, mock_get_username,
    mock_session, mock_request,
):
    """Login con username debe encontrar usuario por username."""
    from src.web.api.auth_router import login, LoginRequest

    user = _make_mock_user(username="jperez", tenant_id="tenant-1")
    tenant = _make_mock_tenant("tenant-1", "Mi Tenant", "active")

    mock_get_username.return_value = user
    mock_session.get.return_value = tenant

    req = LoginRequest(username="jperez", password="correctpw")
    result = await login(req=req, request=mock_request, session=mock_session)

    assert result.token == "jwt_token_123"
    assert result.username == "jperez"
    assert result.tenant_id == "tenant-1"
    assert result.tenant_name == "Mi Tenant"
    mock_get_username.assert_called_once_with(mock_session, "jperez")
    mock_get_email.assert_not_called()


@pytest.mark.asyncio
@patch("src.web.api.auth_router.get_user_by_username")
@patch("src.web.api.auth_router.get_user_by_email")
@patch("src.web.api.auth_router.verify_password", return_value=True)
@patch("src.web.api.auth_router.create_token", return_value="jwt_token_456")
async def test_login_with_email(
    mock_token, mock_verify, mock_get_email, mock_get_username,
    mock_session, mock_request,
):
    """Login con email (outlook_user) debe encontrar usuario por email."""
    from src.web.api.auth_router import login, LoginRequest

    user = _make_mock_user(username="jperez", tenant_id="tenant-1")
    tenant = _make_mock_tenant("tenant-1", "Mi Tenant", "active")

    mock_get_username.return_value = None  # No se encuentra por username
    mock_get_email.return_value = user  # Se encuentra por email
    mock_session.get.return_value = tenant

    req = LoginRequest(username="jperez@empresa.com", password="correctpw")
    result = await login(req=req, request=mock_request, session=mock_session)

    assert result.token == "jwt_token_456"
    assert result.username == "jperez"
    mock_get_username.assert_called_once_with(mock_session, "jperez@empresa.com")
    mock_get_email.assert_called_once_with(mock_session, "jperez@empresa.com")


@pytest.mark.asyncio
@patch("src.web.api.auth_router.get_user_by_username")
@patch("src.web.api.auth_router.get_user_by_email")
@patch("src.web.api.auth_router.verify_password", return_value=True)
@patch("src.web.api.auth_router.create_token", return_value="jwt_super")
async def test_login_super_admin(
    mock_token, mock_verify, mock_get_email, mock_get_username,
    mock_session, mock_request,
):
    """Super admin login debe bypassear tenant status check."""
    from src.web.api.auth_router import login, LoginRequest

    sa = _make_mock_user(username="admin", is_super_admin=True)
    mock_get_username.return_value = sa

    req = LoginRequest(username="admin", password="admin123")
    result = await login(req=req, request=mock_request, session=mock_session)

    assert result.token == "jwt_super"
    assert result.role == "super_admin"
    assert result.tenant_name == "Platform Admin"
    mock_session.get.assert_not_called()


@pytest.mark.asyncio
@patch("src.web.api.auth_router.get_user_by_username")
@patch("src.web.api.auth_router.verify_password", return_value=True)
@patch("src.web.api.auth_router.create_token")
async def test_login_pending_tenant(
    mock_token, mock_verify, mock_get_username, mock_session, mock_request,
):
    """Login con tenant pendiente debe retornar 403."""
    from src.web.api.auth_router import login, LoginRequest

    user = _make_mock_user(username="jperez", tenant_id="tenant-pending")
    tenant = _make_mock_tenant("tenant-pending", "Pendiente", "pending")

    mock_get_username.return_value = user
    mock_session.get.return_value = tenant

    req = LoginRequest(username="jperez", password="correctpw")
    with pytest.raises(HTTPException) as exc:
        await login(req=req, request=mock_request, session=mock_session)
    assert exc.value.status_code == 403
    assert "pendiente" in exc.value.detail.lower()


@pytest.mark.asyncio
@patch("src.web.api.auth_router.get_user_by_username")
@patch("src.web.api.auth_router.verify_password", return_value=True)
@patch("src.web.api.auth_router.create_token")
async def test_login_rejected_tenant(
    mock_token, mock_verify, mock_get_username, mock_session, mock_request,
):
    """Login con tenant rechazado debe retornar 403."""
    from src.web.api.auth_router import login, LoginRequest

    user = _make_mock_user(username="jperez", tenant_id="tenant-rejected")
    tenant = _make_mock_tenant("tenant-rejected", "Rechazado", "rejected")

    mock_get_username.return_value = user
    mock_session.get.return_value = tenant

    req = LoginRequest(username="jperez", password="correctpw")
    with pytest.raises(HTTPException) as exc:
        await login(req=req, request=mock_request, session=mock_session)
    assert exc.value.status_code == 403
    assert "rechazado" in exc.value.detail.lower()


@pytest.mark.asyncio
@patch("src.web.api.auth_router.get_user_by_username")
@patch("src.web.api.auth_router.verify_password", return_value=False)
async def test_login_wrong_password(
    mock_verify, mock_get_username, mock_session, mock_request,
):
    """Login con contraseña incorrecta debe retornar 401."""
    from src.web.api.auth_router import login, LoginRequest

    user = _make_mock_user(username="jperez")
    mock_get_username.return_value = user

    req = LoginRequest(username="jperez", password="wrongpw")
    with pytest.raises(HTTPException) as exc:
        await login(req=req, request=mock_request, session=mock_session)
    assert exc.value.status_code == 401
    assert "Invalid credentials" in str(exc.value.detail)


@pytest.mark.asyncio
@patch("src.web.api.auth_router.get_user_by_username")
@patch("src.web.api.auth_router.get_user_by_email")
@patch("src.web.api.auth_router.verify_password", return_value=True)
@patch("src.web.api.auth_router.create_token", return_value="jwt_token")
async def test_login_inactive_tenant(
    mock_token, mock_verify, mock_get_email, mock_get_username,
    mock_session, mock_request,
):
    """Login con tenant inactivo debe retornar 403."""
    from src.web.api.auth_router import login, LoginRequest

    user = _make_mock_user(username="jperez", tenant_id="tenant-inactive")
    tenant = _make_mock_tenant("tenant-inactive", "Inactivo", "active", is_active=False)

    mock_get_username.return_value = user
    mock_session.get.return_value = tenant

    req = LoginRequest(username="jperez", password="correctpw")
    with pytest.raises(HTTPException) as exc:
        await login(req=req, request=mock_request, session=mock_session)
    assert exc.value.status_code == 403
    assert "inactive" in str(exc.value.detail).lower()


# ── Tests: register ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("src.web.api.auth_router.get_user_by_username")
@patch("src.web.api.auth_router.get_tenant_by_name")
@patch("src.web.api.auth_router.encrypt_outlook_password", return_value="encrypted_pass")
@patch("src.web.api.auth_router.hash_password", return_value="$2b$12$hashed_pw")
async def test_register_success(
    mock_hash, mock_encrypt, mock_get_tenant, mock_get_user,
    mock_session,
):
    """Registro exitoso debe crear Tenant (pending) + User (admin)."""
    from src.web.api.auth_router import register, RegisterRequest

    mock_get_user.return_value = None
    mock_get_tenant.return_value = None

    req = RegisterRequest(
        username="nuevo_admin",
        password="password123",
        tenant_name="Nueva Empresa SAS",
        tenant_email="admin@nueva.com",
        outlook_user="correo@nueva.com",
        outlook_pass="outlook_secret",
    )

    _added = []
    mock_session.add = MagicMock(side_effect=lambda x: _added.append(x))
    async def _mock_flush():
        for obj in _added:
            if hasattr(obj, 'id') and obj.id is None:
                obj.id = str(uuid.uuid4())
    mock_session.flush = AsyncMock(side_effect=_mock_flush)

    result = await register(req=req, session=mock_session)

    assert "registrado" in result.message.lower()
    assert result.tenant_status == "pending"
    assert result.tenant_name == "Nueva Empresa SAS"
    assert result.tenant_id is not None
    assert mock_session.add.call_count >= 2
    mock_session.commit.assert_called()


@pytest.mark.asyncio
@patch("src.web.api.auth_router.get_user_by_username")
async def test_register_duplicate_username(
    mock_get_user, mock_session,
):
    """Registro con username existente debe retornar 409."""
    from src.web.api.auth_router import register, RegisterRequest

    existing = _make_mock_user(username="exists")
    mock_get_user.return_value = existing

    req = RegisterRequest(
        username="exists",
        password="password123",
        tenant_name="Nueva Empresa",
        tenant_email="admin@nueva.com",
        outlook_user="correo@nueva.com",
        outlook_pass="outlook_secret",
    )

    with pytest.raises(HTTPException) as exc:
        await register(req=req, session=mock_session)
    assert exc.value.status_code == 409
    assert "already exists" in exc.value.detail.lower()


# ── Tests: /me endpoint ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_me_returns_current_user():
    """GET /me debe devolver el current_user dict."""
    from src.web.api.auth_router import me

    mock_user = {
        "user_id": "u-1",
        "tenant_id": "tenant-1",
        "role": "admin",
        "username": "jperez",
        "is_super_admin": False,
    }

    result = await me(current_user=mock_user)
    assert result == mock_user
    assert result["username"] == "jperez"
