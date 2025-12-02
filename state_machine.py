#This will control operational states of the Raspberry Pi

"""
1a. Wait in an idle and ideally low power state until an image is received
1b. Or until a packet arrives (receive) to be relayed

1b. if image, Run the detect.py computer vision pipeline, produces a packet with
 - animal species
 - GPS coordinates (from waveshare)
 - time (from waveshare)
2b. Process and relay the packet to the next node or PapaDuck
"""

