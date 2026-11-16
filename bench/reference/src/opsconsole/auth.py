from .errors import NotAuthorized


def is_authorized(store, user, tool):
    return tool in store.tools_for(user)


def require(store, user, tool):
    if not is_authorized(store, user, tool):
        raise NotAuthorized(f"{user} may not use {tool}")
