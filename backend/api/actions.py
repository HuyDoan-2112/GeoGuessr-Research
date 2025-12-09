from fastapi import APIRouter
from models.schema import AgentAction
from core.environment import global_env_state as env


router = APIRouter()

@router.post("/environment/action")
def apply_action(action: AgentAction):
    updated_state = env.apply_action(action.dict())
    return updated_state
