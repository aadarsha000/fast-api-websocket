import asyncio
import json
import time
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import List

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

clients_ws1: List[WebSocket] = []
clients_ws2: List[WebSocket] = []
price_buffer = []

# Store the current game state
current_game = {
    "cycle_start": None,
    "start_price": None,
    "end_price": None,
    "result": None,
}


# Connect to Binance stream and update price buffer
async def binance_listener():
    url = "wss://fstream.binance.com/ws/btcusdt@trade"
    async with websockets.connect(url) as ws:
        async for message in ws:
            data = json.loads(message)
            price = float(data["p"])
            timestamp = int(data["T"])
            price_buffer.append((timestamp, price))
            if len(price_buffer) > 5000:
                price_buffer.pop(0)
            await broadcast(
                clients_ws1, json.dumps({"price": price, "timestamp": timestamp})
            )


# Broadcast helper
async def broadcast(clients: List[WebSocket], message: str):
    for client in clients:
        try:
            await client.send_text(message)
        except:
            pass


# Get price at closest timestamp
def get_price_at(target_ts):
    if not price_buffer:
        return None
    closest = min(price_buffer, key=lambda x: abs(x[0] - target_ts), default=None)
    return closest[1] if closest else None


# WebSocket endpoint for Binance data (WS1)
@app.websocket("/ws1")
async def websocket_stream_binance(websocket: WebSocket):
    await websocket.accept()
    clients_ws1.append(websocket)
    now = int(time.time() * 1000)
    historical_data = [
        {"price": price, "timestamp": ts}
        for ts, price in price_buffer
        if ts >= now - 60_000
    ]

    await websocket.send_text(json.dumps({"type": "history", "data": historical_data}))
    try:
        while True:
            await asyncio.sleep(10)
    except WebSocketDisconnect:
        clients_ws1.remove(websocket)


# WebSocket endpoint for game state (WS2)
@app.websocket("/ws2")
async def websocket_game_logic(websocket: WebSocket):
    await websocket.accept()
    clients_ws2.append(websocket)

    # Send current game state if available
    if current_game["start_price"] is not None:
        await websocket.send_text(
            json.dumps({"type": "start_price", "price": current_game["start_price"]})
        )
    if current_game["end_price"] is not None:
        await websocket.send_text(
            json.dumps({"type": "end_price", "price": current_game["end_price"]})
        )
    if current_game["result"] is not None:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "result",
                    "result": current_game["result"],
                    "start_price": current_game["start_price"],
                    "end_price": current_game["end_price"],
                }
            )
        )

    try:
        while True:
            await asyncio.sleep(10)
    except WebSocketDisconnect:
        clients_ws2.remove(websocket)


# Game cycle manager
async def game_loop():
    while True:
        now = int(time.time() * 1000)
        cycle_duration = 60000
        current_cycle_start = now - (now % cycle_duration)

        start_price_at = current_cycle_start + 40000
        end_price_at = current_cycle_start + 55000
        result_at = current_cycle_start + 60000

        current_game["cycle_start"] = current_cycle_start
        current_game["start_price"] = None
        current_game["end_price"] = None
        current_game["result"] = None

        await asyncio.sleep((start_price_at - int(time.time() * 1000)) / 1000)
        start_price = get_price_at(start_price_at)
        current_game["start_price"] = start_price
        await broadcast(
            clients_ws2, json.dumps({"type": "start_price", "price": start_price})
        )

        await asyncio.sleep((end_price_at - int(time.time() * 1000)) / 1000)
        end_price = get_price_at(end_price_at)
        current_game["end_price"] = end_price
        await broadcast(
            clients_ws2, json.dumps({"type": "end_price", "price": end_price})
        )

        await asyncio.sleep((end_price_at - int(time.time() * 1000)) / 1000)
        if start_price is None or end_price is None:
            result = "no_result"
        else:
            result = (
                "up"
                if end_price > start_price
                else "down" if end_price < start_price else "same"
            )
        current_game["result"] = result
        await broadcast(
            clients_ws2,
            json.dumps(
                {
                    "type": "result",
                    "result": result,
                    "start_price": start_price,
                    "end_price": end_price,
                }
            ),
        )


# Startup
@app.on_event("startup")
async def startup():
    asyncio.create_task(binance_listener())
    asyncio.create_task(game_loop())
