from typing import Dict, Any


class NavTools:
    def rotate_view(self, angle: float) -> Dict[str, Any]:
        """Rotate the agent's view by a specified angle in degrees."""
        return {"type": "rotate", "angle": angle}

    def zoom(self, level: float) -> Dict[str, Any]:
        """Adjust zoom level."""
        return {"type": "zoom", "level": level}

    def move(self, direction: str) -> Dict[str, Any]:
        """Generic move helper so callers can provide any direction string."""
        return {"type": "move", "direction": direction}

    def move_north(self) -> Dict[str, Any]:
        return self.move("N")

    def move_northeast(self) -> Dict[str, Any]:
        return self.move("NE")

    def move_south(self) -> Dict[str, Any]:
        return self.move("S")

    def move_east(self) -> Dict[str, Any]:
        return self.move("E")

    def move_west(self) -> Dict[str, Any]:
        return self.move("W")
