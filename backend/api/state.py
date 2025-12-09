from fastapi import APIRouter
from core.environment import global_env_state


router = APIRouter()

@router.get("/environment/state")
async def get_environment_state():
    """Get the current state of the environment."""
    return global_env_state.get_state()