from duck import Duck
from packet import DuckType


class WildDuck(Duck):
    def __init__(self, duid: int):
        super().__init__(DuckType.UNKNOWN, duid, 1)

    def tick(self):
        print("tick!")
