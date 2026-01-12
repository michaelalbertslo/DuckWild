#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Optional, Tuple

# ---- radio stack ----
from duck import Duck
from packet import DuckType, Topic, UnknownData
# need to change this to make a dynamic DUID for each duck
TARGET_DUID = 8331  # .DUID

# ---- PIR / camera / detection config ----
try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

CAMERA_CLI = Path("/home/micha/DuckWild/camera_cli.py")
PIPELINE_DIR = Path("/home/micha/DuckWild/species-pipeline")
DETECT_CLI = PIPELINE_DIR / "detect.py"
PREDICTIONS_JSON = PIPELINE_DIR / "speciesnet_results_fsm.json"

IMAGE_DIR = Path("/home/micha/DuckWild/captured_images")
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

MOTION_PIN = 4  # BCM
# make this dynamic based on GPS ? A parameter --GPS to turn all that on
DEFAULT_COUNTRY = "USA"
DEFAULT_ADMIN1 = "CA"
DEFAULT_MIN_CONF = 0.50

TICK_HZ = 1.0  # run loop will call tick() ~1x/sec


# ---------- small helpers ----------
def _timestamped_image_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return IMAGE_DIR / f"capture_{stamp}.jpg"


def _run_camera_cli_capture(output_path: Path) -> None:
    """Capture a photo immediately (GPIO handled by FSM)."""
    cmd = [sys.executable, str(CAMERA_CLI), "photo", "-o", str(output_path)]
    logging.info("Running camera CLI: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def _run_speciesnet_cli(image_path: Path) -> Path:
    """Run SpeciesNet via uv; return the JSON path used this run."""
    # Avoid resume mismatch: always start with a fresh JSON for FSM runs
    if PREDICTIONS_JSON.exists():
        PREDICTIONS_JSON.unlink()

    cmd = [
        "uv", "run", "python",
        str(DETECT_CLI),
        str(image_path),
        "--country", DEFAULT_COUNTRY,
        "--admin1_region", DEFAULT_ADMIN1,
        "--min_conf", str(DEFAULT_MIN_CONF),
        "--json", str(PREDICTIONS_JSON),
    ]
    logging.info("Running SpeciesNet CLI: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(PIPELINE_DIR))
    return PREDICTIONS_JSON


def _parse_species_from_json(image_path: Path, json_path: Path) -> Tuple[str, float]:
    if not json_path.exists():
        raise RuntimeError(f"Predictions JSON not found: {json_path}")
    with json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    preds = data.get("predictions", [])
    if not preds:
        raise RuntimeError("No predictions found in JSON")

    filename = image_path.name
    candidates = [p for p in preds if Path(p.get("filepath", "")).name == filename]
    if not candidates:
        candidates = preds
    best = max(candidates, key=lambda p: float(p.get("prediction_score") or 0.0))

    species = best.get("prediction", "")
    conf = float(best.get("prediction_score") or 0.0)
    if not species:
        raise RuntimeError("Empty species in prediction")

    return species, conf


# ---------- FSM + Duck integration ----------
class _State(Enum):
    IDLE = auto()
    PROCESSING = auto()
    ERROR = auto()


class WildDuck(Duck):
    """
    Tick-driven duck with a background FSM:
      - PIR callback enqueues motion
      - worker thread: capture -> detect -> send()
      - tick(): keep it non-blocking; handle receive/periodic
    """

    def __init__(self, duid: int, target_duid: int = TARGET_DUID):
        super().__init__(DuckType.UNKNOWN, duid, 1)
        self._state = _State.IDLE
        self._tick_counter = 0
        self._target_duid = target_duid

        # motion queue + worker thread
        self._motion_q: "queue.Queue[float]" = queue.Queue()
        self._worker_alive = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

        self._setup_gpio()
        logging.info("WildDuck ready (duid=%s, target_dduid=%s)", duid, target_duid)

    # ---- GPIO ----
    def _setup_gpio(self) -> None:
        if GPIO is None:
            raise RuntimeError("RPi.GPIO not available on this system.")
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(MOTION_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        GPIO.add_event_detect(MOTION_PIN, GPIO.RISING, callback=self._on_motion, bouncetime=500)
        logging.info("Configured PIR on BCM pin %d", MOTION_PIN)

    def _on_motion(self, channel: int):
        logging.info("Motion detected on pin %d", channel)
        try:
            self._motion_q.put_nowait(time.time())
        except queue.Full:
            pass

    # ---- Duck tick / receive ----
    def tick(self):
        """Non-blocking; called by Duck.run()."""
        self._tick_counter += 1

        # If your Duck base class exposes a non-blocking receive, poll it here.
        # If it calls a callback automatically, you can delete this block.
        try:
            if hasattr(self, "try_receive_nowait"):
                pkt = self.try_receive_nowait()  # type: ignore[attr-defined]
                if pkt:
                    self._handle_rx(pkt)
        except Exception:
            logging.exception("Receive poll failed")

        if self._tick_counter % int(TICK_HZ * 10) == 0:
            logging.debug("tick state=%s", self._state.name)

    # If your Duck base class uses a callback for RX, you can implement it like this:
    # def on_receive(self, packet) -> None:
    #     self._handle_rx(packet)

    def _handle_rx(self, pkt) -> None:
        logging.info("RX: %r", pkt)
        # Add any relay/forward rules here if needed

    # ---- background worker: do the slow stuff ----
    def _worker_loop(self):
        while self._worker_alive:
            try:
                _ = self._motion_q.get(timeout=0.25)
            except queue.Empty:
                continue

            if self._state is not _State.IDLE:
                logging.info("Ignoring motion; state=%s", self._state.name)
                continue

            try:
                self._state = _State.PROCESSING

                # 1) capture
                image_path = _timestamped_image_path()
                logging.info("Capturing image -> %s", image_path)
                _run_camera_cli_capture(image_path)

                # 2) detect
                json_path = _run_speciesnet_cli(image_path)
                species, conf = _parse_species_from_json(image_path, json_path)

                logging.info("SpeciesNet result: %s (%.3f)", species, conf)
                print(f"Detected species: {species} (confidence={conf:.3f})")

                # 3) build payload and send via your real radio call
                payload = {
                    "species": species,
                    "confidence": conf,
                    "image": image_path.name,
                    "ts": datetime.utcnow().isoformat() + "Z",
                }
                self.send(self._target_duid, Topic.WILD, UnknownData(json.dumps(payload).encode("utf-8")))
                logging.info("Sent WILD payload to dduid=%s", self._target_duid)

            except subprocess.CalledProcessError as e:
                logging.error("Subprocess error: %s", e)
                self._state = _State.ERROR
            except Exception:
                logging.exception("Unexpected error during motion handling")
                self._state = _State.ERROR
            else:
                self._state = _State.IDLE

    # ---- cleanup ----
    def close(self):
        self._worker_alive = False
        try:
            self._worker_thread.join(timeout=1.0)
        except Exception:
            pass
        if GPIO:
            try:
                GPIO.cleanup()
            except Exception:
                pass


# ---- example entrypoint wiring Duck.run() ----
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    # pick a stable DUID for this device
    my_duid = 1337  # like your SendDuck
    duck = WildDuck(duid=my_duid, target_duid=TARGET_DUID)
    try:
        duck.run()  # your Duck base calls tick() on a schedule
    except KeyboardInterrupt:
        logging.info("Shutting down")
    finally:
        duck.close()
