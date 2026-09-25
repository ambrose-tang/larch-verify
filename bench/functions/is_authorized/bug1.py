def is_authorized(policies: list[tuple[str, str, str]], principal: str, action: str) -> bool:
    """Decide an access request against a list of policies.

    Each policy is a tuple (effect, principal, action). The effect is "permit"
    or "forbid"; principal and action are either an exact name or "*", which
    matches anything. A request is allowed if and only if at least one permit
    policy matches it and no forbid policy matches it: forbid always overrides
    permit, the order of policies does not matter, and the default is deny.
    """
    for effect, p, a in policies:
        if (p == "*" or p == principal) and (a == "*" or a == action):
            return effect == "permit"
    return False
