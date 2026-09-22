"""Provider registry."""
from typing import Callable, TypeVar

T=TypeVar("T")
_REGISTRY: dict[str,dict[str,Callable[...,object]]]={"stt":{},"llm":{},"tts":{}}

def register(kind:str,name:str)->Callable[[type[T]],type[T]]:
    """Register a provider class."""
    if kind not in _REGISTRY: raise ValueError(f"Unknown provider kind: {kind}")
    def decorator(cls:type[T])->type[T]:
        _REGISTRY[kind][name]=cls
        return cls
    return decorator

def create(kind:str,name:str,**kwargs:object)->object:
    """Create a registered provider."""
    try: cls=_REGISTRY[kind][name]
    except KeyError as exc: raise ValueError(f"Provider not registered: {kind}/{name}") from exc
    return cls(**kwargs)
