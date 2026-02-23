from __future__ import annotations

from duck import Duck
from packet import DuckType


class ReceiveDuck(Duck):
    DUID = 5928218522295812939

    def __init__(self):
        super().__init__(DuckType.UNKNOWN, self.DUID, 1)

    def tick(self):
        print("tick!")

if __name__ == "__main__":
    duck = ReceiveDuck()
    duck.run()
