from __future__ import annotations

from duck import Duck
from packet import DuckType, Topic, UnknownData
from receive_example import ReceiveDuck


class SendDuck(Duck):
    DUID = 1337

    def __init__(self):
        super().__init__(DuckType.UNKNOWN, self.DUID, 1)

    def tick(self):
        self.send(ReceiveDuck.DUID, Topic.WILD, UnknownData(b"hello there!"))
        print("tick!")


if __name__ == "__main__":
    duck = SendDuck()
    duck.run()
