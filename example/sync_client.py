#!/usr/bin/env python

# WS sync client example

import websockets.sync.client  # TODO

import logging
logging.basicConfig(level=logging.DEBUG)

def hello():
    uri = "ws://localhost:8765"
    with websockets.sync.client.connect(uri) as websocket:
        name = input("What's your name? ")

        websocket.send(name)
        print(f"> {name}")

        greeting = websocket.recv()
        print(f"< {greeting}")

hello()
