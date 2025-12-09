from fastapi import APIRouter, Body, HTTPException
from pydantic import ValidationError
from core.environment import global_env_state
from models.schema import parse_observation_payload
from fastapi.encoders import jsonable_encoder

router = APIRouter()


@router.post("/step")
def step(payload: dict = Body(...)):
    """Handle a step payload from the UI/agent and refresh the global state."""
    try:
        print(payload)
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

    return {
        "message": "Environment updated",
        "new_state":  jsonable_encoder(global_env_state.get_state()),
    }