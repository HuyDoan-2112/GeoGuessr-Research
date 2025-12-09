from typing import Dict, Any


class InfoTools:
    async def describe_scene(self) -> str:
        """Describe the current scene in detail."""
        return "A detailed description of the current scene."

    async def get_observation(self) -> Dict[str, Any]:
        """Get the current observation from the agent's perspective."""
        return {"observation": "data"}

    async def survey_environment_plan(self) -> Dict[str, Any]:
        """
        Recommend a 360-degree scanning plan without modifying the UI.
        """
        return {
            "type": "plan",
            "action": "survey_environment",
            "plan": [
                {"rotate": "N", "angle": 0},
                {"rotate": "E", "angle": 90},
                {"rotate": "S", "angle": 180},
                {"rotate": "W", "angle": 270},
                {"zoom_out": True},
                {"zoom_in": True},
            ],
            "note": "Execute these steps with the rotate_view() and zoom() tools.",
        }
