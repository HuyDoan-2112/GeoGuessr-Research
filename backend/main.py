from fastapi import FastAPI
from api.state import router as state_router
from api.update import router as update_router
from api.actions import router as action_router
from api.step import router as step_router
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # allow all for local testing
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)


app.include_router(state_router)
app.include_router(update_router)
app.include_router(action_router)
app.include_router(step_router)