import logging

__all__ = [
    "DampedVelocityVerlet",
    "Euler",
    "GonzalesSchlegel",
    "IMKMod",
]

logger = logging.getLogger("irc")
logger.setLevel(logging.DEBUG)
# delay = True prevents creation of empty logfiles
handler = logging.FileHandler("irc.log", mode="w", delay=True)
fmt_str = "%(levelname)s - %(message)s"
formatter = logging.Formatter(fmt_str)
handler.setFormatter(formatter)
logger.addHandler(handler)
