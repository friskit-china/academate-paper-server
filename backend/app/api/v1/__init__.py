from fastapi import APIRouter

from app.api.v1.admin_invitations import router as admin_invitations_router
from app.api.v1.admin_users import router as admin_users_router
from app.api.v1.annotations import router as annotations_router
from app.api.v1.auth import router as auth_router
from app.api.v1.credentials import router as credentials_router
from app.api.v1.health import router as health_router
from app.api.v1.local_sessions import router as local_sessions_router
from app.api.v1.me import router as me_router
from app.api.v1.onboarding import router as onboarding_router
from app.api.v1.paper_maps import router as paper_maps_router
from app.api.v1.papers import router as papers_router
from app.api.v1.runtime import router as runtime_router
from app.api.v1.settings import router as settings_router
from app.api.v1.setup import router as setup_router
from app.domain.runtime import IdentityMode


def build_api_v1_router(identity_mode: IdentityMode) -> APIRouter:
    router = APIRouter()
    router.include_router(annotations_router)
    router.include_router(credentials_router)
    router.include_router(health_router)
    if identity_mode is IdentityMode.TRUSTED_LOCAL:
        router.include_router(local_sessions_router)
    else:
        router.include_router(auth_router)
        router.include_router(admin_invitations_router)
        router.include_router(admin_users_router)
        router.include_router(me_router)
        router.include_router(onboarding_router)
    router.include_router(papers_router)
    router.include_router(paper_maps_router)
    router.include_router(runtime_router)
    router.include_router(setup_router)
    router.include_router(settings_router)
    return router


# Backwards-compatible import for tests/tools that inspect the legacy trusted-local API graph.
router = build_api_v1_router(IdentityMode.TRUSTED_LOCAL)
