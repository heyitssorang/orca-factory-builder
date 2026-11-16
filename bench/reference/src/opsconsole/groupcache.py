import time


class GroupCache:
    def __init__(self, directory, ttl=60, clock=time.monotonic):
        self.directory, self.ttl, self.clock = directory, ttl, clock
        self._c = {}

    def groups(self, name):
        now = self.clock()
        hit = self._c.get(name)
        if hit and now - hit[0] < self.ttl:
            return list(hit[1])
        g = self.directory.groups(name)
        self._c[name] = (now, g)
        return list(g)

    def invalidate(self, name=None):
        if name is None:
            self._c.clear()
        else:
            self._c.pop(name, None)
