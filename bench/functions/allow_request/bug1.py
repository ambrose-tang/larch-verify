def allow_request(timestamps: list[int], now: int, limit: int, window: int) -> bool:
    """Sliding-window rate limiter.

    `timestamps` are the times (in seconds) of previously accepted requests. A
    new request at time `now` is allowed if fewer than `limit` of them fall in
    the window (now - window, now], i.e. strictly after now - window and at or
    before now. Requires limit >= 0 and window >= 1.
    """
    recent = [t for t in timestamps if now - window <= t <= now]
    return len(recent) < limit
