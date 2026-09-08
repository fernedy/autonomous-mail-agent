# [src/graph/__init__.py]
"""
Módulo de autenticación y acceso a Microsoft Graph API.

ÚNICO método de conexión: Microsoft Graph API (OAuth2 Client Credentials,
app-only). Sin flujos interactivos de ningún tipo.

Arquitectura:
  ✅ Graph API oficial v1.0 (HTTP directo, tokens Bearer)
  ✅ OAuth2 Client Credentials via MSAL ConfidentialClientApplication
  ✅ App registration propio en Azure AD (Mail.ReadWrite, Mail.Send, User.Read.All)
  ✅ Token cache en memoria con renovación automática
  ✅ Recursos mínimos (~256MB RAM, sin navegador, sin Chromium)
"""
