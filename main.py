import asyncio
import json
import time
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional, Tuple

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
price_buffer: List[Tuple[int, float]] = []

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


# Get price at the closest timestamp
def get_price_at(target_ts: int) -> Optional[float]:
    if not price_buffer:
        return None
    closest = min(price_buffer, key=lambda x: abs(x[0] - target_ts))
    return closest[1]


# WS endpoint for raw price stream (history + live)
@app.websocket("/ws1")
async def websocket_stream_binance(websocket: WebSocket):
    await websocket.accept()
    clients_ws1.append(websocket)

    # send last 60s of data on connect
    now = int(time.time() * 1000)
    history = [
        {"price": price, "timestamp": ts}
        for ts, price in price_buffer
        if ts >= now - 60_000
    ]
    await websocket.send_text(json.dumps({"type": "history", "data": history}))

    try:
        while True:
            await asyncio.sleep(10)
    except WebSocketDisconnect:
        clients_ws1.remove(websocket)


# WS endpoint for game state updates
@app.websocket("/ws2")
async def websocket_game_logic(websocket: WebSocket):
    await websocket.accept()
    clients_ws2.append(websocket)

    # immediately send whatever is current
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


# Game cycle manager: start_price @ +40s, end_price @ +55s, result immediately @ +55s
async def game_loop():
    cycle_duration = 60_000  # ms

    while True:
        now_ms = int(time.time() * 1000)
        cycle_start = now_ms - (now_ms % cycle_duration)

        t_start = cycle_start + 40_000
        t_end = cycle_start + 55_000
        t_cycle_end = cycle_start + cycle_duration  # == cycle_start + 60_000

        # reset state
        current_game.update(
            {
                "cycle_start": cycle_start,
                "start_price": None,
                "end_price": None,
                "result": None,
            }
        )

        # 1) wait until 40s mark
        await asyncio.sleep(max((t_start - int(time.time() * 1000)) / 1000, 0))
        start_price = get_price_at(t_start)
        current_game["start_price"] = start_price
        await broadcast(
            clients_ws2,
            json.dumps(
                {
                    "type": "start_price",
                    "price": start_price,
                }
            ),
        )

        # 2) wait until 55s mark
        await asyncio.sleep(max((t_end - int(time.time() * 1000)) / 1000, 0))
        end_price = get_price_at(t_end)
        current_game["end_price"] = end_price
        await broadcast(
            clients_ws2,
            json.dumps(
                {
                    "type": "end_price",
                    "price": end_price,
                }
            ),
        )

        # 3) compute & broadcast result immediately at 55s
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

        # 4) sleep the remaining ~5s so we wake up at the cycle's 60s mark
        await asyncio.sleep(max((t_cycle_end - int(time.time() * 1000)) / 1000, 0))


# Startup tasks
@app.on_event("startup")
async def startup():
    asyncio.create_task(binance_listener())
    asyncio.create_task(game_loop())
