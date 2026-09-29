from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.deps import DbSession, SettingsDep
from app.models import User
from app.schemas import LoginRequest, RegisterRequest, RegisterResponse, TokenResponse, UserOut
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(tags=["auth"])


def _token_response(user: User, settings: Settings) -> dict:
    return {
        "access_token": create_access_token(user.id, settings),
        "token_type": "bearer",
        "expires_in": settings.jwt_expire_minutes * 60,
    }


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, db: DbSession, settings: SettingsDep) -> RegisterResponse:
    user = User(username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Username is already taken") from None
    return RegisterResponse(user=UserOut.model_validate(user), **_token_response(user, settings))


@router.post("/login")
def login(body: LoginRequest, db: DbSession, settings: SettingsDep) -> TokenResponse:
    user = db.scalar(select(User).where(User.username == body.username))
    if not verify_password(body.password, user.password_hash if user else None):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenResponse(**_token_response(user, settings))
