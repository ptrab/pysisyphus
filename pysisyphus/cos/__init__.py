import logging

__all__ = [
    "NEB",
    "SimpleZTS",
    "ChainOfStates",
]

logger = logging.getLogger("cos")
logger.setLevel(logging.DEBUG)
# delay = True prevents creation of empty logfiles
handler = logging.FileHandler("cos.log", mode="w", delay=True)
fmt_str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
formatter = logging.Formatter(fmt_str)
handler.setFormatter(formatter)
logger.addHandler(handler)
