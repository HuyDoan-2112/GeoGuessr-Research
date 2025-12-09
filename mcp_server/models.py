# model
from typing import Literal, Dict, Any
from pydantic import BaseModel

Direction = Literal["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

DirectionAngles = {
    "N": 0,
    "NE": 45,
    "E": 90,
    "SE": 135,
    "S": 180,
    "SW": 225,
    "W": 270,
    "NW": 315,
}

class NavAction(BaseModel):
    type: str = "nav_action",
    name: str
    direction: str | None = None
    heading_angle: float | None = None
    meta: Dict[str, Any] | None = None


class ObservationReport(BaseModel):
    type: str = "observation_report"
    text: str