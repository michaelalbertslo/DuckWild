import argparse
import time
from datetime import datetime
from pathlib import Path

from picamera2 import Picamera2

try:
    import RPi.GPIO as GPIO
except ImportError:  # pragma: no cover - requires Raspberry Pi hardware
    GPIO = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture a still photo or record a video with Picamera2."
    )
    parser.add_argument(
        "mode",
        choices=["photo", "video"],
        help="capture a still photo or record a video",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="file to write (default: timestamped file in current directory)",
    )
    parser.add_argument(
        "-d",
        "--duration",
        type=float,
        default=5.0,
        help="video length in seconds (video mode only)",
    )
    parser.add_argument(
        "--wait-for-gpio",
        action="store_true",
        help=(
            "wait until the configured GPIO pin goes HIGH before capturing "
            "(photo mode only)"
        ),
    )
    parser.add_argument(
        "--gpio-pin",
        type=int,
        default=4,
        help="BCM pin number to monitor when --wait-for-gpio is used (default: 4)",
    )
    return parser.parse_args()


def timestamped_filename(extension: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(f"capture_{stamp}.{extension}")


def capture_photo(picam2: Picamera2, output_path: Path) -> None:
    picam2.configure(picam2.create_still_configuration())
    picam2.start()
    time.sleep(2)  # allow auto settings to settle
    picam2.capture_file(str(output_path))
    picam2.stop()


def record_video(picam2: Picamera2, output_path: Path, duration: float) -> None:
    picam2.configure(picam2.create_video_configuration())
    picam2.start_recording(str(output_path))
    time.sleep(duration)
    picam2.stop_recording()
    picam2.stop()


def wait_for_gpio_high(pin: int) -> None:
    if GPIO is None:
        raise RuntimeError(
            "RPi.GPIO is required for GPIO triggers. Install it and run on a Raspberry Pi."
        )

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    print(f"Waiting for GPIO pin {pin} to go HIGH...")
    try:
        GPIO.wait_for_edge(pin, GPIO.RISING)
    finally:
        GPIO.cleanup(pin)
    print(f"GPIO pin {pin} went HIGH. Capturing photo...")


def main() -> None:
    args = parse_args()
    picam2 = Picamera2()
    output = args.output

    try:
        if args.mode == "photo":
            if args.wait_for_gpio:
                wait_for_gpio_high(args.gpio_pin)
            if output is None:
                output = timestamped_filename("jpg")
            capture_photo(picam2, output)

        else:  # video mode
            if args.wait_for_gpio:
                raise ValueError("GPIO triggering is only supported in photo mode")
            if output is None:
                output = timestamped_filename("mp4")
            record_video(picam2, output, args.duration)

    finally:
        picam2.close()


if __name__ == "__main__":
    main()
