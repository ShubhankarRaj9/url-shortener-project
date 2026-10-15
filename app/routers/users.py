from fastapi import APIRouter

from app.deps import CurrentUser
from app.schemas import UserOut

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("/me", response_model=UserOut, summary="Current user profile")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
