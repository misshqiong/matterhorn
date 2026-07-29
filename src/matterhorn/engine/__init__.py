__all__ = ["Engine", "Matter"]


def __getattr__(name: str):
    if name == "Engine":
        # Preserve the package-level SDK import while keeping plugin composition
        # in the top-level defaults module, outside the engine package.
        from matterhorn.defaults import Engine

        return Engine
    if name == "Matter":
        from matterhorn.engine.engine import Matter

        return Matter
    raise AttributeError(name)
