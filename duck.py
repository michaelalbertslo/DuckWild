# quack
import random
import time
from collections import deque

from LoRaRF import SX126x

from packet import CdpPacket, Data, DuckType, Topic

DEQUE_LEN = 100
INITIAL_HOP_COUNT = 5
RECEIVE_DELAY = 0.01


class Duck:
    def __init__(self, duck_type: DuckType, duid: int, tps: float):
        self.type = duck_type
        self.duid = duid
        self.muids_seen = deque(maxlen=100)
        self.tps = tps

        busId = 0
        csId = 0
        resetPin = 18
        busyPin = 20
        irqPin = 16
        txenPin = 6
        rxenPin = -1

        self.lora = SX126x()
        print("Begin LoRa radio")
        if not self.lora.begin(
            busId, csId, resetPin, busyPin, irqPin, txenPin, rxenPin
        ):
            raise Exception("Something wrong, can't begin LoRa radio")

        self.lora.setDio2RfSwitch()

        # Configure LoRa to use TCXO with DIO3 as control
        # print("Set RF module to use TCXO as clock reference")
        # self.lora.setDio3TcxoCtrl(self.lora.DIO3_OUTPUT_1_8, self.lora.TCXO_DELAY_10)

        # Set frequency to 915 Mhz
        print("Set frequency to 915 Mhz")
        self.lora.setFrequency(915000000)

        # Set TX power, default power for SX1262 and SX1268 are +22 dBm and for SX1261 is +14 dBm
        # This function will set PA config with optimal setting for requested TX power
        print("Set TX power to +17 dBm")
        self.lora.setTxPower(
            17, self.lora.TX_POWER_SX1262
        )  # TX power +17 dBm using PA boost pin

        # https://github.com/ClusterDuck-Protocol/ClusterDuck-Protocol/blob/c8a6ed14469c7bbbf293c963d1b8df776f30193f/src/radio/DuckLoRa.cpp#L27
        sf = 7
        bw = 125000  # Bandwidth: 125 kHz
        cr = 7  # https://github.com/jgromes/RadioLib/blob/3a5dac095d9f4c823fdc8a9e8f8e427244134981/src/modules/SX126x/SX1262.h#L37
        self.lora.setLoRaModulation(sf, bw, cr)

        # Configure packet parameter including header type, preamble length, payload length, and CRC type
        # The explicit packet includes header contain CR, number of byte, and CRC type
        # Receiver can receive packet with different CR and packet parameters in explicit header mode
        headerType = self.lora.HEADER_EXPLICIT  # Explicit header mode
        preambleLength = 8  # Set preamble length to 12
        payloadLength = 255  # Initialize payloadLength to 15
        crcType = True  # Set CRC enable
        self.lora.setLoRaPacket(headerType, preambleLength, payloadLength, crcType)

        # Set syncronize word for private network (0x1424)
        print("Set syncronize word to 0x1424")
        self.lora.setSyncWord(0x1424)

    def on_received(self, packet: CdpPacket):
        print(
            "Received message",
            packet.muid,
            "from",
            packet.sduid,
            ":",
            packet.data,
        )

    def send(self, dduid: int, topic: Topic, data: Data):
        # Transmit message and counter
        while (
            muid := bytes(
                random.choice(b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ") for i in range(4)
            )
        ) in self.muids_seen:
            pass
        # write() method must be placed between beginPacket() and endPacket()
        self.lora.beginPacket()
        packet = CdpPacket(
            self.duid, dduid, muid, topic, self.type, INITIAL_HOP_COUNT, data
        )
        raw_packet = packet.encode()
        print(raw_packet)
        self.lora.write(list(raw_packet), len(raw_packet))
        self.lora.endPacket()
        # Wait until modulation process for transmitting packet finish
        self.lora.wait()
        self.lora.purge(len(raw_packet))

    def tick(self):
        pass

    def run(self):
        tick_duration = 1.0 / self.tps
        while True:
            entered = time.time()
            end = entered + tick_duration
            self.lora.request(self.lora.RX_CONTINUOUS)
            while time.time() < end:
                if self.lora.available() > 0:
                    message = bytearray()
                    while self.lora.available() > 0:
                        message.append(self.lora.read())
                    print(message)
                    print(
                        "Packet status: RSSI = {0:0.2f} dBm | SNR = {1:0.2f} dB".format(
                            self.lora.packetRssi(), self.lora.snr()
                        )
                    )
                    status = self.lora.status()
                    if status == self.lora.STATUS_CRC_ERR:
                        print("CRC error")
                    if status == self.lora.STATUS_HEADER_ERR:
                        print("Packet header error")
                    try:
                        cdp_packet = CdpPacket.decode(bytes(message))
                        self.on_received(cdp_packet)
                    except:
                        print("Failed to decode message!")
                time.sleep(RECEIVE_DELAY)
            self.tick()
