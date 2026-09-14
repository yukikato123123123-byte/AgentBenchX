"""Controlled timeout. Must run only inside the Mac worker sandbox."""

import time


def agent_main(input: dict) -> str:
    print("Controlled timeout agent started", flush=True)
    while True:
        time.sleep(1)
