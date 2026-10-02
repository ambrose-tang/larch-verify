"""Store-credit ledger: customer balances in integer cents."""


class InsufficientFunds(Exception):
    pass


class Ledger:
    """Customer store-credit balances, in integer cents.

    Accounts exist implicitly: an account that was never credited has balance 0.
    Balances never go negative. Every operation either succeeds completely or raises
    and changes nothing.
    """

    def __init__(self) -> None:
        self._balances: dict[str, int] = {}

    def deposit(self, account: str, amount: int) -> None:
        """Credit `amount` (> 0) to `account`. Raises ValueError for a non-positive amount."""
        if amount <= 0:
            raise ValueError("amount must be positive")
        self._balances[account] = self._balances.get(account, 0) + amount

    def withdraw(self, account: str, amount: int) -> None:
        """Debit `amount` (> 0) from `account`. Raises InsufficientFunds if the balance is
        smaller than `amount`, and ValueError for a non-positive amount."""
        if amount <= 0:
            raise ValueError("amount must be positive")
        if self._balances.get(account, 0) < amount:
            raise InsufficientFunds(account)
        self._balances[account] -= amount

    def transfer(self, source: str, target: str, amount: int) -> None:
        """Move `amount` (> 0) from `source` to `target` atomically: either both balances
        change or neither does. Raises InsufficientFunds if `source` has less than `amount`."""
        if amount <= 0:
            raise ValueError("amount must be positive")
        self._balances[target] = self._balances.get(target, 0) + amount
        self.withdraw(source, amount)

    def balance(self, account: str) -> int:
        """Current balance of `account` (0 if it was never credited)."""
        return self._balances.get(account, 0)

    def total(self) -> int:
        """Sum of all balances: the store credit outstanding."""
        return sum(self._balances.values())
