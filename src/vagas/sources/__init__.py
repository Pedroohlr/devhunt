from .base import Found, Source
from .linkedin import LinkedInSource
from .indeed import IndeedSource
from .gupy import GupySource

SOURCES: dict[str, type[Source]] = {
    "linkedin": LinkedInSource,
    "indeed": IndeedSource,
    "gupy": GupySource,
}

__all__ = ["Found", "Source", "SOURCES", "LinkedInSource", "IndeedSource", "GupySource"]
