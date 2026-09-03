
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

app.mount("/packages", StaticFiles(directory="packages"), name="packages")

connected_agents = {}
dashboard_ws = None

@app.get("/")
async def get_dashboard():
    with open("dashboard.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.websocket("/ws/dashboard")
async def dashboard_endpoint(websocket: WebSocket):
    global dashboard_ws
    await websocket.accept()
    dashboard_ws = websocket
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        dashboard_ws = None

@app.websocket("/ws/agent/{agent_id}")
async def agent_endpoint(websocket: WebSocket, agent_id: str):
    await websocket.accept()
    connected_agents[agent_id] = websocket
    print(f"Agent {agent_id} Connected")
    try:
        while True:
            data = await websocket.receive_json()
            if dashboard_ws:
                await dashboard_ws.send_json(data)
    except WebSocketDisconnect:
        del connected_agents[agent_id]
        print(f"Agent {agent_id} Disconnected")

# Dynamic Instruction Gateway Endpoint
@app.post("/api/dispatch")
async def dispatch_instruction(request: Request):
    payload = await request.json()
    for agent_id, ws in connected_agents.items():
        await ws.send_json(payload)
    return {"status": "Instruction Dispatched", "agents": len(connected_agents)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)