#This will control operational states of the Raspberry Pi

"""
1a. Wait in an idle and ideally low power state until an image is received
1b. Or until a packet arrives (receive) to be relayed

2a. If motion sensor (Adafruit PIR (motion) sensor) sets GPIO (assume any pin for now)
pin high, trigger camera to take image...
3a. Take image using the infrared camera, connected to the camera port of the pi
4a. if image, Run the detect.py computer vision pipeline, produces a packet with
 - animal species
 - GPS coordinates (from waveshare)
 - time (from waveshare)
2b. Process and relay the packet to the next node or PapaDuck
"""

#still need to determine what GPIO pin to use on Pi5 for motion detection


# a collection of helpers to call CLI programs we have written so far
import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Tuple, Optional
#state machine imports
import secrets
from enum import Enum, auto
from typing import Optional

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

from pathlib import Path

CAMERA_CLI = Path("/home/micha/DuckWild/camera_cli.py")  # needs to be put on pi still
DETECT_CLI = Path("/home/micha/DuckWild/species-pipeline/detect.py")
PREDICTIONS_JSON = Path("/home/micha/DuckWild/species-pipeline/speciesnet_results.json")

IMAGE_DIR = Path("/home/micha/DuckWild/captured_images")
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_COUNTRY = "USA"
DEFAULT_ADMIN1 = "CA"
DEFAULT_MIN_CONF = 0.50


def timestamped_image_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return IMAGE_DIR / f"capture_{stamp}.jpg"


def run_camera_cli_capture(output_path: Path) -> None:
    """
    Call your camera CLI to take a photo and write to output_path.
    We do NOT use --wait-for-gpio here; GPIO is handled by the FSM.
    """
    cmd = [
        sys.executable,
        str(CAMERA_CLI),
        "photo",
        "-o",
        str(output_path),
    ]
    logging.info("Running camera CLI: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def run_speciesnet_cli(image_path: Path) -> None:
    """
    Call detect.py on a single image. It will write PREDICTIONS_JSON.
    """
    cmd = [
        sys.executable,
        str(DETECT_CLI),
        str(image_path),
        "--country",
        DEFAULT_COUNTRY,
        "--admin1_region",
        DEFAULT_ADMIN1,
        "--min_conf",
        str(DEFAULT_MIN_CONF),
        "--json",
        str(PREDICTIONS_JSON),
        # you can also pass --out_csv if you want
    ]
    logging.info("Running SpeciesNet CLI: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def parse_species_from_json(image_path: Path) -> Tuple[str, float]:
    """
    Read species + confidence from SpeciesNet JSON for this image.
    """
    if not PREDICTIONS_JSON.exists():
        raise RuntimeError(f"Predictions JSON not found: {PREDICTIONS_JSON}")

    with PREDICTIONS_JSON.open("r", encoding="utf-8") as f:
        data = json.load(f)

    preds = data.get("predictions", [])
    if not preds:
        raise RuntimeError("No predictions found in JSON")

    # Filter for this image filename, if present
    filename = image_path.name
    candidates = [p for p in preds if Path(p.get("filepath", "")).name == filename]
    if not candidates:
        candidates = preds  # fallback: just use highest-conf across all

    best = max(
        candidates,
        key=lambda p: float(p.get("prediction_score") or 0.0),
    )
    species = best.get("prediction", "")
    conf = float(best.get("prediction_score") or 0.0)
    if not species:
        raise RuntimeError("Empty species in prediction")

    return species, conf


MOTION_PIN = 4  # BCM pin for PIR (match your CLI default if you want)
MAX_HOPS = 4


class State(Enum):
    IDLE = auto()
    PROCESSING_IMAGE = auto()
    ERROR = auto()


class DetectorDuckFSM:
    def __init__(self, duck_id: int):
        self.state = State.IDLE
        self.duck_id = duck_id
        self._setup_gpio()

    def _setup_gpio(self):
        if GPIO is None:
            raise RuntimeError("RPi.GPIO not available; must run on Pi with GPIO lib.")
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(MOTION_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        # rising edge -> callback
        GPIO.add_event_detect(
            MOTION_PIN, GPIO.RISING, callback=self._on_motion, bouncetime=200
        )

    # -------------------
    # Event entry points
    # -------------------

    def _on_motion(self, channel: int):
        logging.info("Motion detected on pin %d", channel)
        # Simple: just call handler inline. For heavy work, you might queue an event.
        if self.state == State.IDLE:
            self.handle_motion_event()

    def handle_motion_event(self):
        try:
            self.state = State.PROCESSING_IMAGE

            # 2a: capture image
            image_path = timestamped_image_path()
            run_camera_cli_capture(image_path)

            # 3a + 4a: run SpeciesNet and parse result
            run_speciesnet_cli(image_path)
            species, conf = parse_species_from_json(image_path)
            logging.info("SpeciesNet: %s (%.3f)", species, conf)

            # TODO: get GPS + time from Waveshare, build packet, send it
            # lat, lon = get_gps_from_hat()
            # timestamp = get_time_from_hat()

            # pkt = self.build_wild_packet(
            #     species=species, confidence=conf,
            #     image_path=image_path, lat=lat, lon=lon, timestamp=timestamp
            # )
            # self.send_packet(pkt)

        except Exception:
            logging.exception("Error in motion handler")
            self.state = State.ERROR
        else:
            self.state = State.IDLE

    # ------------------------
    # Packet processing stubs
    # ------------------------

    def receive_packet(self, raw: bytes):
        """
        Called whenever the radio receives bytes.
        """
        # pkt = CdpPacket.decode(raw)
        # if self.should_forward(pkt):
        #     pkt.hop_count += 1
        #     self.send_packet(pkt)
        pass

    def send_packet(self, pkt):
        """
        Serialize and send over radio.
        """
        raw = pkt.encode()
        # radio.send(raw)
        logging.info("Sending packet of %d bytes", len(raw))

    def should_forward(self, pkt) -> bool:
        if pkt.sduid == self.duck_id:
            return False
        if pkt.hop_count >= MAX_HOPS:
            return False
        return True

    # -------------------
    # Lifecycle
    # -------------------

    def run_forever(self):
        logging.info("FSM running, initial state: %s", self.state.name)
        try:
            while True:
                # Could also poll radio here and call receive_packet()
                time.sleep(0.1)
        except KeyboardInterrupt:
            logging.info("Shutting down")
        finally:
            if GPIO:
                GPIO.cleanup()

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    duck_id = secrets.randbits(64)
    fsm = DetectorDuckFSM(duck_id)
    fsm.run_forever()

if __name__ == "__main__":
    main()