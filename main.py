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
# Circular buffer to store exactly 700 price points
price_buffer: List[Tuple[int, float]] = []
MAX_BUFFER_SIZE = 1200

# Store the latest price received from Binance
latest_price_data = {"price": None, "timestamp": None}

# Store the current game state
current_game = {
    "cycle_start": None,
    "start_price": None,
    "end_price": None,
    "result": None,
}


# Receive data from WebSocket and store the latest price
async def binance_listener():
    url = "wss://fstream.binance.com/ws/btcusdt@trade"
    reconnect_delay = 1  # Start with 1 second, exponential backoff
    max_reconnect_delay = 60  # Max 60 seconds between reconnects

    while True:
        try:
            print(f"Connecting to Binance WebSocket: {url}")

            # Connect with ping/pong handling
            async with websockets.connect(
                url,
                ping_interval=180,  # Send ping every 3 minutes (180 seconds)
                ping_timeout=600,  # Wait up to 10 minutes for pong response
                close_timeout=10,
            ) as ws:
                print("Connected to Binance WebSocket")
                reconnect_delay = 1  # Reset delay on successful connection

                # Track connection time for 24-hour limit
                connection_start = time.time()

                async for message in ws:
                    try:
                        # Check if we've been connected for nearly 24 hours
                        if time.time() - connection_start > 23.5 * 3600:  # 23.5 hours
                            print(
                                "Approaching 24-hour connection limit, reconnecting..."
                            )
                            break

                        data = json.loads(message)
                        # Update the latest price data immediately
                        latest_price_data["price"] = float(data["p"])
                        latest_price_data["timestamp"] = int(data["T"])

                    except json.JSONDecodeError as e:
                        print(f"Error parsing JSON: {e}")
                        continue
                    except KeyError as e:
                        print(f"Missing expected field in WebSocket message: {e}")
                        continue
                    except Exception as e:
                        print(f"Error processing WebSocket message: {e}")
                        continue

        except websockets.exceptions.ConnectionClosed as e:
            print(f"WebSocket connection closed: {e}")
        except websockets.exceptions.InvalidStatusCode as e:
            print(f"WebSocket connection failed with status code: {e}")
        except Exception as e:
            print(f"WebSocket connection error: {e}")

        # Exponential backoff for reconnection
        print(f"Reconnecting in {reconnect_delay} seconds...")
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)


# Send price data every 75ms using the latest available price
async def price_broadcaster():
    while True:
        start_time = time.time()

        # Only send if we have valid price data
        if (
            latest_price_data["price"] is not None
            and latest_price_data["timestamp"] is not None
        ):
            current_price = latest_price_data["price"]
            # Use the actual current time for the 75ms interval timestamp
            current_timestamp = int(time.time() * 1000)

            # Add the 75ms interval data to buffer
            price_buffer.append((current_timestamp, current_price))
            # Keep only the last MAX_BUFFER_SIZE data points
            if len(price_buffer) > MAX_BUFFER_SIZE:
                price_buffer.pop(0)

            # Broadcast to all clients with the 75ms interval timestamp
            await broadcast(
                clients_ws1,
                json.dumps({"price": current_price, "timestamp": current_timestamp}),
            )

        # Calculate precise sleep time to maintain 75ms intervals
        elapsed = (time.time() - start_time) * 1000  # Convert to ms
        sleep_time = max(0, (75 - elapsed) / 1000)  # Convert back to seconds
        await asyncio.sleep(sleep_time)


# Broadcast helper
async def broadcast(clients: List[WebSocket], message: str):
    disconnected_clients = []
    for client in clients:
        try:
            await client.send_text(message)
        except:
            disconnected_clients.append(client)

    # Remove disconnected clients
    for client in disconnected_clients:
        if client in clients:
            clients.remove(client)


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

    # send all available data (up to MAX_BUFFER_SIZE points) on connect
    history = [{"price": price, "timestamp": ts} for ts, price in price_buffer]
    await websocket.send_text(json.dumps({"type": "history", "data": history}))

    try:
        while True:
            await asyncio.sleep(10)
    except WebSocketDisconnect:
        if websocket in clients_ws1:
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
        if websocket in clients_ws2:
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
    # Start the Binance WebSocket listener
    asyncio.create_task(binance_listener())
    # Start the price broadcaster that sends data every 75ms
    asyncio.create_task(price_broadcaster())
    # Start the game loop
    asyncio.create_task(game_loop())
