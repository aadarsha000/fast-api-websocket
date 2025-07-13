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
# Circular buffer to store exactly 100 price points
price_buffer: List[Tuple[int, float]] = []
MAX_BUFFER_SIZE = 100

# Store the current game state
current_game = {
    "cycle_start": None,
    "start_price": None,
    "end_price": None,
    "result": None,
}


# Receive data from WebSocket every 75ms with proper Binance WebSocket handling
async def binance_listener():
    url = "wss://fstream.binance.com/ws/btcusdt@trade"
    latest_price = None
    latest_timestamp = None
    reconnect_delay = 1  # Start with 1 second, exponential backoff
    max_reconnect_delay = 60  # Max 60 seconds between reconnects

    async def ws_receiver():
        nonlocal latest_price, latest_timestamp, reconnect_delay

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
                            if (
                                time.time() - connection_start > 23.5 * 3600
                            ):  # 23.5 hours
                                print(
                                    "Approaching 24-hour connection limit, reconnecting..."
                                )
                                break

                            data = json.loads(message)
                            latest_price = float(data["p"])
                            latest_timestamp = int(data["T"])

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

    async def price_processor():
        message_count = 0
        last_reset_time = time.time()

        while True:
            start_time = time.time()

            # Rate limiting: max 10 messages per second to clients
            current_time = time.time()
            if current_time - last_reset_time >= 1.0:
                message_count = 0
                last_reset_time = current_time

            if (
                message_count < 10
                and latest_price is not None
                and latest_timestamp is not None
            ):
                # Use the actual timestamp from Binance
                timestamp = latest_timestamp

                # Add new price to buffer
                price_buffer.append((timestamp, latest_price))
                # Keep only the last 100 data points
                if len(price_buffer) > MAX_BUFFER_SIZE:
                    price_buffer.pop(0)

                await broadcast(
                    clients_ws1,
                    json.dumps({"price": latest_price, "timestamp": timestamp}),
                )
                message_count += 1

            # Calculate how long to wait to maintain 75ms intervals
            elapsed = (time.time() - start_time) * 1000  # Convert to ms
            sleep_time = max(0, (75 - elapsed) / 1000)  # Convert back to seconds
            await asyncio.sleep(sleep_time)

    # Run both tasks concurrently
    await asyncio.gather(ws_receiver(), price_processor())


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

    # send all available data (up to 100 points) on connect
    history = [{"price": price, "timestamp": ts} for ts, price in price_buffer]
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
    # WebSocket connection with 75ms data processing
    asyncio.create_task(binance_listener())
    asyncio.create_task(game_loop())
