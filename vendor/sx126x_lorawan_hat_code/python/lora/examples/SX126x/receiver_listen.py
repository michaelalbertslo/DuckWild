import os, sys
currentdir = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.dirname(os.path.dirname(currentdir)))
from LoRaRF import SX126x
import time

# Begin LoRa radio and set NSS, reset, busy, IRQ, txen, and rxen pin with connected Raspberry Pi gpio pins
busId = 0; csId = 0 
resetPin = 18; busyPin = 20; irqPin = 16; txenPin = 6; rxenPin = -1 
LoRa = SX126x()
print("Begin LoRa radio")
if not LoRa.begin(busId, csId, resetPin, busyPin, irqPin, txenPin, rxenPin) :
    raise Exception("Something wrong, can't begin LoRa radio")

LoRa.setDio2RfSwitch()
# Set frequency to 868 Mhz
print("Set frequency to 868 Mhz")
LoRa.setFrequency(868000000)

# Set RX gain to boosted gain
print("Set RX gain to boosted gain")
LoRa.setRxGain(LoRa.RX_GAIN_BOOSTED)

# Configure modulation parameter including spreading factor (SF), bandwidth (BW), and coding rate (CR)
print("Set modulation parameters:\n\tSpreading factor = 7\n\tBandwidth = 125 kHz\n\tCoding rate = 4/5")
sf = 7
bw = 125000
cr = 5
LoRa.setLoRaModulation(sf, bw, cr)

# Configure packet parameter including header type, preamble length, payload length, and CRC type
print("Set packet parameters:\n\tExplicit header type\n\tPreamble length = 12\n\tPayload Length = 15\n\tCRC on")
headerType = LoRa.HEADER_EXPLICIT
preambleLength = 12
payloadLength = 15
crcType = True
LoRa.setLoRaPacket(headerType, preambleLength, payloadLength, crcType)

# Set syncronize word for public network (0x3444)
print("Set syncronize word to 0x3444")
LoRa.setSyncWord(0x3444)

print("\n-- LoRa Receiver Listen --\n")

# Receive message continuously
while True :

    # Listen for a LoRa packet in 10 ms and sleep in 10 ms
    rxPeriod = 10
    sleepPeriod = 10
    LoRa.listen(rxPeriod, sleepPeriod)

    # Check for incoming LoRa packet
    if LoRa.available() :

        # Put received packet to message and counter variable
        message = ""
        while LoRa.available() > 1 :
            message += chr(LoRa.read())
        counter = LoRa.read()

        # Print received message and counter in serial
        print(f"{message}  {counter}")

        # Print packet/signal status including RSSI, SNR, and signalRSSI
        print("Packet status: RSSI = {0:0.2f} dBm | SNR = {1:0.2f} dB".format(LoRa.packetRssi(), LoRa.snr()))

        # Show received status in case CRC or header error occur
        status = LoRa.status()
        if status == LoRa.STATUS_CRC_ERR : print("CRC error")
        elif status == LoRa.STATUS_HEADER_ERR : print("Packet header error")

try :
    pass
except :
    LoRa.end()