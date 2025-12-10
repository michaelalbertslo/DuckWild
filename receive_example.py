from __future__ import annotations

from duck import Duck
from packet import DuckType


class ReceiveDuck(Duck):
    DUID = 7331

    def __init__(self):
        super().__init__(DuckType.UNKNOWN, self.DUID, 1)


if __name__ == "__main__":
    duck = ReceiveDuck()
    duck.run()
