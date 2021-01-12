#!/usr/bin/env python

# WS sync server example

import websockets.sync.server  # TODO

import logging
logging.basicConfig(level=logging.DEBUG)

def hello(websocket, path):
    name = websocket.recv()
    print(f"< {name}")

    greeting = f"Hello {name}!"

    websocket.send(greeting)
    print(f"> {greeting}")

websockets.sync.server.serve(hello, "localhost", 8765)
