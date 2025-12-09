from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional

from pydantic import BaseModel
from typing import List, Dict, Optional

class Move(BaseModel):
    heading: float
    next_node_id: str

class MetaInfo(BaseModel):
    step_count: int
    history: List[str]

class Observation(BaseModel):
    current_node_id: Optional[str] = None
    gps: Optional[Dict[str, float]] = None
    current_heading: Optional[float] = None
    available_moves: Optional[List[Move]] = []
    image: Optional[str] = None
    meta: Optional[MetaInfo] = None   # ← matches frontend

class AgentAction(BaseModel):
    type: str  # e.g., "rotate", "move", "zoom"
    angle: Optional[float] = None  # for rotate
    next_pano_id: Optional[str] = None  # for move
    heading: Optional[float] = None     # optional heading for move


class StepPayLoad(BaseModel):
    observation: Observation


def parse_observation_payload(payload: Dict) -> Observation:
    """
    Normalize both {"observation": {...}} and flat observation payloads into
    an Observation model.
    """
    if isinstance(payload, Observation):
        return payload
    if isinstance(payload, StepPayLoad):
        return payload.observation
    if isinstance(payload, dict):
        return Observation.model_validate(payload.get("observation", payload))
    raise ValueError("Invalid observation payload")
