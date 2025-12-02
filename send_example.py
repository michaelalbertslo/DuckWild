import os
import sys
import time

from LoRaRF import LoRaGpio, LoRaSpi, SX126x

from packet import CdpPacket, DuckType, Topic, UnknownData

# Begin LoRa radio with connected SPI bus and IO pins (cs and reset) on GPIO
# SPI is defined by bus ID and cs ID and IO pins defined by chip and offset number
spi = LoRaSpi(0, 0)
cs = LoRaGpio(0, 8)
reset = LoRaGpio(0, 24)
busy = LoRaGpio(0, 23)
LoRa = SX126x(spi, cs, reset, busy)
print("Begin LoRa radio")
if not LoRa.begin():
    raise Exception("Something wrong, can't begin LoRa radio")

# Configure LoRa to use TCXO with DIO3 as control
print("Set RF module to use TCXO as clock reference")
LoRa.setDio3TcxoCtrl(LoRa.DIO3_OUTPUT_1_8, LoRa.TCXO_DELAY_10)

# Set frequency to 915 Mhz
print("Set frequency to 915 Mhz")
LoRa.setFrequency(915000000)

# Set TX power, default power for SX1262 and SX1268 are +22 dBm and for SX1261 is +14 dBm
# This function will set PA config with optimal setting for requested TX power
print("Set TX power to +17 dBm")
LoRa.setTxPower(17, LoRa.TX_POWER_SX1262)  # TX power +17 dBm using PA boost pin

# https://github.com/ClusterDuck-Protocol/ClusterDuck-Protocol/blob/c8a6ed14469c7bbbf293c963d1b8df776f30193f/src/radio/DuckLoRa.cpp#L27
sf = 7
bw = 125000  # Bandwidth: 125 kHz
cr = 7  # https://github.com/jgromes/RadioLib/blob/3a5dac095d9f4c823fdc8a9e8f8e427244134981/src/modules/SX126x/SX1262.h#L37
LoRa.setLoRaModulation(sf, bw, cr)

# Configure packet parameter including header type, preamble length, payload length, and CRC type
# The explicit packet includes header contain CR, number of byte, and CRC type
# Receiver can receive packet with different CR and packet parameters in explicit header mode
headerType = LoRa.HEADER_EXPLICIT  # Explicit header mode
preambleLength = 8  # Set preamble length to 12
payloadLength = 255  # Initialize payloadLength to 15
crcType = True  # Set CRC enable
LoRa.setLoRaPacket(headerType, preambleLength, payloadLength, crcType)

# Set syncronize word for private network (0x1424)
print("Set syncronize word to 0x1424")
LoRa.setSyncWord(0x1424)

print("\n-- LoRa Transmitter --\n")

# Transmit message continuously
message_num = 1
while True:
    # Transmit message and counter
    # write() method must be placed between beginPacket() and endPacket()
    LoRa.beginPacket()
    data = UnknownData(b"animal name")
    packet = CdpPacket(67, 2, message_num, Topic.WILD, DuckType.MAMA, 1, data)
    message_num += 1
    LoRa.write([packet], 1)
    LoRa.endPacket()
    print("sent ", packet.data)

    # Wait until modulation process for transmitting packet finish
    LoRa.wait()

    # Print transmit time and data rate
    print(
        "Transmit time: {0:0.2f} ms | Data rate: {1:0.2f} byte/s".format(
            LoRa.transmitTime(), LoRa.dataRate()
        )
    )

    # Don't load RF module with continous transmit
    time.sleep(5)
