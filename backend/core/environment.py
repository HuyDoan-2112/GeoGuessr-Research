
class EnvironmentState:
    def __init__(self):
        self.current_node_id = None
        self.gps = None
        self.current_heading = None
        self.available_moves = []
        self.image = None # base64 string
        self.metadata = {}

    def get_state(self):
        return {
            "current_node_id": self.current_node_id,
            "gps": self.gps,
            "current_heading": self.current_heading,
            "available_moves": self.available_moves,
            "image": self.image,
            "metadata": self.metadata,
        }
    
    def update_state(self, current_node_id, gps, current_heading, available_moves, image, metadata):
        self.current_node_id = current_node_id
        self.gps = gps
        self.current_heading = current_heading
        self.available_moves = available_moves
        self.image = image
        self.metadata = metadata


#  Global environment instance
global_env_state = EnvironmentState()

