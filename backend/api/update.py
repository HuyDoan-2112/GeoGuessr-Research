from fastapi import APIRouter, Body, HTTPException
from pydantic import ValidationError
from core.environment import global_env_state
from models.schema import parse_observation_payload

router = APIRouter()

@router.post("/environment/update")
def update_from_ui(payload: dict = Body(...)):
    """Update environment state from the UI or agent."""
    try:
        obs = parse_observation_payload(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())

    global_env_state.update_state(
        current_node_id=obs.current_node_id,
        gps=obs.gps,
        current_heading=obs.current_heading,
        available_moves=obs.available_moves,
        image=obs.image,
        metadata=obs.meta,
    )
    return {"status": "updated", "state": global_env_state.get_state()}
