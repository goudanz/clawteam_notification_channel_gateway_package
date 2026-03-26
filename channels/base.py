from abc import ABC, abstractmethod


class ChannelAdapter(ABC):
    @abstractmethod
    def start(self) -> None:
        raise NotImplementedError
